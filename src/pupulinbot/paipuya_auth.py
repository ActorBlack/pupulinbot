"""Shared CAP credentials and bounded, isolated proof-of-work execution."""

import asyncio
import json
import math
import os
import signal
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from .paipuya_gate import PaipuyaGate


class PaipuyaAuthError(RuntimeError):
    pass


class PaipuyaAccess:
    def __init__(
        self,
        token: str | None = None,
        *,
        auto_cap: bool = False,
        endpoint: str = "https://akcap.pikapika.me/14f343ec68/",
        cache_dir: Path = Path("data/cap"),
        workers: int = 2,
        timeout: float = 60,
        library: str | None = None,
        rate_limit_file: Path = Path("data/paipuya-rate.sqlite"),
    ):
        self.static_token = token
        self.key_invalid = False
        self.gate = PaipuyaGate(rate_limit_file)
        self.auto_cap = auto_cap
        self.endpoint = endpoint
        self.cache_dir = cache_dir
        self.workers = workers
        self.timeout = timeout
        self.library = library
        self._token: str | None = None
        self._expires = 0.0
        self._blocked_until = 0.0
        self._lock = asyncio.Lock()

    @property
    def using_api_key(self):
        return bool(self.static_token and not self.key_invalid)

    def validate_target(self, base_url):
        if not self.static_token:
            return
        url = urlsplit(base_url)
        if (
            url.scheme != "https"
            or url.hostname != "5-data.amae-koromo.com"
            or url.port not in {None, 443}
            or url.username
            or url.password
            or url.path.rstrip("/") not in {"/api/v2/pl4", "/api/v2/pl3"}
            or url.query
            or url.fragment
        ):
            raise PaipuyaAuthError(
                "API Key 仅允许访问 https://5-data.amae-koromo.com/api/v2/pl3 或 pl4"
            )

    async def wait_turn(self):
        self.check_cooldown()
        await self.gate.wait()
        # A concurrent request can receive Retry-After while this one was queued.
        self.check_cooldown()

    def invalidate_key(self, rejected):
        if self.using_api_key and rejected == self.static_token:
            self.key_invalid = True
            return True
        return False

    def token(self) -> str | None:
        if self.using_api_key:
            return self.static_token
        return self._token if time.time() < self._expires - 30 else None

    def check_cooldown(self) -> None:
        remaining = self._blocked_until - time.monotonic()
        if remaining > 0:
            raise PaipuyaAuthError(f"牌谱屋访问暂缓，请约 {int(remaining) + 1} 秒后重试。")

    def cooldown(self, seconds: float) -> None:
        self._blocked_until = max(self._blocked_until, time.monotonic() + seconds)

    async def refresh(self, rejected: str | None) -> str:
        async with self._lock:
            self.check_cooldown()
            current = self.token()
            if self.using_api_key:
                return current
            if current and current != rejected:
                return current
            try:
                data = await self._run_solver(rejected)
                token = data["token"]
                expires = float(data["expires"]) / 1000
                if (
                    not isinstance(token, str)
                    or not token
                    or not math.isfinite(expires)
                    or expires <= time.time() + 30
                ):
                    raise ValueError("Invalid CAP credentials")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.cooldown(60)
                raise PaipuyaAuthError(
                    "牌谱屋 CAP 验证失败或超时，已暂停重试 60 秒；"
                    "请检查原生库、网络或配置有效 API Key。"
                ) from exc
            self._token, self._expires = token, expires
            return token

    async def _run_solver(self, rejected: str | None) -> dict:
        env = dict(os.environ)
        if self.library:
            env["MAJSOUL_HASHWX_LIBRARY"] = self.library
        # Separate process group permits killing multiprocessing children on timeout/cancel.
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "pupulinbot.cap_worker",
            "--endpoint",
            self.endpoint,
            "--cache-dir",
            str(self.cache_dir.resolve()),
            "--workers",
            str(self.workers),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=os.name == "posix",
        )
        try:
            async with asyncio.timeout(self.timeout):
                output, _ = await proc.communicate(json.dumps({"rejected": rejected}).encode())
            if proc.returncode:
                raise PaipuyaAuthError("CAP worker exited unsuccessfully")
            return json.loads(output)
        finally:
            # Also stop any leftover workers after the solver's parent has exited.
            if os.name == "posix":
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif proc.returncode is None:
                proc.kill()
            await proc.wait()
