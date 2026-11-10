import asyncio
import logging
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from math import isfinite
from typing import Any
from urllib.parse import quote

import httpx

from .models import GameRecord, Player, PlayerStats, ms_datetime
from .paipuya_auth import PaipuyaAccess, PaipuyaAuthError
from .query_cache import QueryCache

logger = logging.getLogger(__name__)


class PaipuyaError(RuntimeError):
    pass


class PaipuyaClient:
    """Small, defensive client for the public Amae-Koromo (牌谱屋) API."""

    def __init__(
        self,
        base_url: str,
        timeout: float = 60,
        mode: str = "四麻",
        *,
        access: PaipuyaAccess | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.mode = mode
        self.access = access or PaipuyaAccess()
        self.cache = QueryCache()
        self.last_success = None
        self.last_error = None
        self._http = None

    @property
    def modes(self) -> list[int]:
        return [26, 25, 24, 23, 22, 21] if self.mode == "三麻" else [16, 15, 12, 11, 9, 8]

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            self.access.validate_target(self.base_url)
            self.access.check_cooldown()
            if self._http is None or self._http.is_closed:
                self._http = httpx.AsyncClient(
                    timeout=self.timeout,
                    limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
                )
            client = self._http
            token = self.access.token()
            for attempt in range(2):
                headers = {"Accept": "application/json"}
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                    if not self.access.using_api_key:
                        headers["X-Cap-Token"] = token
                query = dict(params or {})
                if attempt:
                    headers["Cache-Control"] = "no-cache"
                    query["cap_token_refreshed"] = int(datetime.now(UTC).timestamp() * 1000)
                response = await self._request(client, path, query, headers)
                if self.access.using_api_key and self._invalid_api_key(response):
                    if not attempt and self.access.auto_cap:
                        self.access.invalidate_key(token)
                        token = await self.access.refresh(None)
                        continue
                    raise PaipuyaError("牌谱屋 API Key 已失效，请更新密钥；当前未启用自动备用。")
                if response.status_code == 429:
                    if "x-cap-token-required" in response.text:
                        if not attempt and self.access.auto_cap and not self.access.using_api_key:
                            token = await self.access.refresh(token)
                            continue
                        self.access.cooldown(60)
                        raise PaipuyaError(
                            "牌谱屋需要有效 API Key 或 CAP Token；请配置鉴权，"
                            "或安装并启用原生 CAP 验证。"
                        )
                    retry = response.headers.get("Retry-After", "60")
                    try:
                        seconds = float(retry)
                    except ValueError:
                        try:
                            seconds = (
                                parsedate_to_datetime(retry) - datetime.now(UTC)
                            ).total_seconds()
                        except (ValueError, TypeError, OverflowError):
                            seconds = 60
                    self.access.cooldown(max(1, seconds) if isfinite(seconds) else 60)
                    raise PaipuyaError("牌谱屋请求受限，已按 Retry-After 暂停请求，请稍后重试。")
                response.raise_for_status()
                data = response.json()
                self.last_success = datetime.now(UTC)
                self.last_error = None
                return data
        except PaipuyaError as exc:
            self.last_error = str(exc)
            raise
        except PaipuyaAuthError as exc:
            self.last_error = str(exc)
            raise PaipuyaError(self.last_error) from exc
        except httpx.TimeoutException as exc:
            stage = "连接建立" if isinstance(exc, httpx.ConnectTimeout) else "响应读取"
            self.last_error = (
                f"牌谱屋{stage}超时（已自动重试；响应等待上限 {self.timeout:g} 秒/次），"
                "请稍后重试；监控会在后续轮询继续尝试。"
            )
            raise PaipuyaError(self.last_error) from exc
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            self.last_error = (
                f"牌谱屋拒绝访问（HTTP {code}），请检查 API 鉴权。"
                if code in {401, 403}
                else f"牌谱屋服务返回 HTTP {code}，请稍后重试。"
            )
            raise PaipuyaError(self.last_error) from exc
        except (httpx.HTTPError, ValueError) as exc:
            self.last_error = (
                "牌谱屋网络连接失败，请检查网络或稍后重试。"
                if isinstance(exc, httpx.HTTPError)
                else "牌谱屋返回了无法解析的数据。"
            )
            raise PaipuyaError(self.last_error) from exc

    @staticmethod
    def _invalid_api_key(response):
        if response.status_code == 401:
            return True
        if response.status_code not in {400, 403}:
            return False
        try:
            data = response.json()
        except ValueError:
            return False
        if not isinstance(data, dict):
            return False
        error = data.get("error")
        return isinstance(error, str) and error in {
            "invalid_api_key",
            "expired_api_key",
            "invalid_token",
            "token_expired",
        }

    async def _request(self, client, path, query, headers):
        for attempt in range(2):
            await self.access.wait_turn()
            started = time.monotonic()
            try:
                # Queueing for the shared QPS gate is outside the network clock.
                # Bound the entire response too, including slowly streaming bodies.
                async with asyncio.timeout(self.timeout + 30):
                    response = await client.get(
                        f"{self.base_url}/{path.lstrip('/')}",
                        params=query,
                        headers=headers,
                        timeout=httpx.Timeout(
                            self.timeout, connect=min(20, self.timeout), pool=30, write=20
                        ),
                    )
            except TimeoutError as exc:
                if attempt:
                    raise httpx.ReadTimeout("response deadline exceeded") from exc
            except (
                httpx.ConnectError,
                httpx.ConnectTimeout,
                httpx.ReadTimeout,
                httpx.ReadError,
                httpx.RemoteProtocolError,
            ) as exc:
                logger.warning(
                    "牌谱屋 %s %s，第 %s/2 次请求，耗时 %.1f 秒%s",
                    self.mode,
                    type(exc).__name__,
                    attempt + 1,
                    time.monotonic() - started,
                    "；准备限速重试" if not attempt else "",
                )
                if attempt:
                    raise
            else:
                if response.status_code not in {502, 503, 504} or attempt:
                    return response
            await asyncio.sleep(1.1)

    async def close(self):
        await self.cache.close()
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def search_player(self, nickname: str) -> list[Player]:
        data = await self._get(
            f"search_player/{quote(nickname, safe='')}", {"limit": 20, "tag": "all"}
        )
        try:
            return [Player(int(x["id"]), str(x.get("nickname", nickname))) for x in data]
        except (TypeError, KeyError, ValueError) as exc:
            raise PaipuyaError("牌谱屋玩家响应格式错误") from exc

    async def resolve(self, uid_or_name: str) -> Player:
        return await self.cache.get(
            ("resolve", uid_or_name), 300, lambda: self._resolve(uid_or_name)
        )

    async def _resolve(self, uid_or_name: str) -> Player:
        if uid_or_name.isdecimal():
            account_id = int(uid_or_name)
            records = await self.records(account_id, 1)
            if records:
                seat = records[0].seat(account_id) or {}
                return Player(account_id, str(seat.get("nickname") or uid_or_name))
            return Player(account_id, uid_or_name)
        players = await self.search_player(uid_or_name)
        if not players:
            raise PaipuyaError("没有找到该雀魂玩家（仅收录金之间及以上牌谱）")
        exact = [p for p in players if p.nickname == uid_or_name]
        candidates = exact or players
        if len(candidates) != 1:
            choices = "、".join(f"{p.nickname}（{p.account_id}）" for p in candidates[:5])
            raise PaipuyaError(f"找到多个玩家，请改用 account ID：{choices}")
        return candidates[0]

    async def records(self, account_id: int, limit: int = 10) -> list[GameRecord]:
        now = int(datetime.now(UTC).timestamp() * 1000)
        data = await self._get(
            f"player_records/{account_id}/{now}/0",
            {"limit": limit, "mode": ",".join(map(str, self.modes)), "descending": "true"},
        )
        try:
            return [self._record(x) for x in data]
        except (TypeError, KeyError, ValueError, OverflowError, OSError) as exc:
            raise PaipuyaError("牌谱屋牌谱响应格式错误") from exc

    async def stats(self, account_id: int) -> PlayerStats:
        return await self.cache.get(
            ("stats", account_id), 60, lambda: self._fetch_stats(account_id)
        )

    async def metadata(self, account_id, *, modes=None):
        """Rank/count metadata without waiting for optional hand-level statistics."""

        selected = tuple(sorted(self.modes if modes is None else modes))
        if not selected or not set(selected) <= set(self.modes):
            raise PaipuyaError("统计房间与三麻／四麻模式不匹配")

        async def load():
            now = int(datetime.now(UTC).timestamp() * 1000)
            data = await self._get(
                f"player_stats/{account_id}/0/{now}",
                {"mode": ".".join(map(str, selected))},
            )
            if not isinstance(data, dict):
                raise PaipuyaError("牌谱屋统计响应格式错误")
            try:
                return self._stats(account_id, data)
            except (TypeError, ValueError) as exc:
                raise PaipuyaError("牌谱屋统计响应格式错误") from exc

        key = ("metadata", account_id) if modes is None else ("metadata", account_id, selected)
        return await self.cache.get(key, 60, load)

    async def _fetch_stats(self, account_id: int) -> PlayerStats:
        now = int(datetime.now(UTC).timestamp() * 1000)
        params = {"mode": ".".join(map(str, self.modes))}
        data = await self._get(f"player_stats/{account_id}/0/{now}", params)
        if not isinstance(data, dict):
            raise PaipuyaError("牌谱屋统计响应格式错误")
        try:
            extended = await self._get(f"player_extended_stats/{account_id}/0/{now}", params)
        except PaipuyaError:
            extended = {}
        if isinstance(extended, dict):
            data = {**extended, **data, "_hand_count": extended.get("count")}
        try:
            return self._stats(account_id, data)
        except (TypeError, ValueError) as exc:
            raise PaipuyaError("牌谱屋统计响应格式错误") from exc

    async def range_stats(self, account_id, *, start, end, modes):
        """Statistics for a precise record window, never substituted by lifetime rates."""
        selected = tuple(sorted(modes))
        if not selected or not set(selected) <= set(self.modes):
            raise PaipuyaError("统计房间与三麻／四麻模式不匹配")
        lower, upper = int(start.timestamp() * 1000), int(end.timestamp() * 1000) - 1
        if lower > upper:
            raise PaipuyaError("统计时间范围为空")

        async def load():
            path = f"{account_id}/{lower}/{upper}"
            params = {"mode": ".".join(map(str, selected))}
            data = await self._get("player_stats/" + path, params)
            if not isinstance(data, dict):
                raise PaipuyaError("牌谱屋统计响应格式错误")
            try:
                extended = await self._get("player_extended_stats/" + path, params)
            except PaipuyaError:
                extended = {}
            # Extended count is hands, metadata count is games.
            if isinstance(extended, dict):
                data = {**extended, **data, "_hand_count": extended.get("count")}
            try:
                return self._stats(account_id, data)
            except (TypeError, ValueError) as exc:
                raise PaipuyaError("牌谱屋统计响应格式错误") from exc

        return await self.cache.get(("range_stats", account_id, lower, upper, selected), 30, load)

    async def promotion(self, game, account_id):
        from .promotion import majsoul_promotion

        mode = int(game.mode)
        if mode not in self.modes:
            return None
        stamp = int(game.started_at.timestamp() * 1000)

        async def load():
            data = await self._get(
                f"player_stats/{account_id}/{stamp}/{stamp}", {"mode": str(mode)}
            )
            return majsoul_promotion(game.seat(account_id), data)

        return await self.cache.get(("promotion", game.uuid, account_id), 3600, load)

    async def review_records(self, account_id: int, limit: int) -> list[GameRecord]:
        """Latest N four-player hanchan, with inclusive-cursor de-duplication."""
        if not 1 <= limit <= 100:
            raise PaipuyaError("复盘场数必须为 1–100")
        cursor = int(datetime.now(UTC).timestamp() * 1000)
        found = {}
        for _ in range(105):
            data = await self._get(
                f"player_records/{account_id}/{cursor}/0",
                {
                    "limit": min(100, limit - len(found) + 1),
                    "mode": "16.12.9",
                    "descending": "true",
                },
            )
            if not isinstance(data, list):
                raise PaipuyaError("牌谱屋牌谱响应格式错误")
            if not data:
                break
            try:
                games = [self._record(value) for value in data]
            except (ValueError, TypeError, KeyError, OverflowError, OSError) as exc:
                raise PaipuyaError("牌谱屋牌谱响应格式错误") from exc
            previous = len(found)
            for game in games:
                if game.mode in {"9", "12", "16"} and game.ended_at:
                    found[game.uuid] = game
            if len(found) >= limit:
                break
            oldest = int(min(g.started_at for g in games).timestamp() * 1000)
            if oldest >= cursor:
                # A boundary-only page can be followed safely one millisecond earlier.
                if len(found) == previous:
                    break
                cursor -= 1
            else:
                cursor = oldest
        return sorted(found.values(), key=lambda game: game.started_at, reverse=True)[:limit]

    async def history(
        self,
        account_id: int,
        limit: int = 200,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        modes: list[int] | None = None,
    ) -> tuple[list[GameRecord], bool]:
        """Page real records; return whether the requested sample ceiling was reached."""
        if not 1 <= limit <= 2000:
            raise PaipuyaError("历史查询场数必须为 1–2000")
        selected = modes or self.modes
        if not set(selected) <= set(self.modes):
            raise PaipuyaError("房间与三麻／四麻模式不匹配")
        upper = int((end or datetime.now(UTC)).timestamp() * 1000) - 1
        lower = int(start.timestamp() * 1000) if start else 0
        found = {}
        pages = 0
        while upper >= lower:
            pages += 1
            if pages > limit + 2:
                raise PaipuyaError("牌谱屋分页响应异常，请缩小查询范围")
            data = await self._get(
                f"player_records/{account_id}/{upper}/{lower}",
                {
                    "limit": min(100, limit + 1 - len(found)),
                    "mode": ".".join(map(str, selected)),
                    "descending": "true",
                },
            )
            if not isinstance(data, list):
                raise PaipuyaError("牌谱屋牌谱响应格式错误")
            if not data:
                break
            try:
                page = [self._record(row) for row in data]
            except (TypeError, ValueError, KeyError, OverflowError, OSError) as exc:
                raise PaipuyaError("牌谱屋牌谱响应格式错误") from exc
            for game in page:
                stamp = game.started_at.timestamp() * 1000
                if lower <= stamp <= upper and game.mode in {str(m) for m in selected}:
                    found[game.uuid] = game
            if len(found) > limit:
                return sorted(found.values(), key=lambda game: game.started_at, reverse=True)[
                    :limit
                ], True
            next_upper = int(min(game.started_at for game in page).timestamp() * 1000) - 1
            if next_upper >= upper:
                raise PaipuyaError("牌谱屋分页未前进，请稍后重试")
            upper = next_upper
        return sorted(found.values(), key=lambda game: game.started_at, reverse=True)[:limit], False

    async def query_history(self, account_id, limit=200, *, start=None, end=None, modes=None):
        if limit > 200:
            return await self.history(account_id, limit, start=start, end=end, modes=modes)
        key = ("history", account_id, limit, start, end, tuple(modes or self.modes))
        return await self.cache.get(
            key, 30, lambda: self.history(account_id, limit, start=start, end=end, modes=modes)
        )

    def _stats(self, account_id: int, data: dict[str, Any]) -> PlayerStats:
        count = int(data.get("count", data.get("games", 0)) or 0)

        def rate(*names: str) -> float | None:
            value = next((data[name] for name in names if data.get(name) is not None), None)
            return float(value) if value is not None else None

        ranks = data.get("rank_rates") or data.get("rankRates") or []
        ranks = [float(value or 0) for value in ranks] + [0.0] * 4
        total = sum(ranks)
        if total:
            ranks = [value / total for value in ranks]
        average = data.get("avg_rank", data.get("average_rank"))
        if average is None and any(ranks):
            average = sum((i + 1) * ranks[i] for i in range(4))
        return PlayerStats(
            account_id=account_id,
            mode=self.mode,
            games=count,
            first_rate=ranks[0],
            second_rate=ranks[1],
            third_rate=ranks[2],
            fourth_rate=ranks[3] if self.mode == "四麻" else 0,
            average_rank=float(average or 0),
            negative_rate=rate("negative_rate", "negativeRate") or 0,
            win_rate=rate("win_rate", "和牌率", "和了率"),
            deal_in_rate=rate("deal_in_rate", "放铳率", "放銃率"),
            riichi_rate=rate("riichi_rate", "立直率"),
            call_rate=rate("call_rate", "副露率"),
            raw=data,
        )

    @staticmethod
    def _record(item: dict[str, Any]) -> GameRecord:
        if not isinstance(item, dict):
            raise ValueError("record must be an object")
        players = [dict(p) for p in (item.get("players") or item.get("accounts") or [])]
        for player in players:
            player["account_id"] = int(player.get("account_id", player.get("accountId", -1)))
        # The native API has scores, not ranks. Equal scores favor the earlier seat.
        if players and all(p.get("score") is not None for p in players):
            order = sorted(range(len(players)), key=lambda i: (-float(players[i]["score"]), i))
            for rank, index in enumerate(order, 1):
                players[index].setdefault("rank", rank)
        started = item.get("start_time") or item.get("startTime") or item.get("start")
        ended = item.get("end_time") or item.get("endTime") or item.get("end")
        uuid = item.get("uuid") or item.get("game_uuid") or item.get("id")
        if not uuid or started is None:
            raise ValueError("missing UUID or start time")
        return GameRecord(
            uuid=str(uuid),
            started_at=ms_datetime(started),
            ended_at=ms_datetime(ended) if ended else None,
            mode=str(item.get("mode") or item.get("mode_id") or item.get("modeId") or "未知场"),
            players=players,
            raw=item,
        )
