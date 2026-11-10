"""Cap proof-of-work CAPTCHA solver for the amae-koromo (牌谱屋) API.

The data API (``5-data.amae-koromo.com``) answers record requests with ``429``
and an ``x-cap-token-required`` marker until the caller solves a Cap challenge
and retries with ``X-Cap-Token: <token>``.  Two challenge formats are handled:

**Format 1** (legacy) — ``{challenge: {c, s, d}, token, instrumentation}``.
The ``c`` (salt, target) pairs are regenerated from the challenge JWT with the
widget's FNV-1a + xorshift32 PRNG, a SHA-256 proof of work is solved for each
pair, and the instrumentation script's four state values are evaluated with a
small interpreter.

**Format 2** (current) — ``{format: 2, challenges: [{protocol, payload}...]}``.
The endpoint currently serves four ``hashwx`` challenges plus one
``instrumentation`` challenge.  HashWX is Cap's GPU-resistant PoW; it is solved
by :mod:`pupulinbot._cap.hashwx`.  The instrumentation script computes the same
four values as format 1, and additionally expects a synthetic browser ``p``
probe (see :func:`_instrumentation_probe`) so the solver does not need to run
a browser.

Both formats finish with ``POST {endpoint}/redeem`` -> ``{success, token,
expires}``. The token is cached in memory and on disk (so concurrent worker
processes share one solve) and reused until it expires. The whole solve is
retried with backoff because the CAP endpoint itself is behind a WAF that
answers unrelated requests with ``403``.

Protocol details reverse-engineered from the open-source Cap client:
https://github.com/tiagozip/cap (widget/src/src/cap.js, core/src/hashwx.js)
"""

import base64
import datetime
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
import zlib
from concurrent.futures import ProcessPoolExecutor
from contextlib import nullcontext

import requests

from pupulinbot._cap.hashwx import solve_hashwx_many
from pupulinbot._cap.native_hashwx import NativeBackendUnavailable

try:  # filelock is a project dependency, but stay usable without it.
    from filelock import FileLock
except ImportError:  # pragma: no cover - exercised only in minimal installs
    FileLock = None

logger = logging.getLogger(__name__)

DEFAULT_CAP_ENDPOINT = "https://akcap.pikapika.me/14f343ec68/"
DEFAULT_CHALLENGE_RETRIES = 4

# Headers that satisfy the WAF in front of the CAP endpoint. They mirror what a
# real browser sends to ``https://amae-koromo.sapk.ch`` (the CAP server is
# cross-origin, hence the Sec-Fetch-* values).
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Origin": "https://amae-koromo.sapk.ch",
    "Referer": "https://amae-koromo.sapk.ch/",
    "Sec-Fetch-Site": "cross-site",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
}


# ---------------------------------------------------------------------------
# 32-bit integer helpers that replicate JavaScript semantics exactly.
# ---------------------------------------------------------------------------

def _to_int32(x):
    """JS ToInt32: wrap an integer into the signed 32-bit range."""
    x = int(x) & 0xFFFFFFFF
    return x - 0x100000000 if x >= 0x80000000 else x


def _js_mod(a, b):
    """JS ``%``: sign follows the dividend (truncation toward zero)."""
    return a - int(a / b) * b


def _utf16_units(s):
    """Yield UTF-16 code units, mirroring JS ``str.charCodeAt``."""
    for ch in s:
        cp = ord(ch)
        if cp > 0xFFFF:
            cp -= 0x10000
            yield 0xD800 + (cp >> 10)
            yield 0xDC00 + (cp & 0x3FF)
        else:
            yield cp


# ---------------------------------------------------------------------------
# PRNG used to regenerate (salt, target) from the challenge token.
# Mirrors the widget's `prng` (FNV-1a seed + xorshift32 stream).
# ---------------------------------------------------------------------------

def fnv1a(s):
    h = _to_int32(2166136261)
    for c in _utf16_units(s):
        h = _to_int32(h ^ c)
        h = (
            h
            + _to_int32(h << 1)
            + _to_int32(h << 4)
            + _to_int32(h << 7)
            + _to_int32(h << 8)
            + _to_int32(h << 24)
        )
    return h & 0xFFFFFFFF


def prng(seed, length):
    """Deterministic hex string of `length` chars seeded by `seed`."""
    state = fnv1a(seed)
    result = ""
    while len(result) < length:
        state = (state ^ ((state << 13) & 0xFFFFFFFF)) & 0xFFFFFFFF
        state = (state ^ (state >> 17)) & 0xFFFFFFFF
        state = (state ^ ((state << 5) & 0xFFFFFFFF)) & 0xFFFFFFFF
        result += format(state, "08x")
    return result[:length]


# ---------------------------------------------------------------------------
# SHA-256 proof of work: find nonce where sha256(salt + str(nonce)) starts
# with `target` (the difficulty prefix). The nonce is appended as a decimal
# string, exactly like the browser solver.
# ---------------------------------------------------------------------------

def _solve_pow_one(salt_target):
    salt, target = salt_target
    nonce = 0
    while not hashlib.sha256((salt + str(nonce)).encode()).hexdigest().startswith(target):
        nonce += 1
    return nonce


def solve_pow(challenges, workers=None):
    """Solve each (salt, target) pair, returning the nonce list in order."""
    if not challenges:
        return []
    if workers is None:
        workers = max(1, min(os.cpu_count() or 4, 16, len(challenges)))
    if workers <= 1 or len(challenges) <= 1:
        return [_solve_pow_one(ch) for ch in challenges]
    try:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            return list(ex.map(_solve_pow_one, challenges))
    except Exception:
        logger.debug("ProcessPoolExecutor unavailable, solving serially", exc_info=True)
        return [_solve_pow_one(ch) for ch in challenges]


# ---------------------------------------------------------------------------
# Instrumentation evaluation.
#
# The challenge payload includes a compressed browser-environment script. The
# redeem request must carry `instr: {i, state, ts}` where `state` is the four
# values the script posts back. Those values are computed from the script's own
# constants (init values, a small bitwise/DOM simulation, and salts), so we can
# evaluate the script with a mini-interpreter instead of running a browser.
# ---------------------------------------------------------------------------

_IDENT = r"[A-Za-z0-9_$]+"

_OP_RE = re.compile(
    r"~\((?P<a>{i})\s*&\s*(?P<b>{i})\)"  # ~(a & b)
    r"|\(\((?P<c>{i})\^(?P<s>\d+)\)&0x7FFFFFFF\)%900000\+100000"  # salt
    r"|(?P<ua>{i})\s*\^\s*\(navigator\.userAgent\s*\?\s*(?P<k>\d+)\s*:\s*\d+\)"  # ua xor
    r"|(?P<x1>{i})\s*\^\s*(?P<x2>{i})"  # a ^ b
    r"|(?P<d1>{i})\s*&\s*(?P<d2>{i})"  # a & b
    r"|(?P<o1>{i})\s*\|\s*(?P<o2>{i})"  # a | b
    r"|(?P<fn>{i})\s*\(\s*(?P<f1>{i})\s*,\s*(?P<f2>{i})\s*,\s*(?P<f3>{i})\s*\)"
    .format(i=_IDENT)
)


def _fn_helper(a, b, c):
    """fnHelper(a, b, c) == (b ^ a) | (c ^ b), the server's op-4 formula."""
    return _to_int32((b ^ a) | (c ^ b))


def _dom_helper(x, y, z):
    """domHelper(x, y, z), reimplementing the server's `domSumMock`."""

    def build_chain(parent, val):
        cur, v = parent, val
        for _ in range(8):
            child = {"parent": cur, "text": str(v)}
            if (v & 1) == 0:
                cur = child
            v = v >> 1
        return cur

    def walk(node, root, s):
        if node is None or node is root:
            return _js_mod(s, 256)
        return walk(node["parent"], root, s + int(node["text"]))

    return _to_int32(
        walk(build_chain(build_chain(build_chain("ROOT", x), y), z), "ROOT", 0)
    )


def _split_top(stmt):
    """Split JS on top-level ';' (brace-aware)."""
    parts, depth, cur = [], 0, ""
    for ch in stmt:
        if ch == "{":
            depth += 1
        if ch == "}":
            depth -= 1
        cur += ch
        if ch == ";" and depth == 0:
            parts.append(cur)
            cur = ""
    if cur.strip():
        parts.append(cur)
    return parts


def _iter_functions(text):
    """Yield (name, full_text) for each top-level `function name(...){...}`."""
    pos = 0
    while True:
        m = re.search(r"\bfunction ({i})\(".format(i=_IDENT), text[pos:])
        if not m:
            return
        name = m.group(1)
        start = pos + m.start()
        open_brace = text.find("{", start)
        if open_brace == -1:
            return
        depth = 0
        i = open_brace
        while i < len(text):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        yield name, text[start : i + 1]
        pos = i + 1


def _classify_functions(text):
    """Map helper function name -> True when it is the DOM helper."""
    return {name: "document.createElement" in full for name, full in _iter_functions(text)}


def _decompress_instrumentation(blob):
    """The instrumentation field is base64(deflate-raw(utf-8 JS))."""
    raw = base64.b64decode(blob)
    return zlib.decompress(raw, -15).decode("utf-8")


# ---------------------------------------------------------------------------
# Synthetic browser probe.
#
# Format-2 challenges with ``blockAutomatedBrowsers`` enabled require a ``p``
# object alongside the computed state.  The server scores it with
# ``detectAutomation``: type completeness, Chromium/Gecko consistency,
# WebDriver markers, screen/window geometry, headless UA tokens and whether
# native DOM APIs were monkeypatched.  These values describe a plain desktop
# Chrome session and pass every gating check.
# ---------------------------------------------------------------------------

def _probe_font_count(js):
    """Read the server's font probe stack length from the served script."""
    m = re.search(
        r"fontWidths.*?var\s+[A-Za-z0-9_$]+\s*=\s*(\[.*?\])\s*;"
        r"\s*var\s+[A-Za-z0-9_$]+\s*=\s*\[\]",
        js,
        re.S,
    )
    if m:
        try:
            return len(json.loads(m.group(1)))
        except (ValueError, TypeError):
            pass
    return 17


def _probe_font_widths(count):
    # Real browsers report fractional text widths; whole-pixel values are a
    # canvas/font-fingerprinting tell, so keep every entry fractional.
    return [
        180 + (i * 37) % 70 + ((i * 7) % 9 + 1) / 10.0
        for i in range(max(1, count))
    ]


def _instrumentation_probe(font_count=17):
    return {
        "ua": _BROWSER_HEADERS["User-Agent"],
        "productSub": "20030107",
        "oscpu": "__undefined",
        "uaDataPresent": True,
        "uaData": {
            "mobile": False,
            "brands": ["Chromium/126", "Google Chrome/126", "Not.A/Brand/24"],
        },
        "plugins": {"length": 5},
        "pdfViewerEnabled": True,
        "engine": {"hasMozInnerScreenX": False, "hasChrome": True},
        "screen": {"width": 1920, "height": 1080},
        "outerWH": [1920, 1000],
        "innerWH": [1280, 800],
        "isExtended": False,
        "fontWidths": _probe_font_widths(font_count),
        "tamper": {
            "getParameterWebGL": True,
            "toDataURL": True,
            "getImageData": True,
            "permissionsQuery": True,
            "fnToString": True,
        },
        "webdriver": False,
        "deviceMemory": 8,
    }


def parse_instrumentation(js):
    """Compute {id: str, state: {var: value}} from a Cap instrumentation script."""
    m = re.search(r'nonce:\s*"([0-9a-f]{32})"', js)
    if not m:
        raise ValueError("cap instrumentation: missing id")
    instr_id = m.group(1)

    decl = re.search(
        r"var ({i})=(\d+);var ({i})=(\d+);var ({i})=(\d+);var ({i})=(\d+);".format(
            i=_IDENT
        ),
        js,
    )
    if not decl:
        raise ValueError("cap instrumentation: missing var declarations")
    g = decl.groups()
    names = [g[0], g[2], g[4], g[6]]
    vars_ = dict(zip(names, [int(g[1]), int(g[3]), int(g[5]), int(g[7])]))

    rest = js[decl.end() :]
    m_out = re.search("var " + _IDENT + r"=\{\};", rest)
    if not m_out:
        raise ValueError("cap instrumentation: missing out object")
    ops_text = rest[: m_out.start()]

    # The helper function declarations sit directly against the first op with
    # no ';' between them, so strip them out before statement splitting.
    fn_is_dom = _classify_functions(ops_text)
    ops_only = ops_text
    for _name, full in _iter_functions(ops_text):
        ops_only = ops_only.replace(full, "", 1)

    for stmt in _split_top(ops_only):
        s = stmt.strip()
        if not s:
            continue
        mm = _OP_RE.search(s)
        if not mm:
            continue
        dest = s.split("=")[0].strip()
        if mm.group("a") is not None:
            vars_[dest] = _to_int32(~(vars_[mm.group("a")] & vars_[mm.group("b")]))
        elif mm.group("c") is not None:
            vars_[dest] = (
                (vars_[mm.group("c")] ^ int(mm.group("s"))) & 0x7FFFFFFF
            ) % 900000 + 100000
        elif mm.group("ua") is not None:
            # navigator.userAgent is always truthy in a browser, so the
            # correctKey branch wins.
            vars_[dest] = _to_int32(vars_[dest] ^ int(mm.group("k")))
        elif mm.group("x1") is not None:
            vars_[dest] = _to_int32(vars_[mm.group("x1")] ^ vars_[mm.group("x2")])
        elif mm.group("d1") is not None:
            vars_[dest] = _to_int32(vars_[mm.group("d1")] & vars_[mm.group("d2")])
        elif mm.group("o1") is not None:
            vars_[dest] = _to_int32(vars_[mm.group("o1")] | vars_[mm.group("o2")])
        elif mm.group("fn") is not None:
            a, b, c = vars_[mm.group("f1")], vars_[mm.group("f2")], vars_[mm.group("f3")]
            if fn_is_dom.get(mm.group("fn"), False):
                vars_[dest] = _dom_helper(a, b, c)
            else:
                vars_[dest] = _fn_helper(a, b, c)

    return instr_id, {n: vars_[n] for n in names}


# ---------------------------------------------------------------------------
# Full solve flow with token caching.
# ---------------------------------------------------------------------------

_cap_lock = threading.Lock()
_cached_token = {"token": None, "expires": 0.0}


def _token_cache_path(endpoint):
    key = hashlib.sha256(endpoint.rstrip("/").encode("utf-8")).hexdigest()[:16]
    return os.path.join(tempfile.gettempdir(), f"batchmortal-cap-{key}.json")


def _memory_token():
    if _cached_token["token"] and time.time() * 1000 < _cached_token["expires"]:
        return _cached_token["token"], _cached_token["expires"]
    return None, 0.0


def _read_cached_token(endpoint):
    try:
        with open(_token_cache_path(endpoint), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        token = data.get("token")
        expires = float(data.get("expires") or 0)
    except (OSError, ValueError, TypeError):
        return None, 0.0
    if token and time.time() * 1000 < expires:
        return token, expires
    return None, 0.0


def _write_cached_token(endpoint, token, expires):
    path = _token_cache_path(endpoint)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"token": token, "expires": expires}, fh)
        os.replace(tmp, path)
    except OSError:
        logger.debug("[CAP] could not persist token cache", exc_info=True)
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _clear_cached_token(endpoint):
    try:
        os.unlink(_token_cache_path(endpoint))
    except OSError:
        pass


def clear_cap_token(endpoint=DEFAULT_CAP_ENDPOINT):
    """Drop the cached token, in memory and on disk (used by tests)."""
    with _cap_lock:
        _cached_token["token"] = None
        _cached_token["expires"] = 0.0
    _clear_cached_token(endpoint)


def _solve_cap_v2(challenge, workers):
    """Build the ``solutions`` array for a format-2 Cap challenge."""
    solutions = [None] * len(challenge["challenges"])
    hashwx_specs = []
    hashwx_indexes = []

    for idx, item in enumerate(challenge["challenges"]):
        protocol = item.get("protocol")
        payload = item.get("payload") or {}

        if protocol == "hashwx":
            hashwx_specs.append(
                (str(payload["c"]), int(payload["d"]), int(payload["n"]))
            )
            hashwx_indexes.append(idx)
        elif protocol == "sha256-pow":
            nonce = solve_pow([(payload["salt"], payload["target"])], workers=workers)[0]
            solutions[idx] = {"nonce": nonce}
        elif protocol == "instrumentation":
            js = _decompress_instrumentation(payload["blob"])
            instr_id, state = parse_instrumentation(js)
            solutions[idx] = {
                "instr": {
                    "i": instr_id,
                    "state": state,
                    "p": _instrumentation_probe(_probe_font_count(js)),
                    "ts": int(time.time() * 1000),
                }
            }
        else:
            raise RuntimeError(f"unsupported cap format-2 protocol: {protocol!r}")

    if hashwx_specs:
        nonces = solve_hashwx_many(hashwx_specs, workers=workers)
        for idx, nonce in zip(hashwx_indexes, nonces):
            solutions[idx] = {"nonce": str(nonce)}

    return solutions


def _solve_cap_once(endpoint, headers, workers):
    """Run challenge -> solve -> redeem once, returning (token, expires_ms)."""
    session = requests.Session()
    session.headers.update(headers or _BROWSER_HEADERS)

    resp = session.post(f"{endpoint.rstrip('/')}/challenge", timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(f"cap challenge failed: HTTP {resp.status_code}: {resp.text[:200]}")
    challenge = resp.json()

    if challenge.get("format") == 2:
        payload = {
            "token": challenge["token"],
            "solutions": _solve_cap_v2(challenge, workers),
        }
        resp = session.post(
            f"{endpoint.rstrip('/')}/redeem",
            json=payload,
            timeout=60,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"cap redeem failed: HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        if not data.get("success"):
            raise RuntimeError(f"cap redeem rejected: {data.get('error') or data}")
        expires = data.get("expires")
        if isinstance(expires, str):
            try:
                expires = int(
                    datetime.datetime.fromisoformat(expires.replace("Z", "+00:00")).timestamp()
                    * 1000
                )
            except (ValueError, TypeError):
                expires = time.time() * 1000 + 20 * 60 * 1000
        return data["token"], int(expires)

    cfg = challenge["challenge"]
    count, size, difficulty = cfg["c"], cfg["s"], cfg["d"]
    token = challenge["token"]

    pairs = [
        (prng(f"{token}{i}", size), prng(f"{token}{i}d", difficulty))
        for i in range(1, count + 1)
    ]
    solutions = solve_pow(pairs, workers=workers)

    instr = None
    if challenge.get("instrumentation"):
        js = _decompress_instrumentation(challenge["instrumentation"])
        instr_id, state = parse_instrumentation(js)
        instr = {"i": instr_id, "state": state, "ts": int(time.time() * 1000)}

    payload = {"token": token, "solutions": solutions}
    if instr is not None:
        payload["instr"] = instr

    resp = session.post(
        f"{endpoint.rstrip('/')}/redeem",
        json=payload,
        timeout=60,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"cap redeem failed: HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"cap redeem rejected: {data.get('error') or data}")
    expires = data.get("expires")
    if isinstance(expires, str):
        try:
            expires = int(datetime.datetime.fromisoformat(expires.replace("Z", "+00:00")).timestamp() * 1000)
        except (ValueError, TypeError):
            expires = time.time() * 1000 + 20 * 60 * 1000
    return data["token"], int(expires)


def refresh_cap_token(endpoint=DEFAULT_CAP_ENDPOINT, headers=None, workers=None, retries=DEFAULT_CHALLENGE_RETRIES):
    """Solve a fresh CAP token and cache it.

    Mirrors the frontend's singleton ``refreshCapToken``: the lock is held for
    the whole solve, so concurrent callers block, then see the freshly cached
    token and return it instead of solving again.
    """
    with _cap_lock:
        token, expires = _memory_token()
        if token:
            return token

        # The token is shared through a file so multiple worker processes do
        # not each run an expensive proof of work for the same server.
        token, expires = _read_cached_token(endpoint)
        if token:
            _cached_token["token"], _cached_token["expires"] = token, expires
            return token

        lock = (
            FileLock(_token_cache_path(endpoint) + ".lock")
            if FileLock is not None
            else nullcontext()
        )
        with lock:
            token, expires = _memory_token()
            if token:
                return token
            token, expires = _read_cached_token(endpoint)
            if token:
                _cached_token["token"], _cached_token["expires"] = token, expires
                return token

            last_error = None
            for attempt in range(max(1, retries)):
                try:
                    token, expires = _solve_cap_once(endpoint, headers, workers)
                except NativeBackendUnavailable:
                    raise
                except Exception as exc:  # WAF may answer 403 spuriously
                    last_error = exc
                    logger.warning(f"[CAP] solve attempt {attempt + 1} failed: {exc}")
                    time.sleep(2 + attempt)
                    continue
                _cached_token["token"] = token
                _cached_token["expires"] = expires
                _write_cached_token(endpoint, token, expires)
                logger.info(
                    f"[CAP] temporary token obtained "
                    f"(expires in ~{(expires - time.time() * 1000) / 60000:.0f} min)"
                )
                return token

    raise RuntimeError(f"failed to obtain a CAP token after {retries} attempts: {last_error}")


def get_cap_token():
    """Return the cached non-expired CAP token, or ``None``.

    This intentionally never solves a challenge — matching the frontend's
    ``getCachedCapToken()``, which only reads the in-memory token. A solve is
    triggered exclusively by the 429 + ``x-cap-token-required`` path.
    """
    with _cap_lock:
        token, _ = _memory_token()
        if token:
            return token
        token, expires = _read_cached_token(DEFAULT_CAP_ENDPOINT)
        if token:
            _cached_token["token"], _cached_token["expires"] = token, expires
            return token
    return None
