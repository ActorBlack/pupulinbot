import io
import math

from PIL import Image

from pupulinbot.batch_chart import chart_series, render_batch_chart


def test_chart_preserves_missing_results_and_separates_models():
    job = {
        "nickname": "雀士",
        "requested": 4,
        "rows": [
            {
                "started_at": "2026-01-04",
                "status": "done",
                "metrics": {"rating": 90, "model": "4.1b", "temperature": 0.1},
            },
            {"started_at": "2026-01-03", "status": "pending", "metrics": None},
            {
                "started_at": "2026-01-02",
                "status": "done",
                "metrics": {"rating": 85, "model": "3.0", "temperature": 0.1},
            },
            {
                "started_at": "2026-01-01",
                "status": "done",
                "metrics": {"rating": 80, "model": "4.1b", "temperature": 0.1},
            },
        ],
    }
    _, series = chart_series(job)
    assert series[("4.1b", 0.1)][0] == 80
    assert math.isnan(series[("4.1b", 0.1)][1])
    assert math.isnan(series[("4.1b", 0.1)][2])
    assert series[("4.1b", 0.1)][3] == 90
    assert series[("3.0", 0.1)][1] == 85
    image = Image.open(io.BytesIO(render_batch_chart(job)))
    assert image.format == "PNG"
    assert image.width >= 1200


def test_fifty_game_report_preserves_all_points():
    job = {
        "nickname": "y024",
        "requested": 50,
        "rows": [
            {
                "started_at": f"2026-10-{1 + i // 3:02d}T{i % 3:02d}:00:00",
                "status": "done",
                "metrics": {
                    "rating": 85 + i % 12,
                    "model": "4.1b",
                    "temperature": 0.1,
                    "matches": 80,
                    "decisions": 100,
                    "agreement": 80,
                    "bad5": 5,
                    "bad10": 8,
                    "badmove_decisions": 100,
                    "bad5_rate": 5,
                    "bad10_rate": 8,
                },
            }
            for i in range(50)
        ],
    }
    rows, series = chart_series(job)
    assert len(rows) == 50 and len(series[("4.1b", 0.1)]) == 50
    image = Image.open(io.BytesIO(render_batch_chart(job)))
    assert image.size == (2240, 1600)


def test_table_shows_names_first_and_final_rank_instead_of_model_and_seat():
    from pupulinbot.batch_chart import TABLE_HEADERS, table_rows

    job = {
        "kind": "table",
        "rows": [
            {"seat": 2, "status": "pending", "player_name": "西家昵称"},
            {"seat": 0, "status": "done", "player_name": "东家昵称", "metrics": {"rating": 90}},
            {"seat": 3, "status": "failed"},
            {
                "seat": 1,
                "status": "done",
                "backend_artifacts": {"playerNames": ["东", "南家昵称", "西", "北"]},
            },
        ],
    }
    image = Image.open(io.BytesIO(render_batch_chart(job)))
    assert image.format == "PNG"
    assert TABLE_HEADERS[:2] == ("游戏昵称", "最终名次")
    assert not {"状态", "起始座位", "模型"} & set(TABLE_HEADERS)
    assert [r["name"] for r in table_rows(job)] == ["东家昵称", "南家昵称", "西家昵称", "—"]


def test_table_uses_game_placement_and_rating_highlights():
    from pupulinbot.batch_chart import rating_color, table_rows

    job = {
        "rows": [
            {
                "seat": 0,
                "rank": 4,
                "status": "done",
                "player_name": "高评分四位",
                "metrics": {"rating": 95},
            },
            {
                "seat": 1,
                "rank": 1,
                "status": "done",
                "player_name": "一位",
                "metrics": {"rating": 80},
            },
        ]
    }
    assert [r["name"] for r in table_rows(job)] == ["一位", "高评分四位"]
    assert rating_color(95.01) == "#a855f7"
    assert rating_color(95) == "#a855f7"
    assert rating_color(94.99) == "#a855f7"
    assert rating_color(94) == "#a855f7"
    assert rating_color(93.99) == "#f2ba28"
    assert rating_color(90.01) == "#f2ba28"
    assert rating_color(90) == "#22324b"
    assert rating_color(float("nan")) == "#22324b"
    image = Image.open(io.BytesIO(render_batch_chart({**job, "kind": "table"})))

    def red_pixels(box):
        return sum(r > 170 and g < 100 and b < 110 for r, g, b in image.crop(box).getdata())

    assert red_pixels((610, 232, 820, 306)) == 0
    assert (
        sum(
            b > 170 and r > 100 and g < 160
            for r, g, b in image.crop((610, 312, 820, 386)).getdata()
        )
        > 60
    )
