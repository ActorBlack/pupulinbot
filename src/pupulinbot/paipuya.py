from datetime import UTC, datetime
from typing import Any

import httpx

from .models import GameRecord, Player, PlayerStats, ms_datetime


class PaipuyaError(RuntimeError):
    pass


class PaipuyaClient:
    """Small, defensive client for the public Amae-Koromo (牌谱屋) API."""

    def __init__(self, base_url: str, timeout: float = 15, mode: str = "四麻"):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.mode = mode

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(f"{self.base_url}/{path.lstrip('/')}", params=params)
                response.raise_for_status()
                return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PaipuyaError(f"牌谱屋请求失败：{exc}") from exc

    async def search_player(self, nickname: str) -> list[Player]:
        data = await self._get(f"search_player/{nickname}", {"limit": 20, "tag": "all"})
        return [Player(int(x["id"]), str(x.get("nickname", nickname))) for x in data]

    async def resolve(self, uid_or_name: str) -> Player:
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
        exact = next((p for p in players if p.nickname == uid_or_name), None)
        return exact or players[0]

    async def records(self, account_id: int, limit: int = 10) -> list[GameRecord]:
        now = int(datetime.now(UTC).timestamp() * 1000)
        data = await self._get(
            f"player_records/{account_id}/{now}/0", {"limit": limit, "mode": "16,15,12,11,9,8"}
        )
        return [self._record(x) for x in data]

    async def stats(self, account_id: int) -> PlayerStats:
        now = int(datetime.now(UTC).timestamp() * 1000)
        params = {"mode": "16,15,12,11,9,8"}
        data = await self._get(f"player_stats/{account_id}/{now}/0", params)
        try:
            extended = await self._get(f"player_extended_stats/{account_id}/{now}/0", params)
        except PaipuyaError:
            extended = {}
        if isinstance(extended, dict):
            data = {**extended, **data}
        count = int(data.get("count", data.get("games", 0)) or 0)

        def rate(name: str, fallback: str = "") -> float:
            value = data.get(name, data.get(fallback, 0))
            return float(value or 0)

        ranks = data.get("rank_rates") or data.get("rankRates") or []
        ranks = [float(value or 0) for value in ranks] + [0.0] * 4
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
            negative_rate=rate("negative_rate", "negativeRate"),
            win_rate=rate("win_rate", "和了率"),
            deal_in_rate=rate("deal_in_rate", "放銃率"),
            riichi_rate=rate("riichi_rate", "立直率"),
            call_rate=rate("call_rate", "副露率"),
            raw=data,
        )

    @staticmethod
    def _record(item: dict[str, Any]) -> GameRecord:
        players = item.get("players") or item.get("accounts") or []
        started = item.get("start_time") or item.get("startTime") or item.get("start")
        ended = item.get("end_time") or item.get("endTime") or item.get("end")
        return GameRecord(
            uuid=str(item.get("uuid") or item.get("game_uuid") or item.get("id")),
            started_at=ms_datetime(started),
            ended_at=ms_datetime(ended) if ended else None,
            mode=str(item.get("mode") or item.get("mode_id") or "未知场"),
            players=players,
            raw=item,
        )
