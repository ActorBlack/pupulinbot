import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image, ImageDraw

from pupulinbot.ms_rank import group_ranking, ranking_rows
from pupulinbot.paipuya import PaipuyaError


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,base", [("四麻", 10000), ("三麻", 20000)])
async def test_standings_use_rank_then_current_points(mode, base):
    levels = [
        ("y024", 503, 8500, 0, 1.5, 9999),
        ("同段低分", 701, 500, 25, 1.6, 9998),
        ("同段高分", 701, 700, -25, 2.9, 1),
        ("高段低分", 702, 100, 0, 2.8, 0),
    ]
    bindings = [{"account_id": i, "nickname": v[0]} for i, v in enumerate(levels)]
    data = {
        i: SimpleNamespace(
            raw={"level": {"id": base + code, "score": score, "delta": delta}},
            average_rank=average,
            games=games,
        )
        for i, (_, code, score, delta, average, games) in enumerate(levels)
    }
    client = SimpleNamespace(mode=mode, metadata=AsyncMock(side_effect=lambda i: data[i]))
    assert await ranking_rows(client, bindings) == [
        ("高段低分", "魂天Lv2", "1.0", "20.0"),
        ("同段高分", "魂天Lv1", "6.8", "20.0"),
        ("同段低分", "魂天Lv1", "5.3", "20.0"),
        ("y024", "圣3", "8500", "9000"),
    ]
    assert client.metadata.await_count == 4


@pytest.mark.asyncio
async def test_standings_apply_promotion_and_skip_unavailable_rank():
    bindings = [
        {"account_id": i, "nickname": name}
        for i, name in enumerate(("晋级", "原段", "缺失", "失败"))
    ]
    client = SimpleNamespace(
        mode="四麻",
        metadata=AsyncMock(
            side_effect=[
                SimpleNamespace(raw={"level": {"id": 10502, "score": 5990, "delta": 30}}),
                SimpleNamespace(raw={"level": {"id": 10502, "score": 5900, "delta": 0}}),
                SimpleNamespace(raw={}),
                PaipuyaError("offline"),
            ]
        ),
    )
    assert await ranking_rows(client, bindings) == [
        ("晋级", "圣3", "4500", "9000"),
        ("原段", "圣2", "5900", "6000"),
    ]
    client.metadata = AsyncMock(return_value=SimpleNamespace(raw={}))
    assert await group_ranking(client, bindings) == "牌谱屋暂时没有本群玩家的段位数据。"


@pytest.mark.asyncio
async def test_rank_reply_renders_fixed_columns_for_real_mixed_width_names(monkeypatch):
    standings = [
        ("ott桃", "圣1", "3729", "4000"),
        ("兔兔伯爵单推人", "豪3", "2140", "3600"),
        ("慕汐洛", "豪2", "2294", "3200"),
        ("残阳、落花", "豪2", "1236", "3200"),
        ("彧诀", "豪2", "522", "3200"),
        ("妄寻瑜", "豪1", "1392", "2800"),
    ]
    import pupulinbot.ms_rank as module

    monkeypatch.setattr(module, "ranking_rows", AsyncMock(return_value=standings))
    calls = []
    pasted = []
    original = ImageDraw.ImageDraw.text
    original_paste = Image.Image.paste

    def capture(draw, xy, text, *args, **kwargs):
        calls.append((xy, text, kwargs.get("anchor")))
        return original(draw, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture)

    def capture_paste(image, source, box=None, mask=None):
        if image.size[0] == 680:
            pasted.append((box, source.size))
        return original_paste(image, source, box, mask)

    monkeypatch.setattr(Image.Image, "paste", capture_paste)
    result = await group_ranking(SimpleNamespace(mode="四麻"), [])
    image = Image.open(io.BytesIO(result))
    assert image.format == "PNG" and image.size == (680, 594)
    for index, (name, rank, _points, _cap) in enumerate(standings):
        y = 219 + index * 64
        assert ((48, y), name, "lm") in calls
        assert ((302, y), rank, "lm") in calls
        assert ((526, y), "/", "mm") in calls
        assert any(x + w == 500 and yy == y - 12 for (x, yy), (w, _) in pasted)
        assert any(x + w == 632 and yy == y - 12 for (x, yy), (w, _) in pasted)
        assert ((250, y - 25), (50, 50)) in pasted
