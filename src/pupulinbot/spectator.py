"""Majsoul public-table monitoring via an isolated official browser session."""

import asyncio
import json
import math
import os
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from .live import LiveGame, LiveServiceError
from .query_cache import QueryCache

FILTERS = (208, 209, 211, 212, 215, 216, 221, 222, 223, 224, 225, 226)


def parse_head(head, *, now=None):
    """Normalize official GameLiveHead; never invent time, seats or account IDs."""
    try:
        uuid = head["uuid"]
        if (
            not isinstance(uuid, str)
            or not uuid
            or len(uuid) > 120
            or any(c in uuid for c in "/?&#\\")
        ):
            raise ValueError("uuid")
        started = head["start_time"]
        if (
            isinstance(started, bool)
            or not isinstance(started, (int, float))
            or not math.isfinite(started)
        ):
            raise ValueError("time")
        current = now or datetime.now(UTC)
        if not 0 < current.timestamp() - started < 6 * 3600:
            return None
        mode = int(head["game_config"]["meta"]["mode_id"])
        if mode not in {m - 200 for m in FILTERS}:
            return None
        players = head["players"]
        count = 3 if mode >= 21 else 4
        if len(players) != count:
            raise ValueError("players")
        accounts = [p["account_id"] for p in players]
        if any(type(a) is not int or a <= 0 for a in accounts) or len(set(accounts)) != count:
            raise ValueError("accounts")
        seats = head.get("seat_list")
        if seats:
            if set(seats) != set(accounts) or len(seats) != count:
                raise ValueError("seats")
            players = sorted(players, key=lambda p: seats.index(p["account_id"]))
        normalized = []
        for player in players:
            level = player.get("level3" if count == 3 else "level")
            normalized.append(
                {
                    "account_id": player["account_id"],
                    "nickname": str(player.get("nickname") or player["account_id"]),
                    "level": level,
                }
            )
        return LiveGame(uuid, str(mode), datetime.fromtimestamp(started, UTC), normalized)
    except (ValueError, KeyError, TypeError, OverflowError, OSError) as exc:
        raise LiveServiceError("雀魂观战数据格式异常") from exc


class BrowserSpectatorClient:
    def __init__(self, profile, *, enabled=False):
        self.profile, self.enabled = Path(profile), enabled
        self.process = None
        self.cache = QueryCache(capacity=1)
        self.lock = asyncio.Lock()
        self.last_error = None
        self.last_success = None
        self.opened = False
        self.last_failure = 0.0

    def status(self):
        if not self.enabled:
            return "雀魂观战未开启"
        if self.last_error:
            return self.last_error
        return "雀魂观战正常" if self.last_success else "雀魂观战等待登录及首次检查"

    async def _read(self):
        while True:
            line = await self.process.stdout.readline()
            if not line:
                raise LiveServiceError("雀魂观战浏览器已关闭")
            if line.startswith(b"PUPULIN_SPECTATOR "):
                return json.loads(line[len(b"PUPULIN_SPECTATOR ") :])

    async def start(self):
        async with self.lock:
            if not self.enabled or (self.process and self.process.returncode is None):
                return
            self.process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-u",
                "-m",
                "pupulinbot.spectator_worker",
                str(self.profile),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
                limit=4 * 1024 * 1024,
            )
            try:
                async with asyncio.timeout(70):
                    result = await self._read()
                if result.get("status") != "opened":
                    raise LiveServiceError("雀魂观战浏览器启动失败")
                self.opened = True
                self.last_error = None
                self.last_failure = 0.0
            except BaseException:
                self.last_error = "雀魂观战浏览器启动失败"
                await self._stop()
                raise

    async def _load(self):
        if self.last_error and time.monotonic() - self.last_failure < 20:
            raise LiveServiceError(self.last_error)
        if not self.opened:
            raise LiveServiceError("请在独立雀魂观战窗口登录")
        async with self.lock:
            try:
                if not self.process or self.process.returncode is not None:
                    raise LiveServiceError("雀魂观战浏览器已关闭")
                self.process.stdin.write((json.dumps({"filters": FILTERS}) + "\n").encode())
                await self.process.stdin.drain()
                async with asyncio.timeout(55):
                    result = await self._read()
                if result.get("error"):
                    code = result.get("code")
                    message = "请在独立雀魂观战窗口完成登录"
                    if result["error"] == "not_ready":
                        message = "雀魂观战网页正在加载，请稍后重试"
                    if result["error"] in {"unsupported_transport", "request_ids_exhausted"}:
                        message = "雀魂观战连接需要重启，请重启 Bot"
                    if result["error"] in {"rpc_timeout", "browser_error"}:
                        message = "雀魂观战连接异常，请检查观战窗口"
                    raise LiveServiceError(message + (f"（{code}）" if code else ""))
                games = {}
                for head in result["heads"]:
                    game = parse_head(head)
                    if game:
                        for player in game.players:
                            account = player["account_id"]
                            if account not in games or game.started_at > games[account].started_at:
                                games[account] = game
                self.last_success, self.last_error = datetime.now(UTC), None
                return games
            except (TimeoutError, BrokenPipeError, ConnectionError):
                await self._stop()
                self.last_error = "雀魂观战连接中断，请重启观战窗口"
                raise LiveServiceError(self.last_error) from None
            except (KeyError, TypeError, ValueError):
                self.last_error = "雀魂观战响应异常"
                self.last_failure = time.monotonic()
                raise LiveServiceError(self.last_error) from None
            except LiveServiceError as exc:
                self.last_error = str(exc)
                self.last_failure = time.monotonic()
                raise

    async def current_game(self, account_id):
        if not self.enabled:
            return None
        games = await self.cache.get("list", 30, self._load)
        return games.get(account_id)

    async def _stop(self):
        self.opened = False
        if self.process and self.process.returncode is None:
            self.process.stdin.close()
            try:
                async with asyncio.timeout(8):
                    await self.process.wait()
            except TimeoutError:
                os.killpg(self.process.pid, signal.SIGTERM)
                await self.process.wait()
        self.process = None

    async def close(self):
        await self.cache.close()
        async with self.lock:
            await self._stop()
