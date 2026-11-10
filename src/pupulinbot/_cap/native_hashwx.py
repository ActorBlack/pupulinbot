"""ctypes backend for the pinned upstream HashWX native JIT."""
import ctypes
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from functools import lru_cache
from pathlib import Path

from filelock import FileLock

REVISION = "74b567a31276a4c5d7cb232a5b7639467f49f961"


class NativeBackendUnavailable(RuntimeError):
    """A missing or incompatible library needs configuration, rather than a retry."""


def build_dir() -> Path:
    cache = Path(os.environ.get("PUPULIN_HASHWX_CACHE_DIR", "data/hashwx"))
    directory = cache / REVISION / f"{sys.platform}-{platform.machine()}"
    conda_prefix = os.environ.get("CONDA_PREFIX")
    if conda_prefix and Path(conda_prefix).resolve() == Path(sys.prefix).resolve():
        # Conda compiler paths and runtime libraries belong to that environment.
        tag = hashlib.sha256(str(Path(conda_prefix).resolve()).encode()).hexdigest()[:12]
        directory /= f"conda-{tag}"
    return directory


def library_path(*, configured: bool = True) -> Path:
    if configured and os.environ.get("MAJSOUL_HASHWX_LIBRARY"):
        return Path(os.environ["MAJSOUL_HASHWX_LIBRARY"]).expanduser().resolve()
    name = "hashwx.dll" if sys.platform == "win32" else (
        "libhashwx.dylib" if sys.platform == "darwin" else "libhashwx.so"
    )
    directory = build_dir() / "Release" if sys.platform == "win32" else build_dir()
    return directory / name


@lru_cache(maxsize=4)
def _load(path: str):
    lib = ctypes.CDLL(path)
    lib.hashwx_alloc.argtypes = [ctypes.c_int]
    lib.hashwx_alloc.restype = ctypes.c_void_p
    lib.hashwx_make.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    lib.hashwx_make.restype = None
    lib.hashwx_exec.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
    lib.hashwx_exec.restype = ctypes.c_uint64
    lib.hashwx_free.argtypes = [ctypes.c_void_p]
    lib.hashwx_free.restype = None
    # Reject incompatible external libraries before using them for proof-of-work.
    ctx = _alloc(lib)
    try:
        lib.hashwx_make(ctx, bytes.fromhex(
            "5468697320697320612074657374207365656420666f72206861736877780000"
        ))
        if lib.hashwx_exec(ctx, 0) != 0x973684176F8EE362:
            raise RuntimeError("原生 HashWX 库未通过官方测试向量")
    finally:
        lib.hashwx_free(ctx)
    return lib


def _alloc(lib):
    ctx = lib.hashwx_alloc(1)
    if ctx == ctypes.c_void_p(-1).value:
        ctx = lib.hashwx_alloc(0)
    if not ctx or ctx == ctypes.c_void_p(-1).value:
        raise RuntimeError("无法分配原生 HashWX 上下文")
    return ctx


class NativeRunner:
    def __init__(self, seed: bytes, path: Path | None = None):
        if len(seed) != 32:
            raise ValueError("HashWX seed must be 32 bytes")
        self.lib = _load(str(path or library_path()))
        self.ctx = _alloc(self.lib)
        self.lib.hashwx_make(self.ctx, seed)

    def hash(self, nonce: int) -> int:
        if not self.ctx:
            raise RuntimeError("HashWX 上下文已释放")
        return self.lib.hashwx_exec(self.ctx, nonce)

    def close(self):
        if self.ctx:
            self.lib.hashwx_free(self.ctx)
            self.ctx = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def create_runner(seed: bytes) -> NativeRunner:
    try:
        return NativeRunner(seed)
    except (OSError, AttributeError, RuntimeError):
        raise NativeBackendUnavailable(
            "原生 HashWX 后端不可用，请先运行 majsoul-analyzer cap-build"
        ) from None


def build_native() -> Path:
    cmake = shutil.which("cmake") or str(Path(sys.executable).parent / "cmake")
    if not Path(cmake).is_file():
        raise RuntimeError("编译原生 HashWX 需要 cmake 和 C11 编译器")
    source = Path(__file__).parent / "vendor" / "hashwx"
    output = build_dir()
    output.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(output) + ".lock"):
        configure = [cmake, "-S", str(source), "-B", str(output.resolve()),
                     "-DCMAKE_BUILD_TYPE=Release"]
        if not (output / "CMakeCache.txt").exists() and shutil.which("ninja"):
            configure += ["-G", "Ninja"]
        subprocess.run(configure, check=True)
        subprocess.run([cmake, "--build", str(output), "--config", "Release",
                        "--target", "hashwx", "--parallel", "4"], check=True)
    # Validate what was just built, independently of an external-library override.
    path = library_path(configured=False)
    # Validate the installed library before reporting success.
    with NativeRunner(bytes(32), path):
        pass
    return path


def benchmark(python_hashes: int = 1000, native_hashes: int = 100000) -> dict:
    """Compare steady-state single-thread hashing; seed compilation is excluded."""
    from .hashwx import compile_runner, generate_programs, hash_once
    seed = bytes(range(32))
    p0, p1, k0, k1 = [int.from_bytes(seed[i:i + 8], "little") for i in range(0, 32, 8)]
    runner = compile_runner(generate_programs(p0, p1))
    with NativeRunner(seed) as native:
        for nonce in (0, 1, 42, 123456, 987654321123456789):
            if hash_once(runner, k0, k1, nonce) != native.hash(nonce):
                raise RuntimeError("原生与 Python HashWX 结果不一致")
        start = time.perf_counter()
        for nonce in range(python_hashes):
            hash_once(runner, k0, k1, nonce)
        python_seconds = time.perf_counter() - start
        start = time.perf_counter()
        for nonce in range(native_hashes):
            native.hash(nonce)
        native_seconds = time.perf_counter() - start
    python_rate, native_rate = python_hashes / python_seconds, native_hashes / native_seconds
    result = {
        "seed": seed.hex(), "threads": 1, "includes_seed_compilation": False,
        "cpu": platform.processor() or platform.machine(),
        "library_revision": REVISION, "library": str(library_path()),
        "python": {"hashes": python_hashes, "seconds": python_seconds,
                   "hashes_per_second": python_rate},
        "native": {"hashes": native_hashes, "seconds": native_seconds,
                   "hashes_per_second": native_rate},
        "native_vs_python": native_rate / python_rate,
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return result
