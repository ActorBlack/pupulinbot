from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from pupulinbot.batch_export import export_rows
from pupulinbot.batch_review import BatchReviews
from pupulinbot.batchmortal_worker import validate_request
from pupulinbot.review import ReviewError, parse_review_link
from pupulinbot.review_cache import ReviewCache

LOG = "2026110806gm-00a9-0000-f946a576"
LINK = f"http://tenhou.net/4/?log={LOG}&tw=2"


@pytest.mark.parametrize(
    "text",
    [LINK, f"https://tenhou.net/0/?log={LOG}&tw=2", f"https://www.tenhou.net/3/?log={LOG}&tw=2"],
)
def test_official_tenhou_urls_preserve_seat_and_normalize_to_https(text):
    log = parse_review_link(text)
    assert (log.source, log.game_id, log.seat) == ("tenhou", LOG, 2)
    assert log.url == f"https://tenhou.net/0/?log={LOG}&tw=2"
    assert parse_review_link(text, 1).url.endswith("&tw=1")
    assert parse_review_link(LOG).seat == 0


@pytest.mark.parametrize(
    "text",
    [
        f"https://tenhou.net.evil.invalid/0/?log={LOG}",
        f"https://tenhou.net@evil.invalid/0/?log={LOG}",
        f"https://evil.invalid/0/?log={LOG}",
        f"https://tenhou.net/0/?log={LOG}&log={LOG}",
        f"https://tenhou.net/0/?log={LOG}&tw=4",
        f"https://tenhou.net/0/?log={LOG}&tw=1&tw=2",
        "https://tenhou.net/0/?log=../../report",
        f"https://tenhou.net:444/0/?log={LOG}",
        f"file://tenhou.net/0/?log={LOG}",
    ],
)
def test_untrusted_or_ambiguous_tenhou_links_are_rejected(text):
    with pytest.raises(ReviewError):
        parse_review_link(text)


@pytest.mark.asyncio
async def test_mixed_link_manifests_deduplicate_game_and_seat_and_export_source(tmp_path):
    batches = BatchReviews(tmp_path, None, SimpleNamespace(client=SimpleNamespace(url=None)))
    ms = "https://game.maj-soul.com/1/?paipu=261029-b5c0d5be-b902-42c5-a4e0-e7cf2dbfd4da"
    job = await batches.create_links(
        [LINK, LINK.replace("/4/", "/0/"), LINK.replace("tw=2", "tw=1"), ms], "g", "u"
    )
    assert len(job["rows"]) == 3
    assert [(r["source"], r["seat"]) for r in job["rows"]] == [
        ("tenhou", 2),
        ("tenhou", 1),
        ("majsoul", 0),
    ]
    with pytest.raises(ReviewError, match="昵称"):
        export_rows(job)
    for row in job["rows"]:
        row.update(player_name="真实昵称", player_name_source="original")
    assert [r["source"] for r in export_rows(job)] == ["tenhou", "tenhou", "majsoul"]
    assert all(r["started_at"] is None for r in job["rows"])
    assert ReviewCache.key(job["rows"][0], "4.1b") != ReviewCache.key(
        {**job["rows"][0], "source": "majsoul"}, "4.1b"
    )


@pytest.mark.asyncio
async def test_all_four_table_seats_reach_browser_validator_with_matching_tw(tmp_path):
    batches = BatchReviews(tmp_path, None, SimpleNamespace(client=SimpleNamespace(url=None)))
    job = await batches.create_links([LINK], "g", "u", table=True)
    assert len(job["rows"]) == 4
    for row in job["rows"]:
        canonical = validate_request({"online_url": row["online_url"], "seat": row["seat"]})
        assert canonical.endswith(f"&tw={row['seat']}")
    with pytest.raises(ValueError):
        validate_request(
            {
                "online_url": "https://mjai.ekyu.moe/?"
                + urlencode({"url": f"https://evil.invalid/?log={LOG}"}),
                "seat": 0,
            }
        )
