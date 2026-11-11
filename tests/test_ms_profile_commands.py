import io
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from PIL import Image, ImageDraw
from test_ms_profile import stats as profile_stats

from pupulinbot.help_menu import help_sections
from pupulinbot.models import GameRecord, Player
from pupulinbot.ms_commands import QUERY_COMMANDS, MahjongQueries, install_commands
from pupulinbot.ms_profile import SIZE, profile_content
from pupulinbot.paipuya import PaipuyaError

ACCOUNT = 9934016
STAMP = datetime(2026, 11, 11, tzinfo=UTC)


def client_for(mode=4, room=None, count=4):
    room = room or (12 if mode == 4 else 24)
    value = profile_stats()
    value.games, value.account_id = count, ACCOUNT
    if mode == 3:
        value.mode, value.third_rate, value.fourth_rate = "三麻", 0.44, 0
        value.average_rank = 2.15
    value.raw["level"] = {"id": 10501 if mode == 4 else 20501, "score": 1000, "delta": 0}
    latest = deepcopy(value)
    latest.games = 9000
    latest.win_rate = 0.99
    latest.raw.update(level={"id": 10502 if mode == 4 else 20502, "score": 3785, "delta": 0})
    rows = [
        GameRecord(
            str(i),
            STAMP + timedelta(hours=i),
            STAMP + timedelta(hours=i, minutes=40),
            str(room),
            [{"account_id": ACCOUNT, "rank": 1, "score": 30000}],
            {},
        )
        for i in range(count)
    ]
    return SimpleNamespace(
        modes=[8, 9, 11, 12, 15, 16] if mode == 4 else [21, 22, 23, 24, 25, 26],
        resolve=AsyncMock(return_value=Player(ACCOUNT, "示例玩家")),
        history=AsyncMock(return_value=(rows, False)),
        query_history=AsyncMock(return_value=(rows, False)),
        range_stats=AsyncMock(return_value=value),
        metadata=AsyncMock(return_value=latest),
        stats=AsyncMock(return_value=latest),
    )


@pytest.mark.parametrize("mode", [3, 4])
@pytest.mark.asyncio
async def test_count_and_room_select_profile_card_with_exact_sample_not_lifetime(mode):
    client, other = client_for(mode), client_for(3 if mode == 4 else 4)
    service = MahjongQueries(None, client if mode == 4 else other, client if mode == 3 else other)
    original = deepcopy(client.range_stats.return_value.raw)
    with patch("pupulinbot.ms_commands.render_profile", return_value=b"profile") as render:
        assert await service.query(f"ms{mode}", "示例玩家 -i -r 玉 -l 100", 1, 2) == b"profile"
    client.query_history.assert_awaited_once_with(
        ACCOUNT, 100, start=None, end=None, modes=[12 if mode == 4 else 24]
    )
    selected = client.query_history.return_value[0]
    client.range_stats.assert_awaited_once_with(
        ACCOUNT,
        start=selected[0].started_at,
        end=selected[-1].started_at + timedelta(milliseconds=1),
        modes=[12 if mode == 4 else 24],
    )
    rendered = render.call_args.args[1]
    assert rendered.games == 4 and rendered.win_rate == 0.223
    assert rendered.raw["level"] == client.metadata.return_value.raw["level"]
    assert client.range_stats.return_value.raw == original  # No cached-object mutation.
    assert (
        render.call_args.kwargs["scope"] == f"{'三麻' if mode == 3 else '四麻'} · 玉南 · 最近 4 场"
    )
    assert render.call_args.kwargs["games"] == selected
    client.stats.assert_not_awaited()
    other.resolve.assert_not_awaited()


@pytest.mark.parametrize("mode", [3, 4])
@pytest.mark.asyncio
async def test_room_only_filters_all_recorded_games_without_loading_history(mode):
    room = 22 if mode == 3 else 9
    client = client_for(mode, room)
    service = MahjongQueries(None, client, client)
    with patch("pupulinbot.ms_commands.render_profile", return_value=b"profile") as render:
        assert await service.query(f"ms{mode}", "示例玩家 -i -r 金", 1, 2) == b"profile"
    args = client.range_stats.call_args.kwargs
    assert args["modes"] == [room] and args["start"].timestamp() == 0
    assert args["end"] <= datetime.now(UTC)
    assert "金南 · 已收录 4 场" in render.call_args.kwargs["scope"]
    client.query_history.assert_not_awaited()
    client.history.assert_not_awaited()
    client.stats.assert_not_awaited()


@pytest.mark.asyncio
async def test_date_filters_reach_range_api_without_history_limit_and_text_stays_profile():
    client = client_for()
    service = MahjongQueries(None, client, client)
    with patch("pupulinbot.ms_commands.format_stats", return_value="profile stats"):
        text = await service.query("ms4", "示例玩家 -r 王座 start=2026-11-01 end=2026-11-05", 1, 2)
    assert text.startswith("profile stats") and "所选日期 4 场" in text
    args = client.range_stats.call_args.kwargs
    assert args["modes"] == [16]
    assert args["start"] == datetime(2026, 10, 31, 16, tzinfo=UTC)
    assert args["end"] == datetime(2026, 11, 5, 16, tzinfo=UTC)
    client.query_history.assert_not_awaited()


@pytest.mark.parametrize("mode", [3, 4])
@pytest.mark.asyncio
async def test_unfiltered_profile_keeps_full_mode_stats_and_existing_image_switch(mode):
    client = client_for(mode)
    with patch("pupulinbot.ms_commands.render_profile", return_value=b"profile") as render:
        assert (
            await MahjongQueries(None, client, client).query(f"ms{mode}", "示例玩家 -i", 1, 2)
            == b"profile"
        )
    render.assert_called_once_with(Player(ACCOUNT, "示例玩家"), client.stats.return_value)
    client.stats.assert_awaited_once_with(ACCOUNT)
    client.range_stats.assert_not_awaited()
    client.query_history.assert_not_awaited()


@pytest.mark.parametrize("kind", ["missing", "mismatch", "duplicate", "wrong_room", "empty"])
@pytest.mark.asyncio
async def test_bad_sample_never_falls_back_to_lifetime_profile(kind):
    client = client_for()
    if kind == "missing":
        client.range_stats.side_effect = PaipuyaError("unavailable")
    elif kind == "mismatch":
        client.range_stats.return_value.games = 999
    elif kind == "duplicate":
        client.query_history.return_value[0][-1].uuid = "0"
    elif kind == "wrong_room":
        client.query_history.return_value[0][-1].mode = "16"
    else:
        client.query_history.return_value = ([], False)
    with patch("pupulinbot.ms_commands.render_profile") as render:
        if kind == "empty":
            assert "暂无已结算" in await MahjongQueries(None, client, client).query(
                "ms4", "示例玩家 -i -l 100 -r 玉", 1, 2
            )
        else:
            with pytest.raises(PaipuyaError):
                await MahjongQueries(None, client, client).query(
                    "ms4", "示例玩家 -i -l 100 -r 玉", 1, 2
                )
    render.assert_not_called()
    client.stats.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_latest_identity_does_not_fill_rates_or_claim_old_sample_rank_is_current():
    client = client_for()
    client.metadata.side_effect = PaipuyaError("identity unavailable")
    with patch("pupulinbot.ms_commands.render_profile", return_value=b"profile") as render:
        await MahjongQueries(None, client, client).query("ms4", "示例玩家 -i -l 100 -r 玉", 1, 2)
    value = render.call_args.args[1]
    assert value.games == 4 and value.win_rate == 0.223
    assert value.raw["level"] is None and value.raw["max_level"] is None


@pytest.mark.parametrize("mode", [3, 4])
def test_filtered_profile_scope_and_three_four_player_metrics_render_cleanly(monkeypatch, mode):
    from pupulinbot.ms_charts import render_profile

    value = client_for(mode).range_stats.return_value
    scope = f"{'三麻' if mode == 3 else '四麻'} · 玉南 · 最近 4 场"
    drawn = []
    original = ImageDraw.ImageDraw.text

    def capture(self, xy, text, *args, **kwargs):
        drawn.append(str(text))
        box = self.textbbox(xy, text, font=kwargs["font"], anchor=kwargs.get("anchor"))
        assert 0 <= box[0] <= box[2] <= SIZE[0] and 0 <= box[1] <= box[3] <= SIZE[1]
        return original(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture)
    png = render_profile(Player(ACCOUNT, "示例玩家"), value, scope=scope)
    assert Image.open(io.BytesIO(png)).size == SIZE
    assert scope in drawn
    assert len(profile_content(Player(ACCOUNT, "示例玩家"), value)["ranks"]) == mode


@pytest.mark.asyncio
async def test_removed_msdetail_has_no_handler_or_lookup_side_effect():
    command = "msdetail"
    db = SimpleNamespace(binding=AsyncMock(), bind=AsyncMock())
    client = client_for()
    assert command not in QUERY_COMMANDS
    with pytest.raises(ValueError, match="不支持"):
        await MahjongQueries(db, client, client).query(command, "示例玩家", 1, 2)
    db.binding.assert_not_awaited()
    db.bind.assert_not_awaited()
    client.resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_ms2_allows_query_and_authorized_binding_without_overwriting(tmp_path):
    from pupulinbot.db import Database

    db = Database(tmp_path / "bindings.db")
    await db.initialize()
    client = client_for()
    nickname = "。不记仇的钟会"
    client.resolve.return_value = Player(ACCOUNT, nickname)
    service = MahjongQueries(db, client, client, admins={"345678"})
    target = "234567"

    assert "尚未绑定" in await service.query("ms2", "", "123456", "706530575", mentioned=target)
    for options in ("-i", "-o", "mode=3", f"{nickname} -l 10"):
        with pytest.raises(ValueError, match="用法"):
            await service.query(
                "ms2", options, "123456", "706530575", role="admin", mentioned=target
            )

    with pytest.raises(ValueError, match="群管理员"):
        await service.query("ms2", nickname, "123456", "706530575", mentioned=target)
    assert await db.binding(target) is None
    client.resolve.assert_not_awaited()

    assert "绑定成功" in await service.query(
        "ms2", nickname, "123456", "706530575", role="admin", mentioned=target
    )
    client.resolve.assert_awaited_once_with(nickname)
    assert "已绑定" in await service.query(
        "ms2", "不同昵称", "345678", "706530575", mentioned=target
    )
    assert (await db.binding(target))["nickname"] == nickname
    client.resolve.assert_awaited_once()

    result = await service.query("ms2", "", "123456", "706530575", mentioned=target)
    assert result == f"雀魂账号：{nickname}（{ACCOUNT}）"
    client.stats.assert_not_awaited()


def test_registration_and_help_only_advertise_retained_commands(monkeypatch):
    registered = []

    class Matcher:
        def handle(self):
            return lambda f: f

    def register(name, **kwargs):
        registered.append(name)
        return Matcher()

    monkeypatch.setattr("pupulinbot.ms_commands.on_command", register)
    install_commands(None)
    text = "\n".join(body for _, body in help_sections())
    assert set(registered) == set(QUERY_COMMANDS)
    assert "ms2" in registered and "msdetail" not in registered
    assert "/ms2 @群友" in text and "/msdetail" not in text
    assert "-i -r 玉南 -l 100" in text
