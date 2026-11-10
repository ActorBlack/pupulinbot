"""Protocol and lifecycle regressions; runnable with unittest or pytest."""

import asyncio
import io
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

from pupulinbot.formatting import finish_message, format_stats
from pupulinbot.live import LiveServiceClient, LiveServiceError
from pupulinbot.models import GameRecord
from pupulinbot.monitor import GameMonitor
from pupulinbot.paipuya import PaipuyaClient, PaipuyaError
from pupulinbot.review import MortalClient, ReviewError, ReviewQueue, ReviewResult, render_review

HTTP_CLIENT = httpx.AsyncClient


def mocked_http(handler):
    return patch(
        "httpx.AsyncClient",
        side_effect=lambda **kwargs: HTTP_CLIENT(transport=httpx.MockTransport(handler), **kwargs),
    )


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_record_fields_scores_and_tie_break(self):
        payload = [
            {
                "uuid": "native-game",
                "startTime": 1700000000,
                "endTime": 1700000100,
                "modeId": 12,
                "players": [
                    {"accountId": 42, "nickname": "雀士", "score": 30000, "gradingScore": 55},
                    {"accountId": 43, "score": 30000},
                    {"accountId": 44, "score": 40000},
                ],
            }
        ]
        with mocked_http(lambda request: httpx.Response(200, json=payload)):
            record = (await PaipuyaClient("https://api.example").records(42))[0]
        self.assertEqual(record.seat(42)["rank"], 2)
        self.assertEqual(record.seat(43)["rank"], 3)
        self.assertEqual(record.players[0]["account_id"], 42)
        self.assertEqual(record.mode, "12")
        self.assertIn("第 2 位", finish_message("雀士", record, 42))
        self.assertNotIn("55 位", finish_message("雀士", record, 42))

    async def test_sanma_stats_use_correct_range_and_modes(self):
        def handle(request):
            parts = request.url.path.split("/")
            self.assertEqual(parts[-2], "0")
            self.assertGreater(int(parts[-1]), 0)
            self.assertEqual(request.url.params["mode"], "26.25.24.23.22.21")
            if "extended" in request.url.path:
                return httpx.Response(
                    200,
                    json={
                        "count": 1000,
                        "和牌率": 0.22,
                        "放铳率": 0.1,
                        "立直率": 0.2,
                        "副露率": 0.3,
                    },
                )
            return httpx.Response(200, json={"count": 100, "rank_rates": [40, 35, 25]})

        with mocked_http(handle):
            stats = await PaipuyaClient("https://api.example", mode="三麻").stats(42)
        self.assertEqual(stats.games, 100)
        self.assertAlmostEqual(stats.average_rank, 1.85)
        self.assertEqual(stats.win_rate, 0.22)
        self.assertEqual(stats.deal_in_rate, 0.1)
        self.assertIn("和牌 22.00%", format_stats("雀士", stats))

    async def test_extended_stats_failure_does_not_invent_zero_rates(self):
        def handle(request):
            if "extended" in request.url.path:
                return httpx.Response(503)
            return httpx.Response(200, json={"count": 10, "rank_rates": [0.3, 0.3, 0.2, 0.2]})

        with mocked_http(handle):
            stats = await PaipuyaClient("https://api.example").stats(42)
        self.assertIsNone(stats.win_rate)
        self.assertIn("和牌 暂无数据", format_stats("雀士", stats))

    async def test_sanma_records_are_descending(self):
        def handle(request):
            self.assertEqual(request.url.params["mode"], "26,25,24,23,22,21")
            self.assertEqual(request.url.params["descending"], "true")
            return httpx.Response(200, json=[])

        with mocked_http(handle):
            await PaipuyaClient("https://api.example", mode="三麻").records(42)

    async def test_ambiguous_nickname_requires_account_id(self):
        client = PaipuyaClient("https://api.example")
        with mocked_http(
            lambda request: httpx.Response(
                200,
                json=[
                    {"id": 42, "nickname": "同名"},
                    {"id": 43, "nickname": "同名"},
                ],
            )
        ):
            with self.assertRaisesRegex(PaipuyaError, "42.*43"):
                await client.resolve("同名")

    async def test_bad_records_become_domain_errors(self):
        for payload in [{"error": "maintenance"}, [{"startTime": 1}], [None]]:
            with (
                self.subTest(payload=payload),
                mocked_http(lambda request, payload=payload: httpx.Response(200, json=payload)),
            ):
                with self.assertRaises(PaipuyaError):
                    await PaipuyaClient("https://api.example").records(42)

    async def test_live_responses_and_utc(self):
        client = LiveServiceClient("https://live.example", "secret")
        for started in [1700000000000, "2023-11-14T22:13:20Z", "2023-11-14T22:13:20"]:

            def handle(request, started=started):
                self.assertEqual(request.headers["Authorization"], "Bearer secret")
                return httpx.Response(200, json={"uuid": "game", "started_at": started})

            with self.subTest(started=started), mocked_http(handle):
                game = await client.current_game(42)
                self.assertEqual(game.started_at, datetime.fromtimestamp(1700000000, UTC))

    async def test_bad_live_responses_become_domain_errors(self):
        for payload in [[], {"playing": True}, {"uuid": "game", "started_at": "bad"}]:
            with (
                self.subTest(payload=payload),
                mocked_http(lambda request, payload=payload: httpx.Response(200, json=payload)),
            ):
                with self.assertRaises(LiveServiceError):
                    await LiveServiceClient("https://live.example", None).current_game(42)

    async def test_live_not_playing(self):
        for status, payload in [(404, None), (200, {"playing": False})]:
            with mocked_http(
                lambda request, status=status, payload=payload: httpx.Response(status, json=payload)
            ):
                self.assertIsNone(
                    await LiveServiceClient("https://live.example", None).current_game(42)
                )

    async def test_async_mortal_relative_status_url_and_auth(self):
        def handle(request):
            self.assertEqual(request.headers["Authorization"], "Bearer secret")
            if request.method == "POST":
                return httpx.Response(200, json={"status": "queued", "status_url": "/jobs/1"})
            self.assertEqual(request.url.path, "/jobs/1")
            return httpx.Response(200, json={"status": "done", "rating": 88, "rank": "A"})

        with mocked_http(handle), patch("pupulinbot.review.asyncio.sleep", new=AsyncMock()):
            result = await MortalClient("https://review.example", "secret").review("log")
        self.assertEqual(result.rating, 88)

    async def test_mortal_rejects_invalid_results_and_foreign_poll_url(self):
        for payload in [
            [],
            {"status": "queued"},
            {"status": "done"},
            {"status": "done", "rating": "nan"},
            {"status": "unknown", "rating": 88},
            {"status": "queued", "status_url": "https://other.example/job"},
        ]:
            calls = []

            def handle(request, calls=calls, payload=payload):
                calls.append(request)
                return httpx.Response(200, json=payload)

            with self.subTest(payload=payload), mocked_http(handle):
                with self.assertRaises(ReviewError):
                    await MortalClient("https://review.example", "secret").review("log")
            self.assertEqual(len(calls), 1)

    async def test_mortal_timeout_bounds_whole_request(self):
        async def handle(request):
            await asyncio.sleep(0.1)
            return httpx.Response(200, json={"rating": 88})

        with mocked_http(handle):
            with self.assertRaisesRegex(ReviewError, "超时"):
                await MortalClient("https://review.example", None, 0.01).review("log")

    def test_score_card_with_chinese_nickname_is_valid_png(self):
        rendered = render_review(ReviewResult(88, "A", None, {}), "中文雀士", "game-uuid")
        with Image.open(io.BytesIO(rendered)) as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.size, (1000, 560))


class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

        async def analyze(url, seat):
            self.entered.set()
            await self.release.wait()
            return ReviewResult(88, "A", None, {})

        self.queue = ReviewQueue(SimpleNamespace(url="https://review.example", review=analyze))
        self.queue.start()

    async def asyncTearDown(self):
        await self.queue.close()

    async def test_cancelled_request_does_not_kill_worker(self):
        first = asyncio.create_task(self.queue.submit("one"))
        await self.entered.wait()
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.release.set()
        second = await asyncio.wait_for(self.queue.submit("two"), 1)
        self.assertEqual(second.rating, 88)
        self.assertFalse(self.queue.tasks[0].done())

    async def test_close_resolves_running_and_queued_requests(self):
        first = asyncio.create_task(self.queue.submit("one"))
        await self.entered.wait()
        second = asyncio.create_task(self.queue.submit("two"))
        await asyncio.sleep(0)
        await self.queue.close()
        results = await asyncio.gather(first, second, return_exceptions=True)
        self.assertTrue(all(isinstance(result, ReviewError) for result in results))
        await self.queue.queue.join()

    async def test_queue_capacity_and_idempotent_start(self):
        await self.queue.close()
        self.queue = ReviewQueue(self.queue.client, max_size=1)
        self.queue.start()
        self.queue.start()
        self.assertEqual(len(self.queue.tasks), 1)
        first = asyncio.create_task(self.queue.submit("one"))
        await self.entered.wait()
        second = asyncio.create_task(self.queue.submit("two"))
        await asyncio.sleep(0)
        with self.assertRaisesRegex(ReviewError, "已满"):
            await self.queue.submit("three")
        self.release.set()
        await asyncio.gather(first, second)


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = set()
        self.db = SimpleNamespace(
            claim_game_event=self.claim_game_event,
            claim_event=self.claim_event,
            complete_event=AsyncMock(),
            release_event=self.release_event,
        )
        self.game = GameRecord(
            "game",
            datetime(2026, 1, 2, tzinfo=UTC),
            datetime(2026, 1, 3, tzinfo=UTC),
            "四麻",
            [{"account_id": 42, "rank": 1, "score": 40000}],
            {},
        )
        self.sub = dict(
            group_id="1",
            qq_id="2",
            account_id=42,
            nickname="雀士",
            notify_start=1,
            notify_finish=1,
            auto_review=1,
            created_at="2026-01-01T00:00:00+08:00",
        )
        self.records = SimpleNamespace(records=AsyncMock(return_value=[self.game]))
        self.live = SimpleNamespace(enabled=False)
        self.monitor = GameMonitor(self.db, self.records, self.live)

    async def claim_event(self, uuid, subscriber, event):
        key = uuid, subscriber, event
        if key in self.events:
            return False
        self.events.add(key)
        return True

    async def claim_game_event(self, game, account_id, finished, subscriber):
        return await self.claim_event(game.uuid, subscriber, "finish" if finished else "start")

    async def release_event(self, uuid, subscriber, event):
        self.events.discard((uuid, subscriber, event))

    async def test_failed_send_retries_and_does_not_block_other_groups(self):
        send = AsyncMock(side_effect=[RuntimeError("QQ offline"), None, None])
        other = {**self.sub, "group_id": "3"}
        with self.assertLogs("pupulinbot.monitor", level="ERROR"):
            await self.monitor.poll([self.sub, other], send)
        self.assertEqual(send.await_count, 2)
        self.assertEqual(self.db.complete_event.await_count, 1)
        await self.monitor.poll([self.sub, other], send)
        self.assertEqual(send.await_count, 3)
        self.assertEqual(self.db.complete_event.await_count, 2)

    async def test_reviews_only_after_successful_new_settlement(self):
        send, review = AsyncMock(), AsyncMock()
        await self.monitor.poll([self.sub], send, review)
        await self.monitor.poll([self.sub], send, review)
        self.assertEqual(send.await_count, 1)
        self.assertEqual(review.await_count, 1)

    async def test_live_failure_does_not_block_settlement(self):
        self.live.enabled = True
        self.live.current_game = AsyncMock(side_effect=LiveServiceError("bad upstream"))
        send = AsyncMock()
        with self.assertLogs("pupulinbot.monitor", level="ERROR"):
            await self.monitor.poll([self.sub], send)
        send.assert_awaited_once()

    async def test_subscription_cutoff_respects_timezone(self):
        self.sub["created_at"] = "2026-01-02T01:00:00+08:00"
        send = AsyncMock()
        await self.monitor.poll([self.sub], send)
        send.assert_awaited_once()

    async def test_old_games_and_disabled_notifications_are_skipped(self):
        self.sub["created_at"] = "2026-01-04T00:00:00Z"
        send = AsyncMock()
        await self.monitor.poll([self.sub], send)
        send.assert_not_awaited()
        self.sub.update(created_at="2026-01-01", notify_finish=0)
        await self.monitor.poll([self.sub], send)
        send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
