"""Read-only Majsoul queries through an isolated official browser session."""

import asyncio
import json
import math
import os
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from websockets.exceptions import WebSocketException

from .live import LiveGame, LiveServiceError
from .majsoul_observe import decode_observation
from .majsoul_replay import (
    LobbyKeepalive,
    MajsoulConnectionLost,
    ReplayBrowserUnavailable,
    chrome_browser,
    chrome_page,
    fetch_replay_head,
    official_tab,
    request_observation,
)
from .query_cache import QueryCache

FILTERS = (208, 209, 211, 212, 215, 216, 221, 222, 223, 224, 225, 226)
PUBLIC_INDEX_SECONDS = 90 * 60


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
    def __init__(self, profile, *, enabled=False, records_enabled=False):
        self.profile, self.enabled = Path(profile), enabled
        self.records_enabled = records_enabled
        self.process = None
        self.cache = QueryCache(capacity=1)
        self.recent_public = {}
        self.lock = asyncio.Lock()
        self.record_lock = asyncio.Lock()
        self.record_lobby = LobbyKeepalive(self.profile)
        self.last_error = None
        self.last_success = None
        self.opened = False
        self.last_failure = 0.0
        self.record_session_retry_at = 0.0

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
            if not (self.enabled or self.records_enabled) or (
                self.process and self.process.returncode is None
            ):
                return
            command = [sys.executable, "-u", "-m", "pupulinbot.spectator_worker", str(self.profile)]
            if not self.enabled:
                command.append("--records-only")
            if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
                command = ["xvfb-run", "-a", *command]
            self.process = await asyncio.create_subprocess_exec(
                *command,
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

    async def _request(self, payload, wait_seconds):
        if not self.process or self.process.returncode is not None:
            raise LiveServiceError("雀魂浏览器已关闭")
        try:
            async with asyncio.timeout(wait_seconds):
                self.process.stdin.write((json.dumps(payload) + "\n").encode())
                await self.process.stdin.drain()
                response = await self._read()
                if not isinstance(response, dict):
                    raise ValueError("invalid browser response")
                return response
        except (asyncio.CancelledError, TimeoutError, BrokenPipeError, ConnectionError):
            await self._stop()
            raise

    async def record_head(self, url, uuid):
        if not self.records_enabled or time.monotonic() < self.record_session_retry_at:
            return None
        try:
            async with asyncio.timeout(165), self.record_lock:
                if time.monotonic() < self.record_session_retry_at:
                    return None
                # The replay replaces the lobby before signing in.
                await self.record_lobby.close()
                try:
                    return await fetch_replay_head(self.profile, url, uuid)
                except ReplayBrowserUnavailable:
                    await self.start()
                    return await fetch_replay_head(self.profile, url, uuid)
                except MajsoulConnectionLost:
                    # Reloading the replay makes the official client reconnect.
                    return await fetch_replay_head(self.profile, url, uuid)
        except (
            ReplayBrowserUnavailable,
            MajsoulConnectionLost,
            LiveServiceError,
            OSError,
            TimeoutError,
            ValueError,
        ):
            self.record_session_retry_at = time.monotonic() + 30
            return None

    async def keep_record_session(self):
        if not self.records_enabled:
            return False
        async with self.record_lock:
            try:
                return await self.record_lobby.click()
            except ReplayBrowserUnavailable:
                # The next record lookup can launch Chrome if it was closed.
                return False

    async def observation(self, uuid):
        if not (self.enabled or self.records_enabled):
            raise LiveServiceError("雀魂官方观战未配置。")
        try:
            async with asyncio.timeout(140), self.record_lock:
                if self.enabled:
                    if not self.opened:
                        await self.start()
                    async with self.lock, chrome_browser(self.profile) as (browser, origin):
                        target = await official_tab(browser)
                        async with chrome_page(origin, target) as page:
                            result = await request_observation(page, uuid)
                else:
                    try:
                        result = await self.record_lobby.observation(uuid)
                    except ReplayBrowserUnavailable:
                        await self.start()
                        result = await self.record_lobby.observation(uuid)
                return decode_observation(result)
        except (
            ReplayBrowserUnavailable,
            MajsoulConnectionLost,
            OSError,
            TimeoutError,
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            WebSocketException,
        ) as exc:
            raise LiveServiceError("雀魂观战连接暂不可用，请检查专用浏览器登录。") from exc

    async def _load(self):
        if self.last_error and time.monotonic() - self.last_failure < 20:
            raise LiveServiceError(self.last_error)
        if self.enabled and not self.opened:
            raise LiveServiceError("请在独立雀魂观战窗口登录")
        try:
            if self.enabled:
                async with self.lock:
                    result = await self._request({"filters": FILTERS}, 55)
            else:
                async with asyncio.timeout(220), self.record_lock:
                    try:
                        result = await self.record_lobby.public_heads(FILTERS)
                    except ReplayBrowserUnavailable:
                        await self.start()
                        result = await self.record_lobby.public_heads(FILTERS)
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
            now = time.monotonic()
            self.recent_public = {
                account: entry for account, entry in self.recent_public.items() if entry[0] > now
            }
            self.recent_public.update(
                (account, (now + PUBLIC_INDEX_SECONDS, game)) for account, game in games.items()
            )
            self.last_success, self.last_error = datetime.now(UTC), None
            return games
        except (
            ReplayBrowserUnavailable,
            MajsoulConnectionLost,
            OSError,
            WebSocketException,
            TimeoutError,
        ) as exc:
            self.last_error = "雀魂观战连接超时或中断，请检查专用浏览器登录后重试"
            self.last_failure = time.monotonic()
            raise LiveServiceError(self.last_error) from exc
        except (KeyError, TypeError, ValueError) as exc:
            self.last_error = "雀魂观战响应异常"
            self.last_failure = time.monotonic()
            raise LiveServiceError(self.last_error) from exc
        except LiveServiceError as exc:
            self.last_error = str(exc)
            self.last_failure = time.monotonic()
            raise

    async def public_games(self):
        if not (self.enabled or self.records_enabled):
            raise LiveServiceError("雀魂官方观战未配置。")
        return await self.cache.get("list", 30, self._load)

    async def current_game(self, account_id):
        if not self.enabled:
            return None
        games = await self.public_games()
        return games.get(account_id)

    def recently_listed_games(self):
        now = time.monotonic()
        return {
            account: game for account, (until, game) in self.recent_public.items() if until > now
        }

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
        await self.record_lobby.close()
        async with self.lock:
            await self._stop()
