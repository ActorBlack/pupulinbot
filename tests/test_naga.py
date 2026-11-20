import asyncio
import gzip
import io
import json
from pathlib import Path

import httpx
import pytest
from PIL import Image

from pupulinbot.naga import (
    NagaClient,
    NagaError,
    command_url,
    parse_report,
    render_report,
    report_id,
)

ID = "137fae62304e2b60c48682aa14cfe75bf8e8945936e7e203e28899501c38a20fv2_2"
URL = f"https://naga.dmv.nico/htmls/report_viewer.html?report_id={ID}&tw=0"
FIXTURE = Path(__file__).parent / "fixtures/naga/report.json.gz"


def real_report():
    return json.loads(gzip.decompress(FIXTURE.read_bytes()))


def test_real_report_matches_all_metrics_in_user_example():
    report = parse_report(real_report(), ID)
    assert report.game == "2026111913gm-00a9-0000-95f9cfde"
    assert report.rounds == 12
    assert report.models == ["ガンマ", "ニシキ", "アナンタ", "ウロボロス"]
    assert [p.name for p in report.players] == ["ププリン", "fangran", "ぷーやん", "tacolog"]
    assert [p.score for p in report.players] == [38500, 31100, 20300, 10100]
    expected = [
        [(92.80, 83.45, 3.60), (92.61, 87.05, 4.32), (77.47, 69.78, 15.11), (83.16, 77.70, 13.67)],
        [(87.43, 76.15, 6.92), (86.39, 77.69, 10.00), (74.33, 70.00, 22.31), (75.14, 73.85, 23.08)],
        [(90.20, 79.84, 3.10), (89.57, 80.62, 5.43), (83.44, 75.19, 12.40), (80.91, 75.97, 15.50)],
        [(90.74, 79.26, 4.44), (89.21, 80.00, 4.44), (66.87, 57.78, 30.37), (74.60, 66.67, 21.48)],
    ]
    for player, values in zip(report.players, expected, strict=True):
        assert [tuple(round(v, 2) for v in metric) for metric in player.metrics] == values
    assert [(p.reach, p.calls, p.wins, p.deal_ins, p.decisions) for p in report.players] == [
        (1, 4, 3, 2, 139),
        (3, 7, 4, 1, 130),
        (3, 3, 2, 2, 129),
        (1, 2, 0, 2, 135),
    ]


@pytest.mark.parametrize(
    "url",
    [
        "http://naga.dmv.nico/htmls/report_viewer.html?report_id=abcdefgh",
        "https://127.0.0.1/htmls/report_viewer.html?report_id=abcdefgh",
        "https://naga.dmv.nico.evil.com/htmls/report_viewer.html?report_id=abcdefgh",
        "https://naga.dmv.nico@evil.com/htmls/report_viewer.html?report_id=abcdefgh",
        "https://naga.dmv.nico/htmls/report_viewer.html?report_id=..%2Fsecret",
        URL + "&report_id=abcdefgh",
        "https://naga.dmv.nico/",
        URL.replace("report_viewer", "private"),
    ],
)
def test_only_public_report_url_is_accepted(url):
    with pytest.raises(NagaError):
        report_id(url)


def test_command_and_new_viewer():
    assert command_url("-r " + URL) == URL
    assert report_id(URL.replace("report_viewer.html", "report_viewer_v2.html")) == ID
    for text in (URL, "-r", "-r " + URL + " other", "-x " + URL):
        with pytest.raises(NagaError):
            command_url(text)


def minimal_report():
    vector = [0] * 34
    vector[0] = 100
    vector[1] = 9900
    return {
        "player_info": {"name": ["甲", "乙", "丙", "丁"]},
        "pred": [
            [
                {
                    "info": {
                        "msg": {
                            "type": "start_kyoku",
                            "end_msgs": [
                                {
                                    "type": "hora",
                                    "actor": 0,
                                    "target": 1,
                                    "scores": [28000, 22000, 25000, 25000],
                                }
                            ],
                        }
                    }
                },
                {
                    "info": {
                        "msg": {"type": "tsumo", "actor": 0, "real_dahai": "1m", "pred_dahai": "2m"}
                    },
                    "dahai_pred": vector,
                },
            ]
        ],
    }


def test_legacy_single_model_and_no_decisions():
    report = parse_report(minimal_report(), ID)
    assert report.models == ["ω"]
    p = report.players[0]
    assert p.name == "甲" and p.metrics[0] == pytest.approx((2, 0, 100))
    assert report.players[1].metrics == [(None, None, None)]
    assert report.players[-1].deal_ins == 1
    assert Image.open(io.BytesIO(render_report(report))).height == 570


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(pred=[]),
        lambda d: d["player_info"].update(name=["甲"]),
        lambda d: d["pred"][0][1]["info"]["msg"].update(actor=9),
        lambda d: d["pred"][0][0]["info"]["msg"]["end_msgs"][0].pop("scores"),
        lambda d: d["pred"][0][1]["dahai_pred"].__setitem__(0, float("nan")),
    ],
)
def test_bad_data_fails_cleanly(mutate):
    data = minimal_report()
    mutate(data)
    with pytest.raises(NagaError):
        parse_report(data, ID)


def test_render_four_models_and_long_names():
    data = real_report()
    data["player_info"]["name"][0] = "很长的昵称" * 20
    picture = render_report(parse_report(data, ID))
    image = Image.open(io.BytesIO(picture))
    assert image.format == "PNG" and image.size == (1978, 570)


@pytest.mark.asyncio
@pytest.mark.parametrize("compressed", [True, False])
async def test_public_download_cache_and_concurrent_coalescing(compressed):
    calls = []

    async def respond(request):
        calls.append(request)
        assert request.url == f"https://naga.dmv.nico/reports/{ID}.json.gz"
        assert "authorization" not in request.headers
        await asyncio.sleep(0.01)
        raw = FIXTURE.read_bytes() if compressed else gzip.decompress(FIXTURE.read_bytes())
        return httpx.Response(200, content=raw)

    client = NagaClient(httpx.MockTransport(respond))
    try:
        results = await asyncio.gather(*(client.picture(URL) for _ in range(3)))
        assert results[0] == results[1] == results[2]
        assert await client.picture(URL) == results[0]
        assert len(calls) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body", [(404, b""), (403, b""), (302, b""), (200, b"not json"), (200, b"{}")]
)
async def test_download_failures_return_user_message(status, body):
    client = NagaClient(httpx.MockTransport(lambda r: httpx.Response(status, content=body)))
    try:
        with pytest.raises(NagaError):
            await client.picture(URL)
    finally:
        await client.close()
