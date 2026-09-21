from datetime import UTC, datetime
from typing import Any

import httpx

from .models import GameRecord, Player, ms_datetime


class PaipuyaError(RuntimeError):
    pass


class PaipuyaClient:
    """Small, defensive client for the public Amae-Koromo (牌谱屋) API."""

    def __init__(self, base_url: str, timeout: float = 15):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

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
            # 牌谱屋的 account_id 即社区通常所称 UID；用查询结果尽量补全昵称。
            return Player(int(uid_or_name), uid_or_name)
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
