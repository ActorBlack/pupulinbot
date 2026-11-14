"""Group-scoped small-round notifications for the public Mahjong Soul observer."""

import asyncio
import logging
import time
from contextlib import aclosing
from dataclasses import dataclass, field

from .live import LiveServiceError
from .majsoul_observe import observe, watch_uuid
from .majsoul_rounds import MajsoulRoundResults, RoundNotice
from .ms_options import MODE_NAMES, TZ
from .spectator import parse_head

logger = logging.getLogger(__name__)
STOPPED = RoundNotice("unavailable", "雀魂观战已中断，逐局播报已停止。")


@dataclass(slots=True)
class Subscription:
    owner: str
    joined_at: float = field(default_factory=time.time)


@dataclass(slots=True)
class FollowedGame:
    uuid: str
    groups: dict[str, Subscription] = field(default_factory=dict)
    task: asyncio.Task | None = None
    room: str = ""
    delay: int = 0


class MajsoulLiveWatch:
    def __init__(self, db, browser, send):
        self.db, self.browser, self.send = db, browser, send
        self.tables = {}
        self.lock = asyncio.Lock()

    def current(self, group):
        return next((table for table in self.tables.values() if group in table.groups), None)

    def status(self, group):
        table = self.current(group)
        if table is None:
            return "本群未开启雀魂逐局播报。"
        return f"雀魂逐局播报已开启（官方延迟 {table.delay} 秒）\n{table.room}\n{table.uuid}"

    async def find(self, user, value):
        if not value:
            bound = await self.db.binding(user)
            if not bound:
                raise LiveServiceError("请发送 mslive find <雀魂昵称或 account ID>，或先绑定账号。")
            account, label = bound["account_id"], bound["nickname"]
        else:
            account = int(value) if value.isdecimal() and len(value) <= 10 else None
            label = value

        current = await self.browser.public_games()
        games = self.browser.recently_listed_games() | current
        game = games.get(account)
        if value and game is None:
            matches = [
                player_id
                for player_id, table in games.items()
                if any(
                    player["account_id"] == player_id and player["nickname"] == value
                    for player in table.players
                )
            ]
            if len(matches) > 1:
                choices = "、".join(str(player_id) for player_id in sorted(matches))
                return f"公开观战列表里有同名玩家，请改用 account ID：{choices}"
            if matches:
                account = matches[0]
                game = games[account]
        if game is None:
            return f"{label}：当前公开观战列表未找到对局。"
        if watch_uuid(game.uuid) != game.uuid:
            raise LiveServiceError("雀魂观战返回的对局编号异常，请稍后重试。")

        from_index = account not in current
        if from_index:
            authorization = await self.browser.observation(game.uuid)
            async with observe(authorization) as (head, _):
                verified = parse_head(head)
                if (
                    verified is None
                    or verified.uuid != game.uuid
                    or not any(p["account_id"] == account for p in verified.players)
                ):
                    raise LiveServiceError("最近扫到的对局暂无法确认，请稍后重试。")
                game = verified
        name = next(
            player["nickname"] for player in game.players if player["account_id"] == account
        )
        room = MODE_NAMES.get(game.mode, "雀魂")
        if len(game.players) == 4:
            room = "四麻" + room
        start = game.started_at.astimezone(TZ)
        state = "的对局可观战" if from_index else " 正在对局"
        return f"{name}{state}\n{room}　{start:%m-%d %H:%M} 开始\nmslive {game.uuid}"

    async def follow(self, group, user, value):
        try:
            uuid = watch_uuid(value)
        except ValueError:
            uuid = None
        if uuid is None:
            raise LiveServiceError("用法：mslive <对局编号或链接> / find [玩家] / status / off")
        async with self.lock:
            current = self.current(group)
            if current:
                if current.uuid == uuid:
                    return self.status(group)
                raise LiveServiceError("本群已有雀魂逐局播报，请先发送 mslive off。")
            table = self.tables.get(uuid)
            if table:
                table.groups[group] = Subscription(user)
                return self.status(group)
            authorization = await self.browser.observation(uuid)
            ready = asyncio.get_running_loop().create_future()
            table = FollowedGame(uuid, delay=authorization.delay)
            table.task = asyncio.create_task(self._run(table, authorization, group, user, ready))
            self.tables[uuid] = table
            try:
                await ready
            except BaseException:
                table.task.cancel()
                await asyncio.gather(table.task, return_exceptions=True)
                raise
            return self.status(group)

    async def stop(self, group, user, *, manager=False):
        async with self.lock:
            table = self.current(group)
            if table is None:
                return self.status(group)
            if table.groups[group].owner != user and not manager:
                raise LiveServiceError("只有开启者或管理员可以停止本群雀魂逐局播报。")
            del table.groups[group]
            if not table.groups:
                table.task.cancel()
                await asyncio.gather(table.task, return_exceptions=True)
            return "已停止本群雀魂逐局播报。"

    async def _deliver(self, group, table, notice):
        subscriber = f"majsoul:round:group:{group}"
        event = f"round:{notice.key}"
        if not await self.db.claim_event(table.uuid, subscriber, event):
            return
        try:
            if group not in table.groups:
                await self.db.release_event(table.uuid, subscriber, event)
                return
            async with asyncio.timeout(30):
                await self.send(group, notice.text)
        except BaseException:
            await self.db.release_event(table.uuid, subscriber, event)
            raise
        await self.db.complete_event(table.uuid, subscriber, event)

    async def _broadcast(self, table, notice, *, released_at=None):
        async def send(group):
            for delay in (0, 2):
                if delay:
                    await asyncio.sleep(delay)
                if group not in table.groups:
                    return
                try:
                    await self._deliver(group, table, notice)
                    return
                except Exception:
                    logger.warning("雀魂逐局播报发送失败：group=%s", group)

        await asyncio.gather(
            *(
                send(group)
                for group in tuple(table.groups)
                if released_at is None or table.groups[group].joined_at <= released_at
            )
        )

    async def _run(self, table, authorization, group, user, ready):
        try:
            async with asyncio.timeout(3 * 60 * 60):
                await self._stream(table, authorization, group, user, ready)
        except (LiveServiceError, ValueError, TypeError, KeyError, IndexError, TimeoutError) as exc:
            if not ready.done():
                ready.set_exception(
                    exc
                    if isinstance(exc, LiveServiceError)
                    else LiveServiceError("雀魂观战数据异常。")
                )
            else:
                await self._broadcast(table, STOPPED)
        finally:
            if self.tables.get(table.uuid) is table:
                self.tables.pop(table.uuid)

    async def _stream(self, table, authorization, group, user, ready):
        async with observe(authorization) as (head, stream):
            if head.get("uuid") != table.uuid or not head.get("seat_list"):
                raise LiveServiceError("雀魂观战对局或座位与请求不一致。")
            game = parse_head(head)
            if game is None:
                raise LiveServiceError("该对局不在支持的雀魂公开段位场范围内。")
            if any(not player.get("nickname") for player in head["players"]):
                raise LiveServiceError("雀魂公开观战未返回完整的座位昵称。")
            table.room = MODE_NAMES.get(game.mode, "雀魂")
            table.groups[group] = Subscription(user)
            ready.set_result(None)
            results = MajsoulRoundResults(game)
            await stream.start(authorization)
            async with aclosing(stream.events()) as events:
                async for _, stamp, name, data in events:
                    notice = results.feed(name, data)
                    if notice:
                        when = authorization.start + authorization.delay + stamp / 1000
                        if any(sub.joined_at <= when for sub in table.groups.values()):
                            await asyncio.sleep(max(0, when - time.time()))
                            await self._broadcast(table, notice, released_at=when)
                    if results.finished or name == "GameEndAction":
                        return
            raise LiveServiceError("雀魂观战流提前结束。")

    async def close(self):
        tasks = [table.task for table in self.tables.values() if table.task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
