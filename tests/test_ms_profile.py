import io
import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from pupulinbot.models import Player, PlayerStats
from pupulinbot.ms_charts import render_profile
from pupulinbot.ms_profile import SIZE, profile_content


def stats():
    raw = dict(
        json.loads((Path(__file__).parent / "fixtures/style-reference-oracle.json").read_text())[0][
            "metrics"
        ]
    )
    raw.update(
        {
            "level": {"id": 10502, "score": 3700, "delta": 85},
            "平均打点": 6800,
            "平均铳点": 5400,
        }
    )
    return PlayerStats(
        9934016, "四麻", 2193, 0.29, 0.27, 0.19, 0.25, 2.4, 0.032, 0.223, 0.109, 0.201, 0.254, raw
    )


def test_profile_uses_api_metrics_and_shared_weighted_score_formula():
    content = profile_content(Player(9934016, "喵杰"), stats())
    hero = dict(content["hero"])
    rows = dict(row for _, _, section in content["groups"] for row in section)
    assert hero == {"平均顺位": "2.40", "连对率": "56.00%", "和铳度差": "+927.8", "击飞率": "3.20%"}
    assert rows["平均打点"] == "6,800" and rows["平均铳点"] == "5,400"
    assert rows["流局听牌率"] == "42.16%"
    assert rows["立直后放铳率"] == "—"
    assert content["rank"] == "雀圣2星" and content["point_text"] == "3785 / 6000 PT"


@pytest.mark.parametrize("mode", [3, 4])
def test_profile_render_has_no_account_id_or_removed_prefixes_and_no_clipped_text(
    monkeypatch, mode
):
    drawn = []
    original = ImageDraw.ImageDraw.text

    def record(self, xy, value, *args, **kwargs):
        drawn.append(str(value))
        box = self.textbbox(xy, value, font=kwargs["font"], anchor=kwargs.get("anchor"))
        assert 0 <= box[0] <= box[2] <= SIZE[0]
        assert 0 <= box[1] <= box[3] <= SIZE[1]
        return original(self, xy, value, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", record)
    value = stats()
    if mode == 3:
        value.mode, value.third_rate, value.average_rank = "三麻", 0.44, 2.15
        value.raw["level"] = {"id": 20502, "score": 3700, "delta": 85}
    png = render_profile(Player(9934016, "喵杰"), value)
    rendered = Image.open(io.BytesIO(png))
    assert rendered.size == SIZE and rendered.mode == "RGB"
    text = "\n".join(drawn)
    assert all(
        value not in text for value in ("account ID", "9934016", "牌谱屋记录段位", "累计牌风")
    )
    assert "好友码" in text and "和铳度差" in text and "平均打点" in text
    assert "连对率" in drawn and "击飞率" in drawn
    assert drawn.index("和牌与打点") < drawn.index("防守与流局") < drawn.index("立直与副露")
    assert all(f"{i}位" in drawn for i in range(1, mode + 1))
    if mode == 3:
        assert "4位" not in drawn and "三麻　已收录 2,193 场" in drawn
        assert not any(label.startswith("#") for label in drawn)


@pytest.mark.parametrize("case", ["empty", "invalid", "sanma", "long_name"])
def test_profile_handles_missing_data_sanma_and_long_nicknames(case):
    value, name = stats(), "喵杰"
    if case in {"empty", "invalid"}:
        value.raw = {}
        value.win_rate = value.deal_in_rate = value.call_rate = value.riichi_rate = None
        value.negative_rate = None
        value.games, value.average_rank = 0, 0
        if case == "empty":
            value.win_rate = value.deal_in_rate = value.call_rate = value.riichi_rate = 0
            value.negative_rate = 0
            value.raw.update({"平均打点": 0, "平均铳点": 0})
        if case == "invalid":
            value.raw.update({"平均打点": float("nan"), "平均铳点": -1})
        content = profile_content(Player(9934016, name), value)
        assert content["ranks"] == [None] * 4 and not content["tag"]
        assert dict(content["hero"])["和铳度差"] == "—"
        assert dict(content["hero"])["平均顺位"] == "—"
        assert dict(content["hero"])["击飞率"] == "—"
        assert all(v == "—" for _, _, rows in content["groups"] for _, v in rows)
    elif case == "sanma":
        value.mode, value.third_rate, value.average_rank = "三麻", 0.44, 2.15
        content = profile_content(Player(9934016, name), value)
        assert len(content["ranks"]) == 3 and content["tag"] == ""
    else:
        name = "超长昵称ABCDEFG" * 30
    assert Image.open(io.BytesIO(render_profile(Player(9934016, name), value))).size == SIZE


def test_profile_uses_post_promotion_rank_and_progress():
    value = stats()
    value.raw["level"] = {"id": 10502, "score": 5900, "delta": 110}
    content = profile_content(Player(9934016, "喵杰"), value)
    assert content["rank"] == "雀圣3星"
    assert content["point_text"] == "4500 / 9000 PT" and content["progress"] == 0.5
