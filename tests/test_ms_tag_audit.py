import io
import math
from datetime import UTC, datetime

import pytest
from PIL import Image, ImageDraw
from test_ms_profile import stats as profile_stats
from test_ms_tags import records, stats
from test_ms_trait_tags import classify, sample_raw

from pupulinbot.models import Player
from pupulinbot.ms_charts import render_profile
from pupulinbot.ms_tags import MAX_TAGS, reference_for, supplemental_tags
from pupulinbot.ms_trait_tags import efficiency_metrics, select_tags, trait_tags, yakuman_victim


@pytest.mark.parametrize(
    "key,side,label",
    [
        ("立直后放铳率", "high", "全自动放铳"),
        ("立直后放铳率", "low", "全自动防守"),
        ("立直后和牌率", "high", "立直收割机"),
        ("立直后和牌率", "low", "场供贡献者"),
        ("立直后流局率", "high", "立直流局王"),
        ("立直多面", "high", "好形爱好者"),
        ("立直多面", "low", "愚形爱好者"),
    ],
)
def test_new_riichi_traits_use_room_thresholds_and_inclusive_boundaries(key, side, label):
    raw = sample_raw()
    if key == "立直后和牌率":
        raw.pop("立直和了")
    raw[key] = reference_for("12")["traits"][key][side]
    assert label in classify(raw)["style"]
    raw[key] += -1e-7 if side == "high" else 1e-7
    assert label not in classify(raw)["style"]


def test_riichi_traits_need_50_declarations_not_just_many_hands():
    raw = sample_raw()
    raw.update(立直率=49 / 3600, 立直后放铳率=0.3)
    raw.pop("立直和了")
    assert "全自动放铳" not in classify(raw)["style"]
    raw["立直率"] = 50 / 3600
    assert "全自动放铳" in classify(raw)["style"]


def test_chase_risk_is_a_combination_and_does_not_invent_a_conditional_deal_in_rate():
    raw = sample_raw()
    raw.update(追立率=0.35, 先制率=0.65, 立直后放铳率=0.3)
    tags = classify(raw)["style"]
    assert "追立冒险家" in tags
    assert "后发制人" not in tags and "全自动放铳" not in tags
    raw.pop("立直后放铳率")
    raw["追立放铳率"] = 0.9  # Not an endpoint field; do not consume invented data.
    assert "追立冒险家" not in classify(raw)["style"]


def test_inconsistent_riichi_outcomes_and_win_counter_are_rejected():
    raw = sample_raw()
    raw.update(立直后放铳率=0.9, 立直后和牌率=0.9, 立直后流局率=0.9)
    assert not {"全自动放铳", "立直收割机", "立直流局王"} & set(classify(raw)["style"])
    raw = sample_raw()
    raw["立直后和牌率"] = 0.7  # Contradicts the exact 342 wins / 684 declarations.
    assert "立直收割机" not in classify(raw)["style"]
    raw["里宝率"] = 0.9
    assert "里宝超人" not in classify(raw)["luck"]


def test_multisided_does_not_mean_good_shape_and_requires_full_collection_period():
    raw = sample_raw()
    raw["立直多面"] = 0.95
    raw["立直好型2"] = 0.1
    assert "好形爱好者" in classify(raw)["style"]
    old = records(count=300)
    old[0].started_at = datetime(2021, 9, 1, tzinfo=UTC)
    assert "好形爱好者" not in trait_tags(raw, reference_for("12"), 3600, games=old)["style"]
    raw.pop("立直多面")
    raw["立直好型"] = 0.95
    assert "好形爱好者" not in classify(raw)["style"]


def efficiency_raw():
    raw = sample_raw()
    for key in ("打点效率", "铳点损失", "净打点效率"):
        raw.pop(key, None)
    raw.update(和牌率=0.22, 放铳率=0.12, 平均打点=6500, 平均铳点=5400)
    return raw


@pytest.mark.parametrize(
    "key,side,label",
    [
        ("打点效率", "high", "火力全开"),
        ("铳点损失", "high", "散财童子"),
        ("净打点效率", "high", "高效打点"),
        ("净打点效率", "low", "低效打点"),
    ],
)
def test_efficiency_traits_compare_real_products_not_pt_or_round_income(key, side, label):
    raw = efficiency_raw()
    target = reference_for("12")["traits"][key][side]

    def set_target(value):
        if key == "打点效率":
            raw["平均打点"] = value / raw["和牌率"]
        elif key == "铳点损失":
            raw["平均铳点"] = value / raw["放铳率"]
        else:
            raw["平均打点"] = (value + raw["放铳率"] * raw["平均铳点"]) / raw["和牌率"]

    # Move a tiny amount inside to avoid arithmetic round-trip cancellation.
    set_target(target + (1e-7 if side == "high" else -1e-7))
    assert label in classify(raw)["style"]
    set_target(target + (-1e-7 if side == "high" else 1e-7))
    assert label not in classify(raw)["style"]
    raw["局收支"] = 1000000
    raw["gradingScore"] = 1000000
    assert label not in classify(raw)["style"]


def test_net_efficiency_can_be_negative_and_accepts_only_consistent_rounding():
    raw = efficiency_raw()
    raw.update(和牌率=0.1, 放铳率=0.3, 平均打点=5000, 平均铳点=7000)
    assert efficiency_metrics(raw) == {"打点效率": 500, "铳点损失": 2100, "净打点效率": -1600}
    assert {"散财童子", "低效打点"} <= set(classify(raw)["style"])
    raw.update(打点效率=500, 铳点损失=2100, 净打点效率=-1599)
    assert efficiency_metrics(raw)["净打点效率"] == -1600
    raw["净打点效率"] = 9999
    assert "净打点效率" not in efficiency_metrics(raw)
    assert "高效打点" not in classify(raw)["style"]
    raw["铳点损失"] = None
    assert "铳点损失" not in efficiency_metrics(raw)
    assert "净打点效率" not in efficiency_metrics(raw)


def test_efficiency_traits_need_actual_win_and_deal_sample_sizes():
    raw = efficiency_raw()
    raw.update(放铳率=0.005, 平均铳点=1000)
    assert "以小搏大" not in classify(raw)["style"]
    raw.update(和牌率=0.005, 平均打点=400000)
    assert "火力全开" not in classify(raw)["style"]


@pytest.mark.parametrize("mode", ["8", "9", "11", "12", "16"])
def test_high_points_threshold_is_mean_plus_500_rounded_up_to_100(mode):
    reference = reference_for(mode)
    threshold = reference["thresholds"]["points_high"]
    assert threshold == math.ceil((reference["means"]["平均打点"] + 500) / 100) * 100
    rows = records(count=300)
    for row in rows:
        row.mode = mode
    sample = stats(300, win_rate=0.15, raw={"平均打点": threshold})
    assert "高打点" in supplemental_tags(sample, rows, 42)
    sample.raw["平均打点"] = threshold - 1
    assert "高打点" not in supplemental_tags(sample, rows, 42)


def test_high_points_does_not_round_less_than_30_wins_up_to_minimum():
    sample = stats(100, win_rate=0.0296, raw={"_hand_count": 1000, "平均打点": 10000})
    assert "高打点" not in supplemental_tags(sample, records(), 42)
    sample.win_rate = 0.03
    assert "高打点" in supplemental_tags(sample, records(), 42)


def loss_record(game, *, count=0, limit=1):
    return {
        "id": game.uuid,
        "start_time": game.started_at.timestamp(),
        "fans": [{"id": 42, "label": "国士无双", "count": count, "役满": limit}],
    }


def test_yakuman_victim_requires_confirmed_fans_and_sample_uuid_and_time():
    rows = records()
    raw = {"最近大铳": loss_record(rows[0])}
    assert yakuman_victim(raw, rows)
    assert "国士受害者" in supplemental_tags(stats(raw=raw), rows, 42)
    raw["最近大铳"]["id"] = "outside-selected-sample"
    assert not yakuman_victim(raw, rows)
    raw["最近大铳"] = loss_record(rows[0])
    raw["最近大铳"]["start_time"] -= 10000
    assert not yakuman_victim(raw, rows)
    raw["最近大铳"] = loss_record(rows[0], count=13, limit=0)
    assert not yakuman_victim(raw, rows)  # Total han cannot identify a named yakuman.
    raw["最近大铳"] = loss_record(rows[0], count=12, limit=0)
    assert not yakuman_victim(raw, rows)
    rows[0].players[0]["score"] = -48000
    assert not yakuman_victim({}, rows)  # Final scores cannot identify a hand's yaku.


@pytest.mark.parametrize(
    "fans",
    [None, [], [None], [{"count": 13}], [{"count": True, "役满": 1}], [{"count": 13, "役满": -1}]],
)
def test_bad_yakuman_loss_fan_evidence_does_not_create_achievement(fans):
    rows = records()
    loss = loss_record(rows[0])
    loss["fans"] = fans
    assert not yakuman_victim({"最近大铳": loss}, rows)


def test_named_yakuman_can_share_one_slot_per_category():
    rows = records(count=1)
    loss = loss_record(rows[0])
    loss["fans"].extend([{"label": "四暗刻", "count": 1, "役满": 1}, loss["fans"][0]])
    raw = sample_raw()
    raw.update(
        {
            "役满": 2,
            "流满": 1,
            "最近大铳": loss,
            "_yakuman_win_names": ("大三元", "字一色"),
        }
    )
    extra = trait_tags(raw, reference_for("12"), 3600, games=rows)
    assert select_tags(["门清流"], extra, 4) == [
        "大三元",
        "字一色",
        "流局满贯",
        "国士受害者",
        "四暗刻受害者",
        "门清流",
    ]
    loss["fans"] = [{"label": "混一色", "count": 13, "役满": 0}]
    assert yakuman_victim(raw, rows) == ()
    loss["fans"] = [{"count": 1, "役满": 1}]
    assert yakuman_victim(raw, rows) == ()


def test_double_riichi_counter_has_no_display_label():
    raw = sample_raw()
    raw["W立直"] = 10
    assert classify(raw)["achievement"] == []


@pytest.mark.parametrize("key,label", [("役满", "大三元"), ("流满", "流局满贯")])
def test_real_achievement_can_appear_in_one_game_without_style_sample(key, label):
    rows = records(count=1)
    sample = stats(1, win_rate=1, raw={"_hand_count": 1, key: 1, "_yakuman_win_names": ("大三元",)})
    assert supplemental_tags(sample, rows, 42) == [label]
    sample.games = 2
    assert supplemental_tags(sample, rows, 42) == []


def test_yakuman_victim_only_needs_one_matched_record_not_hand_count():
    rows = records(count=1)
    sample = stats(1, raw={"_hand_count": None, "最近大铳": loss_record(rows[0])})
    assert supplemental_tags(sample, rows, 42) == ["国士受害者"]
    sample.raw["最近大铳"]["id"] = "not-in-sample"
    assert supplemental_tags(sample, rows, 42) == []


def test_style_needs_twenty_games_and_one_hundred_hands_but_not_one_hundred_games():
    rows = records(count=20)
    sample = stats(20, call_rate=0.6, raw={"_hand_count": 99})
    assert "副露流" not in supplemental_tags(sample, rows, 42)
    sample.raw["_hand_count"] = 100
    assert "副露流" in supplemental_tags(sample, rows, 42)
    sample.games = 19
    assert "副露流" not in supplemental_tags(sample, rows[:19], 42)


def test_value_and_income_traits_are_available_before_fifty_games():
    rows = records(count=20)
    sample = stats(20, win_rate=0.1, raw={"_hand_count": 300, "平均打点": 7100, "局收支": 150})
    assert {"高打点", "局收支重视"} <= set(supplemental_tags(sample, rows, 42))
    sample.raw["_hand_count"] = 299
    tags = supplemental_tags(sample, rows, 42)
    assert "局收支重视" not in tags and "高打点" not in tags


def test_recent_form_uses_thirty_previous_and_twenty_recent_games():
    rows = records([4] * 30 + [1] * 20)
    assert "好调中" in supplemental_tags(stats(50), rows, 42)
    assert "好调中" not in supplemental_tags(stats(49), rows[1:], 42)


def test_starting_hand_trait_requires_three_hundred_hands_and_a_tenth_shanten_gap():
    reference = reference_for("12")
    raw = sample_raw()
    raw.update(平均起手向听=reference["traits"]["平均起手向听"]["low"])
    assert "生于罗马" not in trait_tags(raw, reference, 299)["luck"]
    assert "生于罗马" in trait_tags(raw, reference, 300)["luck"]
    raw["平均起手向听"] += 1e-7
    assert "生于罗马" not in trait_tags(raw, reference, 300)["luck"]


def test_efficiency_minimum_is_four_hundred_hands_and_thirty_relevant_events():
    raw = efficiency_raw()
    raw.update(和牌率=0.3, 平均打点=8000)
    reference = reference_for("12")
    assert "火力全开" not in trait_tags(raw, reference, 399)["style"]
    assert "火力全开" in trait_tags(raw, reference, 400)["style"]
    raw.update(放铳率=29 / 400, 平均铳点=20000)
    assert "散财童子" not in trait_tags(raw, reference, 400)["style"]
    raw["放铳率"] = 30 / 400
    assert "散财童子" in trait_tags(raw, reference, 400)["style"]
    raw["平均铳点"] = 1000
    assert "以小搏大" not in trait_tags(raw, reference, 400)["style"]


@pytest.mark.parametrize("many_yakuman", [False, True])
def test_additional_tags_fit_inside_filtered_profile_card(monkeypatch, many_yakuman):
    tags = [
        "国士受害者",
        "罚符小偷",
        "愚形爱好者",
        "低效打点",
        "里宝绝缘体",
        "立直收割机",
        "一发绝缘体",
        "立直流局王",
        "追立冒险家",
        "以小搏大",
    ]
    if many_yakuman:
        names = (
            "大三元",
            "大四喜",
            "四暗刻单骑",
            "字一色",
            "绿一色",
            "四杠子",
            "清老头",
            "小四喜",
            "纯正九莲宝灯",
            "九莲宝灯",
            "国士无双十三面",
            "天和",
            "地和",
        )
        tags = [*names, *(name + "受害者" for name in names), *tags[1:]]
    monkeypatch.setattr("pupulinbot.ms_profile.supplemental_tags", lambda *a, **k: tags)
    drawn = []
    rows = set()
    bottoms = []
    original = ImageDraw.ImageDraw.text

    def capture(self, xy, value, *args, **kwargs):
        if value.startswith("#") and any(tag in value for tag in tags):
            drawn.append(value)
            rows.add(xy[1])
            assert kwargs["fill"] == (
                "#986b1d"
                if value[1:] == "国士受害者" or (many_yakuman and value[1:] not in tags[-9:])
                else "#7561a8"
            )
            box = self.textbbox(xy, value, font=kwargs["font"], anchor=kwargs.get("anchor"))
            assert 0 <= box[0] < box[2] <= 1600 and 0 <= box[1] < box[3]
            bottoms.append(box[3])
        return original(self, xy, value, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture)
    value = profile_stats()
    png = render_profile(
        Player(42, "标签排版示例"), value, scope="四麻 · 玉南 · 最近100场", games=records()
    )
    width, height = Image.open(io.BytesIO(png)).size
    assert width == 1600
    assert height == 1258 + 38 * max(0, len(rows) - 2)
    assert (len(rows) > 2) == many_yakuman
    assert max(bottoms) < height
    assert " ".join(drawn) == " ".join("#" + tag for tag in tags)
    assert MAX_TAGS == 10
