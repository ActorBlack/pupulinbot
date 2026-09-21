from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx


class LiveServiceError(RuntimeError):
    pass


@dataclass(slots=True)
class LiveGame:
    uuid: str
    mode: str
    started_at: datetime
    players: list[dict[str, Any]]

    @property
    def url(self) -> str:
        return f"https://game.maj-soul.com/1/?paipu={self.uuid}"


class LiveServiceClient:
    """Client for an optional Mahjong Soul gateway with live presence support."""

    def __init__(self, url: str | None, token: str | None, timeout: float = 10):
        self.url, self.token, self.timeout = url, token, timeout

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    async def current_game(self, account_id: int) -> LiveGame | None:
        if not self.url:
            return None
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    f"{self.url.rstrip('/')}/players/{account_id}/current-game", headers=headers
                )
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise LiveServiceError(f"实时网关请求失败：{exc}") from exc
        if not data or data.get("playing") is False:
            return None
        started = data.get("started_at") or data.get("start_time") or 0
        if isinstance(started, str):
            started_at = datetime.fromisoformat(started.replace("Z", "+00:00"))
        else:
            if started > 10_000_000_000:
                started /= 1000
            started_at = datetime.fromtimestamp(started, UTC)
        return LiveGame(
            uuid=str(data["uuid"]),
            mode=str(data.get("mode", "未知场")),
            started_at=started_at,
            players=list(data.get("players") or []),
        )
