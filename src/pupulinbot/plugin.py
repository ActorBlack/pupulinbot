import asyncio
import base64
from contextlib import suppress
from datetime import UTC, datetime

from nonebot import get_driver, on_command, require
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageSegment
from nonebot.params import CommandArg

from .config import settings
from .db import Database
from .formatting import finish_message, format_records, format_stats
from .live import LiveServiceClient, LiveServiceError
from .models import Player
from .paipuya import PaipuyaClient, PaipuyaError
from .review import MortalClient, ReviewError, ReviewQueue, extract_uuid, render_review

require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler  # noqa: E402

db = Database(settings.database)
paipuya = PaipuyaClient(settings.paipuya_base_url, settings.paipuya_timeout)
sanma = PaipuyaClient(settings.paipuya_sanma_base_url, settings.paipuya_timeout, "三麻")
live = LiveServiceClient(settings.live_api_url, settings.live_api_token)
reviews = ReviewQueue(
    MortalClient(settings.mortal_api_url, settings.mortal_api_token, settings.mortal_timeout),
    settings.review_workers,
)

bind_cmd = on_command("雀魂绑定", aliases={"绑定雀魂", "mj bind"}, priority=10, block=True)
unbind_cmd = on_command("雀魂解绑", aliases={"解绑雀魂"}, priority=10, block=True)
records_cmd = on_command("雀魂牌谱", aliases={"牌谱屋", "最近牌谱"}, priority=10, block=True)
monitor_cmd = on_command("雀魂监控", aliases={"开局提醒"}, priority=10, block=True)
review_cmd = on_command("mortal", aliases={"Mortal", "牌谱分析"}, priority=10, block=True)
help_cmd = on_command("麻将帮助", aliases={"雀魂帮助"}, priority=20, block=True)
stats_cmd = on_command("雀魂统计", aliases={"雀魂信息", "mj stats"}, priority=10, block=True)
rank_cmd = on_command("群雀魂排行", aliases={"雀魂排行"}, priority=10, block=True)


@get_driver().on_startup
async def startup() -> None:
    await db.initialize()
    reviews.start()


@get_driver().on_shutdown
async def shutdown() -> None:
    await reviews.close()


@bind_cmd.handle()
async def bind(event: GroupMessageEvent, args=CommandArg()):
    query = args.extract_plain_text().strip()
    if not query:
        await bind_cmd.finish("用法：雀魂绑定 <UID或昵称>")
    try:
        player = await paipuya.resolve(query)
        await db.bind(str(event.user_id), player.account_id, player.nickname)
        await bind_cmd.finish(f"绑定成功：{player.nickname}（{player.account_id}）")
    except PaipuyaError as exc:
        await bind_cmd.finish(str(exc))


@unbind_cmd.handle()
async def unbind(event: GroupMessageEvent):
    await unbind_cmd.finish(
        "已解除绑定。" if await db.unbind(str(event.user_id)) else "你还没有绑定雀魂 UID。"
    )


@records_cmd.handle()
async def records(event: GroupMessageEvent, args=CommandArg()):
    query = args.extract_plain_text().strip()
    try:
        if query:
            player = await paipuya.resolve(query)
        else:
            row = await db.binding(str(event.user_id))
            if not row:
                await records_cmd.finish("请先发送：雀魂绑定 <UID或昵称>")
            player = Player(row["account_id"], row["nickname"])
        games = await paipuya.records(player.account_id, 5)
        await records_cmd.finish(format_records(games, player.account_id))
    except PaipuyaError as exc:
        await records_cmd.finish(str(exc))


@stats_cmd.handle()
async def stats(event: GroupMessageEvent, args=CommandArg()):
    parts = args.extract_plain_text().strip().split()
    use_sanma = bool(parts and parts[-1].lower() in {"三麻", "3", "sanma"})
    if use_sanma:
        parts.pop()
    client = sanma if use_sanma else paipuya
    query = " ".join(parts)
    try:
        if query:
            player = await client.resolve(query)
        else:
            row = await db.binding(str(event.user_id))
            if not row:
                await stats_cmd.finish("请先发送：雀魂绑定 <UID或昵称>")
            player = Player(row["account_id"], row["nickname"])
        summary = await client.stats(player.account_id)
        await stats_cmd.finish(format_stats(player.nickname, summary))
    except PaipuyaError as exc:
        await stats_cmd.finish(str(exc))


@rank_cmd.handle()
async def group_rank(event: GroupMessageEvent, args=CommandArg()):
    mode = args.extract_plain_text().strip().lower()
    client = sanma if mode in {"三麻", "3", "sanma"} else paipuya
    bindings = await db.group_bindings(str(event.group_id))
    if not bindings:
        await rank_cmd.finish("本群还没有已开启监控的雀魂玩家。")

    async def load(row):
        try:
            return row, await client.stats(row["account_id"])
        except PaipuyaError:
            return row, None

    loaded = await asyncio.gather(*(load(row) for row in bindings))
    available = [(row, value) for row, value in loaded if value and value.games]
    available.sort(key=lambda item: (item[1].average_rank or 99, -item[1].games))
    if not available:
        await rank_cmd.finish("牌谱屋暂时没有本群玩家的统计数据。")
    lines = [f"本群{client.mode}排行（按平均顺位）"]
    for index, (row, value) in enumerate(available[:20], 1):
        lines.append(f"{index}. {row['nickname']}｜{value.average_rank:.2f}｜{value.games}场")
    await rank_cmd.finish("\n".join(lines))


@monitor_cmd.handle()
async def monitor(event: GroupMessageEvent, args=CommandArg()):
    option = args.extract_plain_text().strip().lower()
    enabled = option not in {"关", "关闭", "off", "0"}
    auto_review = any(word in option for word in {"分析", "复盘", "review", "mortal"})
    row = await db.binding(str(event.user_id))
    if not row:
        await monitor_cmd.finish("请先绑定雀魂 UID。")
    if enabled:
        await db.subscribe(str(event.group_id), str(event.user_id), auto_review)
        # Establish a baseline so enabling monitoring does not announce an old game.
        with suppress(PaipuyaError):
            latest = await paipuya.records(row["account_id"], 1)
            if latest:
                game = latest[0]
                subscriber = f"{event.group_id}:{event.user_id}"
                await db.claim_game_event(game, row["account_id"], bool(game.ended_at), subscriber)
    else:
        await db.unsubscribe(str(event.group_id), str(event.user_id))
    if enabled:
        suffix = "，并将在结算后自动复盘。" if auto_review else "。"
        await monitor_cmd.finish(f"本群开局/结算监控已开启{suffix}")
    await monitor_cmd.finish("本群监控已关闭。")


@review_cmd.handle()
async def mortal(event: GroupMessageEvent, args=CommandArg()):
    text = args.extract_plain_text().strip()
    if not text:
        await review_cmd.finish("用法：mortal <雀魂牌谱链接或UUID> [座位0-3]")
    parts = text.split()
    try:
        uuid = extract_uuid(parts[0])
        seat = int(parts[1]) if len(parts) > 1 else None
        if seat is not None and seat not in range(4):
            raise ValueError("座位必须是 0、1、2 或 3")
        await review_cmd.send("已加入 Mortal 分析队列，请稍候……")
        result = await reviews.submit(f"https://game.maj-soul.com/1/?paipu={uuid}", seat)
        image = base64.b64encode(render_review(result, str(event.user_id), uuid)).decode()
        message = MessageSegment.image(f"base64://{image}")
        if result.details_url:
            message += MessageSegment.text(f"\n详细报告：{result.details_url}")
        await review_cmd.finish(message)
    except (ValueError, ReviewError) as exc:
        await review_cmd.finish(str(exc))


@help_cmd.handle()
async def help_handler():
    await help_cmd.finish(
        "麻将命令\n雀魂绑定 <UID/昵称>｜雀魂解绑\n牌谱屋 [UID/昵称]\n"
        "雀魂统计 [UID/昵称] [三麻]\n群雀魂排行 [三麻]\n"
        "雀魂监控 开 [自动分析] / 关（群聊）\n"
        "mortal <牌谱链接/UUID> [座位0-3]"
    )


async def send_auto_review(bot: Bot, group_id: int, nickname: str, game, account_id: int):
    try:
        seat = next(
            (
                index
                for index, player in enumerate(game.players)
                if int(player.get("account_id", -1)) == account_id
            ),
            None,
        )
        result = await reviews.submit(game.url, seat)
        encoded = base64.b64encode(render_review(result, nickname, game.uuid)).decode()
        await bot.send_group_msg(
            group_id=group_id, message=MessageSegment.image(f"base64://{encoded}")
        )
    except ReviewError as exc:
        await bot.send_group_msg(group_id=group_id, message=f"Mortal 自动分析失败：{exc}")


@scheduler.scheduled_job(
    "interval", seconds=settings.monitor_interval, id="pupulin_monitor", max_instances=1
)
async def poll_games() -> None:
    bot = next(iter(get_driver().bots.values()), None)
    if not isinstance(bot, Bot):
        return
    for sub in await db.subscriptions():
        subscriber = f"{sub['group_id']}:{sub['qq_id']}"
        if live.enabled:
            try:
                current = await live.current_game(sub["account_id"])
                if current and await db.claim_event(current.uuid, subscriber, "start"):
                    await bot.send_group_msg(
                        group_id=int(sub["group_id"]),
                        message=f"🀄 {sub['nickname']} 开局了：{current.mode}\n{current.url}",
                    )
            except LiveServiceError:
                pass
        with suppress(PaipuyaError):
            games = await paipuya.records(sub["account_id"], settings.monitor_page_size)
            if not games:
                continue
            # Process oldest first so temporary downtime does not lose settlements.
            for game in reversed(games):
                subscribed_at = datetime.fromisoformat(sub["created_at"]).replace(tzinfo=UTC)
                if game.started_at < subscribed_at:
                    continue
                # Public 牌谱屋 data appears after settlement, so this reliably supplies
                # settlement notifications. Start notifications require an upstream that
                # exposes an unfinished record and are emitted whenever ended_at is absent.
                if game.ended_at:
                    if await db.claim_game_event(game, sub["account_id"], True, subscriber):
                        await bot.send_group_msg(
                            group_id=int(sub["group_id"]),
                            message=finish_message(sub["nickname"], game, sub["account_id"]),
                        )
                        if settings.mortal_api_url and sub["auto_review"]:
                            asyncio.create_task(
                                send_auto_review(
                                    bot,
                                    int(sub["group_id"]),
                                    sub["nickname"],
                                    game,
                                    sub["account_id"],
                                ),
                                name=f"auto-review-{game.uuid}",
                            )
                elif not live.enabled and await db.claim_game_event(
                    game, sub["account_id"], False, subscriber
                ):
                    await bot.send_group_msg(
                        group_id=int(sub["group_id"]),
                        message=f"🀄 {sub['nickname']} 开局了：{game.url}",
                    )
        await asyncio.sleep(0.3)
