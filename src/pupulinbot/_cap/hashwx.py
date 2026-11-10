"""Native HashWX solver for Cap format-2, with a Python reference implementation.

HashWX is Cap's GPU-resistant proof of work: every challenge derives a fresh
hash function from a seed, built from a small VM with 32 generated programs,
a 16 KiB scratchpad and 64-bit integer arithmetic.  This module is a direct
port of tevador's reference implementation (LGPL-3.0):

    https://github.com/tevador/hashwx

The reference C sources are vendored by Cap at commit
``74b567a31276a4c5d7cb232a5b7639467f49f961``.  The generator tables in
``_hashwx_tables.py`` come from that revision, and the gold test vectors in
``tests/test_hashwx.py`` come from its ``doc/specification.md``.

Challenges are solved by the upstream native library built with ``cap-build``
and split into block/shard jobs with ``ProcessPoolExecutor``. The Python port
is retained for reference-vector verification and benchmarks only. Hosts that
cannot spawn processes use a single native worker.
"""

from __future__ import annotations

import hashlib
import logging
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from struct import pack_into, unpack_from

from pupulinbot._cap._hashwx_tables import (
    SRC_LOOKUP_DEEP,
    SRC_LOOKUP_SHALLOW,
)
from pupulinbot._cap.native_hashwx import create_runner

logger = logging.getLogger(__name__)

M64 = (1 << 64) - 1
M1 = 1 << 64
HALF = 1 << 63

NUM_PROGRAMS = 32
PROGRAM_SIZE = 11
BRANCH_COUNT = 32
NUM_REPEATS = 4
WRITABLE_SIZE = 16384
MEM_SIZE = WRITABLE_SIZE + 8

STORE, HALT, CBRANCH, UBRANCH = 15, 16, 13, 14
RMCG = 3
MUL_OR, MUL_XOR, MUL_ADD = 0, 1, 2
XOR_ROR, ADD_ROR, SUB_ROR = 4, 5, 6
XOR_ASR, ADD_ASR, SUB_ASR = 7, 8, 9
XOR_LSR, ADD_LSR, SUB_LSR = 10, 11, 12

MUL_IMMS = (1, 9, 33)
XAS_LOOKUP = (4, 5, 6, 7, 8, 9, 10, 11, 12)
OPCODE_TPL = (
    0x07, 0x0B, 0x13, 0x23, 0x43, 0x0D, 0x15, 0x25, 0x45, 0x19, 0x29, 0x49,
    0x31, 0x51, 0x61, 0x0E, 0x16, 0x26, 0x46, 0x1A, 0x2A, 0x4A, 0x32, 0x52,
    0x62, 0x1C, 0x2C, 0x4C, 0x34, 0x54, 0x64, 0x38, 0x58, 0x68, 0x70,
)

WORKERS_ENV = "MAJSOUL_CAP_WORKERS"


# ---------------------------------------------------------------------------
# SipHash generator used by program generation and register initialisation.
# ---------------------------------------------------------------------------

def _rotl(x, b):
    return ((x << b) | (x >> (64 - b))) & M64


def _sipround(v0, v1, v2, v3):
    v0 = (v0 + v1) & M64
    v2 = (v2 + v3) & M64
    v1 = _rotl(v1, 13)
    v3 = _rotl(v3, 16)
    v1 ^= v0
    v3 ^= v2
    v0 = _rotl(v0, 32)
    v2 = (v2 + v1) & M64
    v0 = (v0 + v3) & M64
    v1 = _rotl(v1, 17)
    v3 = _rotl(v3, 21)
    v1 ^= v2
    v3 ^= v0
    v2 = _rotl(v2, 32)
    return v0, v1, v2, v3


def _mix(v0, v1, v2, v3, k0, k1):
    v0 ^= k0
    v1 ^= k1
    v2 ^= k0
    v3 ^= k1
    for _ in range(4):
        v0, v1, v2, v3 = _sipround(v0, v1, v2, v3)
    return v0, v1, v2, v3


def rng8(k0, k1, salt):
    """Return R0..R7 plus an unused R8 slot for one nonce."""
    v0 = 0x736F6D6570736575 ^ k0
    v1 = 0x646F72616E646F6D ^ k1
    v2 = 0x6C7967656E657261 ^ k0
    v3 = 0x7465646279746573 ^ k1
    v3 ^= salt
    v0, v1, v2, v3 = _sipround(v0, v1, v2, v3)
    v0 ^= salt
    v2 ^= 0xBB
    for _ in range(3):
        v0, v1, v2, v3 = _sipround(v0, v1, v2, v3)
    n0, n1, n2, n3 = _mix(v0, v1, v2, v3, k0, k1)
    return [v3, v2, v1, v0, n3, n2, n1, n0, 0]


def finalize(r0, r1, r2, r3, r4, r5, r6, r7, r8):
    r0, r1, r2, r3 = _sipround(r0, r1, r2, r3)
    r4, r5, r6, r7 = _sipround(r4, r5, r6, r7)
    return r3 ^ r7 ^ r8


class _Rng:
    __slots__ = ("k0", "k1", "v0", "v1", "v2", "v3", "n")

    def __init__(self, k0, k1, salt):
        self.k0, self.k1 = k0, k1
        self.v0 = 0x736F6D6570736575 ^ k0
        self.v1 = 0x646F72616E646F6D ^ k1
        self.v2 = 0x6C7967656E657261 ^ k0
        self.v3 = 0x7465646279746573 ^ k1
        self.v3 ^= salt
        self.v0, self.v1, self.v2, self.v3 = _sipround(
            self.v0, self.v1, self.v2, self.v3
        )
        self.v0 ^= salt
        self.v2 ^= 0xBB
        for _ in range(3):
            self.v0, self.v1, self.v2, self.v3 = _sipround(
                self.v0, self.v1, self.v2, self.v3
            )
        self.n = 4

    def next(self):
        if self.n == 0:
            self.v0, self.v1, self.v2, self.v3 = _mix(
                self.v0, self.v1, self.v2, self.v3, self.k0, self.k1
            )
            self.n = 4
        self.n -= 1
        return (self.v3, self.v2, self.v1, self.v0)[3 - self.n]


# ---------------------------------------------------------------------------
# Program generation.
# ---------------------------------------------------------------------------

def _gen_imm(op, select):
    if op <= MUL_ADD:
        return MUL_IMMS[select % 3]
    if op <= SUB_ROR:
        return 1 + (select % 63)
    return 1 + (select % 3)


def generate_programs(k0, k1):
    """Generate the 32 programs for the hash function derived from a key."""
    rng = _Rng(k0, k1, M64)
    progs = []
    for i in range(NUM_PROGRAMS):
        is_last = i == NUM_PROGRAMS - 1
        is_deep = (i % 6) == 0 and not is_last
        progs.append(_generate_program(rng, is_last, is_deep))
    return progs


def _generate_program(rng, is_last, is_deep):
    gen = [rng.next() for _ in range(8)]
    seq = OPCODE_TPL[gen[7] % 35]

    code = [None] * PROGRAM_SIZE
    code[0] = (STORE, 0, 0, 0)
    code[1] = (RMCG, 0, 0, 0)

    dst_select = [0] * 7
    for i in range(7):
        sel = gen[i]
        dst_select[i] = (sel >> 32) & 0xFFFFFFFF
        if (seq >> i) & 1:
            code[i + 2] = ((sel & 0xFFFFFFFF) % 3, 0, 0, 0)
        else:
            code[i + 2] = (XAS_LOOKUP[(sel & 0xFFFFFFFF) % 9], 0, 0, 0)

    dst = [0] * 8
    for i in range(1, 8):
        dst[i] = i
        j = dst_select[i - 1] % (i + 1)
        dst[i] = dst[j]
        dst[j] = i

    perms = SRC_LOOKUP_DEEP if is_deep else SRC_LOOKUP_SHALLOW
    perm = perms[gen[7] % (24 if is_deep else 256)]
    src = [8] + [dst[perm[i]] for i in range(1, 8)]

    for i in range(8):
        op = code[i + 1][0]
        code[i + 1] = (op, dst[i], src[i], _gen_imm(op, rng.next()))

    code[9] = (UBRANCH if is_last else CBRANCH, 0, 0, 0)
    code[10] = (HALT, 0, 0, 0)
    return code


# ---------------------------------------------------------------------------
# Code generation: one Python function executes a complete HashWX hash.
#
# Emitting the 32 programs unrolled removes the opcode dispatch from the hot
# loop, which is the difference between a usable and an unusable Python port.
# ---------------------------------------------------------------------------

def _emit_op(lines, op, dst, src, imm, mem_phase):
    d = f"r{dst}"
    s = (
        f"U('<Q', mem, r{src} & 16383)[0]"
        if mem_phase and op != RMCG
        else f"r{src}"
    )
    if op == RMCG:
        lines.append(f"            t = ({d} * {s}) & M64")
        lines.append(
            f"            {d} = ((t >> {imm}) | (t << {64 - imm})) & M64"
        )
        # The branch flag is bit 5 of the rotated result.
        lines.append(f"            bf = {d}")
    elif op == MUL_OR:
        lines.append(f"            {d} = (({d} | {imm}) * {s}) & M64")
    elif op == MUL_XOR:
        lines.append(f"            {d} = (({d} ^ {imm}) * {s}) & M64")
    elif op == MUL_ADD:
        lines.append(f"            {d} = (({d} + {imm}) * {s}) & M64")
    elif op == XOR_ROR:
        lines.append(f"            t = {d}")
        lines.append(
            f"            {d} = (((t >> {imm}) | (t << {64 - imm})) & M64) ^ {s}"
        )
    elif op == ADD_ROR:
        lines.append(f"            t = {d}")
        lines.append(
            f"            {d} = ((((t >> {imm}) | (t << {64 - imm})) & M64) + {s}) & M64"
        )
    elif op == SUB_ROR:
        lines.append(f"            t = {d}")
        lines.append(
            f"            {d} = ((((t >> {imm}) | (t << {64 - imm})) & M64) - {s}) & M64"
        )
    elif op in (XOR_ASR, ADD_ASR, SUB_ASR):
        lines.append(f"            t = {d} - M1 if {d} >> 63 else {d}")
        if op == XOR_ASR:
            lines.append(f"            {d} = ((t >> {imm}) ^ {s}) & M64")
        elif op == ADD_ASR:
            lines.append(f"            {d} = ((t >> {imm}) + {s}) & M64")
        else:
            lines.append(f"            {d} = ((t >> {imm}) - {s}) & M64")
    elif op == XOR_LSR:
        lines.append(f"            {d} = ({d} >> {imm}) ^ {s}")
    elif op == ADD_LSR:
        lines.append(f"            {d} = (({d} >> {imm}) + {s}) & M64")
    elif op == SUB_LSR:
        lines.append(f"            {d} = (({d} >> {imm}) - {s}) & M64")
    else:
        raise ValueError(f"unknown hashwx opcode {op}")


def _emit_program(lines, code, mem_phase):
    lines.append("        while True:")
    if not mem_phase:
        lines.append("            sp -= 64")
        lines.append(
            "            P('<8Q', mem, sp, r7, r6, r5, r4, r3, r2, r1, r0)"
        )
    for idx in range(1, 9):
        _emit_op(lines, *code[idx], mem_phase)
    if code[9][0] == CBRANCH:
        lines.append("            if bc != 0 and (bf & 32) == 0:")
    else:
        lines.append("            if bc != 0:")
    lines.append("                bc -= 1")
    lines.append("                continue")
    lines.append("            break")


def compile_runner(progs):
    """Compile ``run(r) -> tuple(r0..r8)`` for one generated program list."""
    lines = ["def _run(r, mem=mem, U=U, P=P):"]
    lines.append("    r0, r1, r2, r3, r4, r5, r6, r7, r8 = r")
    lines.append("    P('<Q', mem, 16384, r8)")
    lines.append("    sp = 16384")
    for mem_phase in (False, True):
        lines.append("    for _rep in range(4):")
        lines.append("        bc = 32")
        for code in progs:
            _emit_program(lines, code, mem_phase)
    lines.append("    return (r0, r1, r2, r3, r4, r5, r6, r7, r8)")
    src = "\n".join(lines) + "\n"
    namespace = {
        "M64": M64,
        "M1": M1,
        "mem": bytearray(MEM_SIZE),
        "U": unpack_from,
        "P": pack_into,
    }
    exec(compile(src, "<hashwx>", "exec"), namespace)
    return namespace["_run"]


def hash_once(runner, k0, k1, nonce):
    r = rng8(k0, k1, nonce)
    r[8] = ((r[0] ^ r[4]) & -8) | 3
    return finalize(*runner(r))


def hash_seed(seed, nonce):
    """Reference helper: hash ``nonce`` with a 32-byte HashWX seed."""
    if len(seed) != 32:
        raise ValueError("hashwx seed must be 32 bytes")
    p0 = int.from_bytes(seed[0:8], "little")
    p1 = int.from_bytes(seed[8:16], "little")
    k0 = int.from_bytes(seed[16:24], "little")
    k1 = int.from_bytes(seed[24:32], "little")
    runner = compile_runner(generate_programs(p0, p1))
    return hash_once(runner, k0, k1, nonce)


# ---------------------------------------------------------------------------
# Solving: scan one block of nonces, then shard blocks across processes.
# ---------------------------------------------------------------------------

def solve_block(c_hex, block, n, target, start, end):
    """Scan nonce range ``[start, end)`` of one block; return ``(nonce, work)``."""
    challenge = bytes.fromhex(c_hex)
    seed = hashlib.sha256(challenge + block.to_bytes(8, "little")).digest()
    native = create_runner(seed)
    with native:
        for nonce in range(start, end):
            if native.hash(nonce) <= target:
                return nonce, nonce - start + 1
    return None, end - start


def _block_job(args):
    return solve_block(*args)


def _worker_count(workers):
    if workers is None:
        raw = os.environ.get(WORKERS_ENV) or os.environ.get("BATCHMORTAL_CAP_WORKERS")
        if raw:
            try:
                workers = int(raw)
            except ValueError:
                logger.warning("[CAP] ignoring invalid %s=%r", WORKERS_ENV, raw)
        if workers is None:
            workers = os.cpu_count() or 1
    return max(1, min(int(workers), 64))


def _solve_one(ex, c_hex, d, n, workers):
    target = M64 // d
    block = 0
    while True:
        shard = max(1, (n + workers - 1) // workers)
        futures = [
            ex.submit(
                _block_job,
                (
                    c_hex,
                    block,
                    n,
                    target,
                    block * n + start,
                    block * n + min(start + shard, n),
                ),
            )
            for start in range(0, n, shard)
        ]
        found = None
        failed = 0
        for fut in as_completed(futures):
            try:
                nonce, _ = fut.result()
            except Exception as exc:
                failed += 1
                logger.debug("[CAP] hashwx shard failed: %s", exc, exc_info=True)
                continue
            if nonce is not None:
                found = nonce
                break
        if found is not None:
            for fut in futures:
                fut.cancel()
            return found
        if failed == len(futures):
            raise RuntimeError("all hashwx shards failed")
        block += 1


def _new_executor(workers):
    # Use the platform default, matching cap.solve_pow.  Spawn-based contexts
    # re-import the host's __main__ in every worker, which is unsafe inside a
    # long-lived plugin process; failures degrade to a serial solve instead.
    return ProcessPoolExecutor(max_workers=workers)


def _solve_one_serial(c_hex, d, n):
    target = M64 // d
    block = 0
    while True:
        nonce, _ = solve_block(c_hex, block, n, target, block * n, (block + 1) * n)
        if nonce is not None:
            return nonce
        block += 1


def solve_hashwx(c_hex, d, n, workers=None):
    """Solve one HashWX challenge and return the nonce."""
    return solve_hashwx_many([(c_hex, d, n)], workers)[0]


def solve_hashwx_many(specs, workers=None):
    """Solve ``[(c, d, n), ...]`` challenges, returning nonces in order.

    One process pool is shared across challenges.  If processes cannot be
    spawned (for example inside a daemonic worker), the solve degrades to a
    serial scan instead of failing.
    """
    specs = list(specs)
    if not specs:
        return []
    # Validate once before starting workers or falling back to a serial loop.
    for c_hex, d, n in specs:
        if len(bytes.fromhex(c_hex)) != 32 or d < 1 or n < 1:
            raise ValueError("Invalid HashWX challenge: expected 32-byte c and positive d/n")
    # Fail once with the build instruction, before spawning processes or retrying shards.
    with create_runner(bytes(32)):
        pass
    workers = _worker_count(workers)
    logger.info(
        "[CAP] solving %d hashwx challenge(s) with %d worker(s) (d=%s, n=%s)",
        len(specs),
        workers,
        [d for _, d, _ in specs],
        specs[0][2],
    )

    if workers <= 1:
        return [_solve_one_serial(c, d, n) for c, d, n in specs]

    results = [None] * len(specs)
    executor = None
    try:
        executor = _new_executor(workers)
    except Exception:
        logger.warning("[CAP] process pool unavailable, solving hashwx serially",
                       exc_info=True)

    if executor is not None:
        try:
            with executor:
                for i, (c_hex, d, n) in enumerate(specs):
                    results[i] = _solve_one(executor, c_hex, d, n, workers)
            return results
        except Exception as exc:
            logger.warning(
                "[CAP] parallel hashwx failed (%s); falling back to serial",
                exc,
                exc_info=True,
            )

    for i, (c_hex, d, n) in enumerate(specs):
        if results[i] is None:
            results[i] = _solve_one_serial(c_hex, d, n)
    return results
