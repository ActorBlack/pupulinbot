import asyncio
import logging
from datetime import UTC, datetime

from .formatting import finish_message

logger = logging.getLogger(__name__)


class GameMonitor:
    """Poll subscriptions independently and acknowledge only successful deliveries."""

    def __init__(
        self,
        db,
        records_client,
        live_client,
        page_size: int = 20,
        sanma_client=None,
        catchup_limit=200,
        notification_builder=None,
        build_timeout=150,
        send_timeout=95,
        notify_starts=True,
    ):
        self.db = db
        self.records = records_client
        self.live = live_client
        self.page_size = page_size
        self.sanma = sanma_client
        self.catchup_limit = max(page_size, catchup_limit)
        self.anchors = {}
        self.notification_builder = notification_builder
        self.build_timeout = build_timeout
        self.send_timeout = send_timeout
        self.notify_starts = notify_starts

    async def _deliver(self, sub, game, event, message, send, *, live=False) -> bool:
        subscriber = sub.get("subscriber") or f"{sub['group_id']}:{sub['qq_id']}"
        if live:
            claimed = await self.db.claim_event(game.uuid, subscriber, event)
        else:
            claimed = await self.db.claim_game_event(
                game, sub["account_id"], event == "finish", subscriber
            )
        if not claimed:
            return False
        try:
            if self.notification_builder:
                async with asyncio.timeout(self.build_timeout):
                    message = await self.notification_builder(sub, game, event, message)
            async with asyncio.timeout(self.send_timeout):
                await send(sub, message)
        except BaseException:
            await self.db.release_event(game.uuid, subscriber, event)
            raise
        await self.db.complete_event(game.uuid, subscriber, event)
        return True

    @staticmethod
    def _since(sub, event):
        value = sub.get(event + "_since") or sub["created_at"]
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)

    @staticmethod
    def _live_mode(game, mode):
        if len(game.players) in {3, 4}:
            return len(game.players) == mode
        if game.mode in {"21", "22", "23", "24", "25", "26"} or "三" in game.mode:
            return mode == 3
        # Keep legacy four-player gateways working; unknown modes cannot imply sanma.
        return mode == 4

    async def _health(self, sub, error=None, capped=False):
        if hasattr(self.db, "set_monitor_health"):
            key = sub.get("subscriber") or f"{sub['group_id']}:{sub['qq_id']}"
            try:
                await self.db.set_monitor_health(key, datetime.now(UTC).isoformat(), error, capped)
            except Exception:
                logger.exception("监控状态保存失败：group=%s", sub["group_id"])

    async def poll(self, subscriptions, send, review=None) -> None:
        subscriptions = [dict(s) for s in subscriptions]
        records_cache, live_cache = {}, {}
        cutoffs = {}
        for sub in subscriptions:
            try:
                key = (int(sub.get("mode", 4)), sub["account_id"])
                value = self._since(sub, "finish")
                cutoffs[key] = min(cutoffs.get(key, value), value)
            except (ValueError, TypeError):
                continue
        self.anchors = {key: value for key, value in self.anchors.items() if key in cutoffs}

        async def load_records(client, key):
            games = await client.records(key[1], self.page_size)
            cutoff = cutoffs[key]
            previous = self.anchors.get(key)
            seen = (
                previous is not None
                and previous[1] == cutoff
                and any(g.uuid == previous[0] for g in games)
            )
            capped = False
            boundary_failed = False
            if (
                not seen
                and len(games) >= self.page_size
                and games
                and min(g.started_at for g in games) >= cutoff
            ):
                if hasattr(client, "history"):
                    games, capped = await client.history(key[1], self.catchup_limit, start=cutoff)
                    if not capped:
                        # History filters by start time. Also check the match that
                        # could have been in progress when monitoring was enabled.
                        try:
                            previous_games, _ = await client.history(key[1], 1, end=cutoff)
                        except Exception:
                            boundary_failed = True
                            logger.exception("监控起点补查失败：mode=%s account=%s", *key)
                        else:
                            for game in previous_games:
                                if game.ended_at and game.started_at < cutoff <= game.ended_at:
                                    if len(games) < self.catchup_limit:
                                        games = [*games, game]
                                    else:
                                        capped = True
                else:
                    capped = True
            if boundary_failed:
                self.anchors.pop(key, None)
            elif games and not capped:
                self.anchors[key] = (max(games, key=lambda g: g.started_at).uuid, cutoff)
            return games, capped, boundary_failed

        async def cached(cache, key, load):
            if key not in cache:
                try:
                    cache[key] = (await load(), None)
                except Exception as exc:
                    cache[key] = (None, exc)
            value, error = cache[key]
            if error:
                raise error
            return value

        for value in subscriptions:
            sub = dict(value)
            mode = int(sub.get("mode", 4))
            if self.notify_starts and sub["notify_start"] and self.live.enabled:
                try:
                    current = await cached(
                        live_cache,
                        sub["account_id"],
                        lambda account=sub["account_id"]: self.live.current_game(account),
                    )
                    if (
                        current
                        and sub["notify_start"]
                        and self._live_mode(current, mode)
                        and current.started_at >= self._since(sub, "start")
                    ):
                        await self._deliver(
                            sub,
                            current,
                            "start",
                            f"🀄 {sub['nickname']} 开局了：{current.mode}\n{current.url}",
                            send,
                            live=True,
                        )
                except Exception:
                    logger.exception("实时监控失败：group=%s qq=%s", sub["group_id"], sub["qq_id"])
            try:
                client = self.sanma if mode == 3 else self.records
                if client is None:
                    raise RuntimeError("三麻结算数据源未配置")
                games, capped, boundary_failed = await cached(
                    records_cache,
                    (mode, sub["account_id"]),
                    lambda client=client, key=(mode, sub["account_id"]): load_records(client, key),
                )
                subscribed_at = self._since(sub, "finish")
            except Exception:
                logger.exception("结算查询失败：group=%s qq=%s", sub["group_id"], sub["qq_id"])
                await self._health(sub, "结算查询失败，请稍后重试或检查数据源连接。")
                continue
            delivery_error = (
                "监控开启时的对局补查失败，后续轮询将重试。" if boundary_failed else None
            )
            for game in sorted(games, key=lambda g: g.started_at):
                # Indexed partial records do not establish a genuine live start.
                if not game.ended_at or not sub["notify_finish"] or game.ended_at < subscribed_at:
                    continue
                message = finish_message(sub["nickname"], game, sub["account_id"])
                try:
                    delivered = await self._deliver(sub, game, "finish", message, send)
                    if (
                        delivered
                        and sub["auto_review"]
                        and review
                        and mode == 4
                        and game.started_at >= self._since(sub, "review")
                    ):
                        await review(sub, game)
                except Exception:
                    delivery_error = "通知或分析处理失败；未确认通知会在后续轮询重试。"
                    self.anchors.pop((mode, sub["account_id"]), None)
                    logger.exception(
                        "通知处理失败：group=%s qq=%s uuid=%s",
                        sub["group_id"],
                        sub["qq_id"],
                        game.uuid,
                    )
            await self._health(sub, delivery_error, capped)
