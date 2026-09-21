import asyncio
import base64
from contextlib import suppress

from nonebot import get_driver, on_command, require
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageSegment
from nonebot.params import CommandArg

from .config import settings
from .db import Database
from .formatting import finish_message, format_records
from .paipuya import PaipuyaClient, PaipuyaError
from .review import MortalClient, ReviewError, ReviewQueue, extract_uuid, render_review

require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler  # noqa: E402

db = Database(settings.database)
paipuya = PaipuyaClient(settings.paipuya_base_url, settings.paipuya_timeout)
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
            player = type(
                "Player", (), {"account_id": row["account_id"], "nickname": row["nickname"]}
            )()
        games = await paipuya.records(player.account_id, 5)
        await records_cmd.finish(format_records(games, player.account_id))
    except PaipuyaError as exc:
        await records_cmd.finish(str(exc))


@monitor_cmd.handle()
async def monitor(event: GroupMessageEvent, args=CommandArg()):
    enabled = args.extract_plain_text().strip().lower() not in {"关", "关闭", "off", "0"}
    row = await db.binding(str(event.user_id))
    if not row:
        await monitor_cmd.finish("请先绑定雀魂 UID。")
    if enabled:
        await db.subscribe(str(event.group_id), str(event.user_id))
    else:
        await db.unsubscribe(str(event.group_id), str(event.user_id))
    await monitor_cmd.finish("本群开局/结算监控已开启。" if enabled else "本群监控已关闭。")


@review_cmd.handle()
async def mortal(event: GroupMessageEvent, args=CommandArg()):
    text = args.extract_plain_text().strip()
    if not text:
        await review_cmd.finish("用法：mortal <雀魂牌谱链接或UUID> [座位0-3]")
    parts = text.split()
    try:
        uuid = extract_uuid(parts[0])
        seat = int(parts[1]) if len(parts) > 1 else None
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
        "雀魂监控 开/关（群聊）\nmortal <牌谱链接/UUID> [座位0-3]"
    )


@scheduler.scheduled_job(
    "interval", seconds=settings.monitor_interval, id="pupulin_monitor", max_instances=1
)
async def poll_games() -> None:
    bot = next(iter(get_driver().bots.values()), None)
    if not isinstance(bot, Bot):
        return
    for sub in await db.subscriptions():
        with suppress(PaipuyaError):
            games = await paipuya.records(sub["account_id"], 1)
            if not games:
                continue
            game = games[0]
            # Public 牌谱屋 data appears after settlement, so this reliably supplies
            # settlement notifications. Start notifications require an upstream that
            # exposes an unfinished record and are emitted whenever ended_at is absent.
            if game.ended_at:
                subscriber = f"{sub['group_id']}:{sub['qq_id']}"
                if await db.claim_game_event(game, sub["account_id"], True, subscriber):
                    await bot.send_group_msg(
                        group_id=int(sub["group_id"]),
                        message=finish_message(sub["nickname"], game, sub["account_id"]),
                    )
                    if settings.mortal_api_url:
                        try:
                            result = await reviews.submit(game.url)
                            encoded = base64.b64encode(
                                render_review(result, sub["nickname"], game.uuid)
                            ).decode()
                            await bot.send_group_msg(
                                group_id=int(sub["group_id"]),
                                message=MessageSegment.image(f"base64://{encoded}"),
                            )
                        except ReviewError as exc:
                            await bot.send_group_msg(
                                group_id=int(sub["group_id"]), message=f"Mortal 自动分析失败：{exc}"
                            )
            else:
                subscriber = f"{sub['group_id']}:{sub['qq_id']}"
                if await db.claim_game_event(game, sub["account_id"], False, subscriber):
                    await bot.send_group_msg(
                        group_id=int(sub["group_id"]),
                        message=f"🀄 {sub['nickname']} 开局了：{game.url}",
                    )
        await asyncio.sleep(0.3)
