import copy
import io
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image, ImageDraw

from pupulinbot.db import Database
from pupulinbot.tenhou import CN, TenhouClient, TenhouError
from pupulinbot.tenhou_monitor import TenhouStore
from pupulinbot.tenhou_options import parse_tenhou_options
from pupulinbot.tenhou_queries import TenhouQueries, make_view, summary_text
from pupulinbot.tenhou_query_card import SIZE, render_life, render_profile, render_recent

FIXTURES = Path(__file__).parent / "fixtures/tenhou"
NOW = datetime(2026, 11, 10, 20, tzinfo=CN)


def history(name="applePie"):
    original = json.loads((FIXTURES / "result.json").read_text())
    rows = []
    for i, (mode, room, length) in enumerate([(4, 3, 2), (4, 2, 1), (3, 3, 2), (4, 3, 2)]):
        row = copy.deepcopy(original)
        row.update(
            starttime=NOW.timestamp() - (4 - i) * 3600,
            playernum=mode,
            playerlevel=room,
            playlength=length,
            rate=2000 + i,
            seq=i + 1,
        )
        row["url"] = row["url"].replace("f946a576", f"f946a57{i}")
        row["player1"] = name
        rows.append(row)
    return {"name": name, "list": rows, "rate": {"4": 2100, "3": 1900}}


@pytest.fixture
async def store(tmp_path):
    db = Database(tmp_path / "bot.sqlite")
    await db.initialize()
    store = TenhouStore(db)
    await store.initialize()
    return store


def client(name="applePie"):
    return SimpleNamespace(
        user_history=AsyncMock(return_value=history(name)),
        phoenix_percentiles=AsyncMock(return_value={}),
        phoenix_stats=AsyncMock(
            return_value=json.loads((FIXTURES / "profile.json").read_text())["s4"]
        ),
    )


def test_short_options_rooms_dates_zero_boundaries_and_quoted_names():
    opts = parse_tenhou_options('"Nick Name" -l 0 -r 凤凰东 mode=3 start=2026-11-01 end=2026-11-10')
    assert opts.name == "Nick Name" and opts.last == 500 and opts.mode == 3
    assert (opts.room, opts.length) == (3, 1)
    assert opts.end == datetime(2026, 11, 11, tzinfo=CN).timestamp()
    assert parse_tenhou_options("-l 500 -r 特上").last == 500
    assert parse_tenhou_options("-r 特上").room == 2
    assert parse_tenhou_options("game=东 -r 特东").length == 1
    month = parse_tenhou_options("-m", now=NOW)
    assert month.start == datetime(2026, 11, 1, tzinfo=CN).timestamp()
    assert month.end == NOW.timestamp()


@pytest.mark.parametrize(
    "text",
    [
        "-l 501",
        "-l -1",
        "-l x",
        "-l",
        "-r",
        "-r 玉",
        "mode=2",
        "-x",
        "-r 凤 game=东",
        "game=东 -r 凤",
        "-m -y",
        "-m start=2026-11-01",
        "start=2026-11-12 end=2026-11-01",
        "end=oops",
        "foo=1",
        "-l 5 last=10",
    ],
)
def test_invalid_options_fail_before_fetch(text):
    with pytest.raises(TenhouError):
        parse_tenhou_options(text)


def test_recent_default_room_one_mode_and_correct_stat_scope():
    data = history()
    view = make_view(
        data, "applePie", parse_tenhou_options("-l 100"), recent=True, now=NOW.timestamp()
    )
    assert view["count"] == 2 and view["scope"] == "鳳南"
    assert view["counts"] == [2, 0, 0, 0] and view["average"] == 1
    assert view["activity"] == [2, 2, 2]
    assert view["phoenix_scope"] == "四鳳南"
    assert view["average_result"] == 59.2 and view["negative"] == 0
    text = summary_text(view)
    assert "连对率: 100.0%" in text and "击飞率: 0.0%" in text
    assert "和/铳" not in text  # No aggregate hand metrics disguised as recent-N data.
    assert "https://" not in text
    east = make_view(data, "applePie", parse_tenhou_options("-r 特东"), recent=True)
    assert east["count"] == 1 and east["scope"] == "特东"
    sanma = make_view(data, "applePie", parse_tenhou_options("mode=3"), recent=True)
    assert sanma["count"] == 1 and len(sanma["counts"]) == 3
    assert sanma["negative"] is None and not sanma["tag"]  # No invented 3p final scores.


def test_full_profile_and_date_filter_dont_mix_latest_room_or_modes():
    data = history()
    view = make_view(data, "applePie", parse_tenhou_options(""), recent=False)
    assert view["count"] == 3 and view["scope"] == "全部房间"
    opts = parse_tenhou_options("start=2026-11-11")
    with pytest.raises(TenhouError, match="没有已收录"):
        make_view(data, "applePie", opts, recent=True)
    with pytest.raises(TenhouError, match="没有已收录"):
        make_view(data, "applePie", parse_tenhou_options("-r 般"), recent=True)


@pytest.mark.asyncio
async def test_binding_migrates_persists_and_never_changes_ms_binding_or_monitors(store):
    await store.db.bind("user", 42, "雀魂昵称")
    await store.add("group", "manager", "applePie")
    assert await store.bind("user", "applePie")
    assert not await store.bind("user", "other", if_absent=True)
    restored = TenhouStore(store.db)
    await restored.initialize()
    assert await restored.binding("user") == "applePie"
    assert (await store.db.binding("user"))["nickname"] == "雀魂昵称"
    assert await restored.unbind("user")
    assert not await restored.unbind("user")
    assert len(await restored.subscriptions("group")) == 1
    assert (await store.db.binding("user"))["account_id"] == 42


@pytest.mark.asyncio
async def test_self_binding_default_query_profile_and_missing_account(store):
    source = client()
    service = TenhouQueries(store, source, manager="manager")
    with pytest.raises(TenhouError, match="thbind"):
        await service.query("th", "", "user", "group")
    assert await service.query("thbind", "applePie", "user", "group") == "绑定成功"
    assert "applePie（" in await service.query("th", "-l 100", "user", "group")
    source.phoenix_stats.assert_not_awaited()
    profile = await service.query("th4", "", "user", "group")
    assert "凤凰桌统计　四鳳东/南（839 战）" in profile
    source.phoenix_stats.assert_awaited_once_with("applePie", 4, None)
    assert "安定段:" in profile
    assert await service.query("thunbind", "", "user", "group") == "已解除绑定"
    assert await store.binding("user") is None
    source.user_history.return_value = {"name": "empty", "list": []}
    with pytest.raises(TenhouError, match="未找到"):
        await service.query("thbind", "empty", "user", "group")
    assert await store.binding("user") is None


@pytest.mark.asyncio
async def test_binding_other_member_requires_authority_and_never_overwrites(store):
    source = client()
    service = TenhouQueries(store, source, manager="manager")
    with pytest.raises(TenhouError, match="代绑"):
        await service.query("th2", "applePie", "ordinary", "group", mentioned="friend")
    source.user_history.assert_not_awaited()
    for user, role in (("manager", "member"), ("admin", "admin"), ("owner", "owner")):
        assert (
            await service.query(
                "th2", "applePie", user, "group", role=role, mentioned=user + "friend"
            )
            == "绑定成功"
        )
    source.user_history.reset_mock()
    assert (
        await service.query("th2", "other", "manager", "group", mentioned="managerfriend")
        == "该群友已绑定"
    )
    source.user_history.assert_not_awaited()
    assert await store.binding("managerfriend") == "applePie"
    assert "applePie（" in await service.query(
        "th2", "", "ordinary", "group", mentioned="managerfriend"
    )


@pytest.mark.asyncio
async def test_last_compatibility_limits_default_binding_and_group_isolation(store):
    source = client()
    service = TenhouQueries(store, source)
    await store.add("group", "manager", "applePie")
    result = await service.query("thlast", "", "user", "group")
    assert "applePie(49200)" in result and "https://tenhou.net/4/?log=" in result
    with pytest.raises(TenhouError, match="thbind"):
        await service.query("thlast", "", "user", "othergroup")
    source.user_history.reset_mock()
    with pytest.raises(TenhouError, match="最多"):
        await service.query("thlast", "applePie -l 21 -i", "user", "group")
    source.user_history.assert_not_awaited()
    result = await service.query("thlast", "applePie -l 5 -i", "user", "group")
    assert result.startswith(b"\x89PNG")


@pytest.mark.asyncio
async def test_sanma_and_scope_specific_hand_stats_and_optional_failure(store):
    source = client()
    service = TenhouQueries(store, source)
    await service.query("th3", "applePie -l 100", "user", "group")
    source.phoenix_stats.assert_awaited_once_with("applePie", 3, 2)
    source.phoenix_stats.side_effect = TenhouError("offline")
    text = await service.query("th4", "applePie", "user", "group")
    assert "凤凰桌统计：暂时不可用" in text and "最近战绩" in text
    with pytest.raises(TenhouError, match="仅查询"):
        await service.query("th3", "applePie mode=4", "user", "group")


@pytest.mark.asyncio
async def test_group_rank_uses_only_current_group_watch_names(store):
    await store.add("group", "manager", "applePie")
    await store.add("other", "manager", "other")
    source = client()
    service = TenhouQueries(store, source)
    result = await service.query("thrank", "", "user", "group")
    assert "applePie " in result and "other" not in result
    source.user_history.assert_awaited_once_with("applePie")
    source.phoenix_stats.assert_not_awaited()
    assert await service.query("thrank", "", "user", "emptygroup") == "本群天凤监控名单为空。"


@pytest.mark.asyncio
async def test_public_client_endpoints_identity_scope_cache_and_no_credentials():
    seen = []

    async def respond(request):
        seen.append(request)
        assert "authorization" not in request.headers and "x-cap-token" not in request.headers
        if request.url.path == "/api/listuser.php":
            assert request.url.params["name"] == "applePie"
            return httpx.Response(200, json=history())
        assert request.url.path == "/api/phoenix_status.php"
        assert request.url.params["username"] == "applePie"
        return httpx.Response(200, json={"s4": {"agariC": 0.2}, "s3_2": {"agariC": 0.3}})

    source = TenhouClient(httpx.MockTransport(respond))
    try:
        assert (await source.user_history("applePie"))["name"] == "applePie"
        assert (await source.phoenix_stats("applePie", 3, 2))["agariC"] == 0.3
        assert (await source.phoenix_stats("applePie", 4))["agariC"] == 0.2
        assert len(seen) == 2
    finally:
        await source.close()


@pytest.mark.parametrize("mode", [3, 4])
def test_real_rendering_text_bounds_modes_missing_data_and_separate_phoenix_scope(
    monkeypatch, mode
):
    drawn = []
    original = ImageDraw.ImageDraw.text

    def capture(self, xy, text, *args, **kwargs):
        box = self.textbbox(xy, text, font=kwargs["font"], anchor=kwargs.get("anchor"))
        width, height = self._image.size
        assert 0 <= box[0] <= box[2] <= width, (text, box)
        assert 0 <= box[1] <= box[3] <= height, (text, box)
        drawn.append(str(text))
        return original(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture)
    data = history()
    opts = parse_tenhou_options(f"mode={mode}")
    view = make_view(data, "applePie", opts, recent=False)
    png = render_profile(view)
    assert Image.open(io.BytesIO(png)).size == SIZE
    assert "连对率" in drawn and "击飞率" in drawn
    assert drawn.index("防守与流局") < drawn.index("立直与副露")
    assert not any(t.startswith("#") for t in drawn)
    if mode == 3:
        assert "4位" not in drawn
    for text in ("-l 1", "-l 20"):
        selected = make_view(data, "applePie", parse_tenhou_options(text, mode=mode), recent=True)
        assert render_recent("applePie", selected["rows"], mode).startswith(b"\x89PNG")
    assert render_life(view).startswith(b"\x89PNG")
