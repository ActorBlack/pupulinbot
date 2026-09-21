from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


def ms_datetime(value: int | float | None) -> datetime:
    value = value or 0
    if value > 10_000_000_000:
        value /= 1000
    return datetime.fromtimestamp(value, UTC)


@dataclass(slots=True)
class Player:
    account_id: int
    nickname: str


@dataclass(slots=True)
class GameRecord:
    uuid: str
    started_at: datetime
    ended_at: datetime | None
    mode: str
    players: list[dict[str, Any]]
    raw: dict[str, Any]

    @property
    def url(self) -> str:
        return f"https://game.maj-soul.com/1/?paipu={self.uuid}"

    def seat(self, account_id: int) -> dict[str, Any] | None:
        return next((p for p in self.players if int(p.get("account_id", -1)) == account_id), None)


@dataclass(slots=True)
class PlayerStats:
    account_id: int
    mode: str
    games: int
    first_rate: float
    second_rate: float
    third_rate: float
    fourth_rate: float
    average_rank: float
    negative_rate: float
    win_rate: float
    deal_in_rate: float
    riichi_rate: float
    call_rate: float
    raw: dict[str, Any]
