from datetime import UTC, datetime

import pytest

from pupulinbot.db import Database
from pupulinbot.formatting import format_records
from pupulinbot.models import GameRecord
from pupulinbot.paipuya import PaipuyaClient
from pupulinbot.review import ReviewResult, extract_uuid, render_review


def game(ended=True):
    return GameRecord(
        "240101-test",
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 1, 1, tzinfo=UTC) if ended else None,
        "四人南",
        [{"account_id": 42, "rank": 1, "score": 42000}],
        {"uuid": "240101-test"},
    )


def test_extract_uuid():
    assert extract_uuid("https://game.maj-soul.com/1/?paipu=abc_def&lang=zh") == "abc_def"
    assert extract_uuid("abc") == "abc"
    with pytest.raises(ValueError):
        extract_uuid("")


def test_record_parser_and_format():
    item = {
        "uuid": "x",
        "startTime": 1_700_000_000_000,
        "endTime": 1_700_000_100_000,
        "mode": "玉之间",
        "players": [{"account_id": 42, "rank": 2, "score": 28000}],
    }
    parsed = PaipuyaClient._record(item)
    assert parsed.uuid == "x"
    assert "28000" in format_records([parsed], 42)


@pytest.mark.asyncio
async def test_database_bind_subscribe_and_deduplicate(tmp_path):
    db = Database(tmp_path / "test.db")
    await db.initialize()
    await db.bind("100", 42, "雀士")
    assert (await db.binding("100"))["account_id"] == 42
    await db.subscribe("200", "100")
    assert len(await db.subscriptions()) == 1
    assert await db.claim_game_event(game(), 42, True)
    assert not await db.claim_game_event(game(), 42, True)


def test_render_png():
    data = render_review(ReviewResult(87.5, "A", None, {}), "player", "uuid")
    assert data.startswith(b"\x89PNG")
