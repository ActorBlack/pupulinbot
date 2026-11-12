import asyncio
import base64
import logging
import re
from collections import deque
from datetime import datetime
from random import randint

import httpx
from nonebot import get_driver, on_command, require
from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
)
from nonebot.message import event_preprocessor, run_preprocessor
from nonebot.params import CommandArg
from nonebot.typing import T_State

from .batch_chart import render_batch_chart
from .batch_limits import MAX_BATCH_COUNT, batch_count
from .batch_review import STATUS_LABELS, BatchReviews, summaries, upload_report
from .config import settings
from .db import Database
from .formatting import format_stats
from .group_policy import (
    MONITOR_COMMANDS,
    MORTAL_COMMANDS,
    check_group_command,
    command_name,
)
from .help_menu import BATCH_ACTIONS, send_help
from .live import LiveServiceClient
from .models import Player
from .monitor import GameMonitor
from .monitor_card import collect_profiles, render_monitor_card
from .ms_commands import MahjongQueries, install_commands
from .ms_ids import friend_to_account
from .ms_options import parse_options
from .ms_rank import group_ranking
from .ms_style import style_lines
from .ms_watch import WatchCommands
from .online_pool import OnlinePool
from .paipuya import PaipuyaClient, PaipuyaError
from .paipuya_auth import PaipuyaAccess
from .promotion import promotion_line
from .qq_delivery import send_ranking_forward, send_verified_image
from .qq_members import GroupMembersError, group_member_ids
from .record_names import RecordNames
from .repeat_stop import GROUP_ID as REPEAT_STOP_GROUP
from .repeat_stop import RepeatStop
from .review import MortalClient, ReviewError, ReviewQueue, parse_review_link, render_review
from .review_cache import ReviewCache
from .spectator import BrowserSpectatorClient
from .tenhou import TenhouClient
from .tenhou_commands import install_tenhou_commands
from .tenhou_live import TenhouLiveWatch
from .tenhou_monitor import TenhouMonitor, TenhouStartNotice, TenhouStore

require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler  # noqa: E402

superusers = frozenset(get_driver().config.superusers)
MORTAL_PAUSE_ALERT_GROUP = 1131357545
PHRASE_REWRITE_GROUPS = frozenset({REPEAT_STOP_GROUP, 996147537})
logger = logging.getLogger(__name__)
repeat_stop = RepeatStop()
replied_message_ids: deque[tuple[int, int]] = deque(maxlen=64)


@event_preprocessor
async def handle_group_reactions(bot: Bot, event: Event):
    if not isinstance(event, GroupMessageEvent):
        return
    await repeat_stop.handle(bot, event)
    key = (event.group_id, event.message_id)
    if event.user_id == event.self_id or key in replied_message_ids:
        return
    original = event.original_message
    is_plain_text = all(segment.type == "text" for segment in original)
    text = original.extract_plain_text() if is_plain_text else ""
    fssz = re.fullmatch(r"fssz(?: ([1-9]|[12][0-9]|3[0-2]))?", text)
    if fssz:
        limit = fssz[1]
        prefix = "发四是巨大猪" if limit else "发四是巨大"
        count = randint(1, int(limit) if limit else 6)
        message = Message(MessageSegment.text(prefix + "🐷" * count))
    elif text == "alsz":
        roll = randint(1, 100)
        if roll <= 45:
            reply = "阿落是小🐷"
        elif roll <= 90:
            reply = "阿落不是小🐷"
        else:
            reply = "泥才是小🐷"
        message = Message(MessageSegment.text(reply))
    elif event.group_id in PHRASE_REWRITE_GROUPS:
        message = original.copy()
        message.reduce()
        changed = False
        for segment in message["text"]:
            if "好厉害" in segment.data["text"]:
                segment.data["text"] = segment.data["text"].replace("好厉害", "井棋风")
                changed = True
        if not changed:
            return
    else:
        return
    replied_message_ids.append(key)
    try:
        await bot.send_group_msg(group_id=event.group_id, message=message)
    except Exception:
        logger.exception("发送群消息自动回复失败")


@run_preprocessor
async def restrict_group_commands(event: Event, state: T_State):
    if not isinstance(event, GroupMessageEvent):
        return
    user = str(event.user_id)
    command = command_name(state)
    restricted = str(event.group_id) in settings.monitor_only_groups
    vip2 = (
        user not in superusers
        and (command in MONITOR_COMMANDS or (restricted and command in MORTAL_COMMANDS))
        and await db.is_vip2(user)
    )
    check_group_command(
        event,
        state,
        settings.monitor_only_groups,
        superusers=superusers,
        monitor_owner=settings.monitor_owner,
        vip2=vip2,
    )


db = Database(settings.database, legacy_follow_group=settings.legacy_follow_group)
tenhou_client = TenhouClient(history_directory=settings.database.parent / "tenhou-history")
tenhou_store = TenhouStore(db)
tenhou_monitor = TenhouMonitor(tenhou_store, tenhou_client)


async def send_tenhou_message(group, message):
    bot = next(iter(get_driver().bots.values()), None)
    if not isinstance(bot, Bot):
        raise ConnectionError("OneBot is disconnected")
    if isinstance(message, TenhouStartNotice):
        picture = MessageSegment.image(
            "base64://" + base64.b64encode(message.picture).decode("ascii")
        )
        payload = Message([MessageSegment.text(message.text + "\n"), picture])
        await send_verified_image(bot, group, payload)
    else:
        # Public nicknames are text and can contain strings resembling CQ codes.
        payload = Message(MessageSegment.text(message))
        await bot.send_group_msg(group_id=int(group), message=payload, _timeout=90)


tenhou_rounds = TenhouLiveWatch(tenhou_store, tenhou_client, tenhou_monitor, send_tenhou_message)
install_tenhou_commands(
    tenhou_store,
    tenhou_client,
    tenhou_monitor,
    tenhou_rounds,
    manager=settings.monitor_owner,
    superusers=superusers,
)
paipuya_access = PaipuyaAccess(
    settings.paipuya_api_token,
    auto_cap=settings.paipuya_auto_cap,
    endpoint=settings.cap_endpoint,
    cache_dir=settings.cap_cache_dir,
    workers=settings.cap_workers,
    timeout=settings.cap_timeout,
    library=settings.hashwx_library,
    rate_limit_file=settings.paipuya_rate_limit_file,
)
paipuya = PaipuyaClient(settings.paipuya_base_url, settings.paipuya_timeout, access=paipuya_access)
sanma = PaipuyaClient(
    settings.paipuya_sanma_base_url, settings.paipuya_timeout, "三麻", access=paipuya_access
)
live = (
    LiveServiceClient(settings.live_api_url, settings.live_api_token)
    if settings.live_api_url
    else BrowserSpectatorClient(
        settings.spectator_browser_profile, enabled=settings.spectator_browser
    )
)
reviews = ReviewQueue(
    MortalClient(settings.mortal_api_url, settings.mortal_api_token, settings.mortal_timeout),
    settings.review_workers,
    settings.review_queue_size,
)
online = (
    OnlinePool(
        settings.mortal_browser_profile,
        settings.mortal_online_model,
        settings.mortal_online_cooldown,
        settings.mortal_online_timeout,
        cache=ReviewCache(
            settings.batch_directory.with_name("mortal-cache"),
            batch_directory=settings.batch_directory,
        ),
        options={
            "retries": settings.mortal_online_retries,
            "headless": settings.mortal_online_headless,
            "proxy": settings.mortal_online_proxy,
            "save_html": settings.mortal_save_html,
            "save_screenshot": settings.mortal_save_screenshot,
        },
    )
    if settings.mortal_online
    else None
)


async def mortal_allowed(group_id, owner):
    return (
        str(group_id) not in settings.monitor_only_groups
        or str(owner) in superusers
        or bool(owner and await db.is_vip2(str(owner)))
    )


def mention_requester(owner, result):
    qq = str(owner)
    if qq.isascii() and qq.isdecimal() and int(qq) > 0:
        return MessageSegment.at(int(qq)) + MessageSegment.text("\n") + result
    return result


async def notify_batch(job):
    status = job.get("status")
    if status == "retry_wait":
        if job.get("retry_phase") == "verification":
            scheduler.add_job(
                retry_mortal_jobs,
                "date",
                run_date=datetime.fromisoformat(job["next_retry_at"]),
                id=f"pupulin_mortal_retry_{job['id']}",
                replace_existing=True,
            )
        return
    verification_paused = status == "verification_paused"
    if not verification_paused and not await mortal_allowed(job["group_id"], job.get("owner")):
        return
    bot = next(iter(get_driver().bots.values()), None)
    if not isinstance(bot, Bot):
        return
    if status == "done":
        try:
            picture = await asyncio.to_thread(render_batch_chart, job)
            await bot.send_group_msg(
                group_id=int(job["group_id"]),
                message=mention_requester(
                    job.get("owner"),
                    MessageSegment.image("base64://" + base64.b64encode(picture).decode("ascii")),
                ),
            )
        except Exception:
            logger.exception("Mortal 结果图生成或发送失败：job=%s", job["id"])
            await bot.send_group_msg(
                group_id=int(job["group_id"]),
                message=mention_requester(
                    job.get("owner"),
                    MessageSegment.text(
                        f"Mortal 任务 {job['id']}：结果图生成或发送失败，分析结果已保存。\n"
                        f"发送 mortal report {job['id']} 重试。"
                    ),
                ),
            )
        return
    done = sum(row["status"] == "done" for row in job["rows"])
    text = (
        f"Mortal 任务 {job['id']}：{STATUS_LABELS.get(status, status)}，"
        f"完成 {done}/{len(job['rows'])} 场。"
    )
    if job.get("message"):
        text += "\n" + job["message"]
    group_id = MORTAL_PAUSE_ALERT_GROUP if verification_paused else int(job["group_id"])
    await bot.send_group_msg(group_id=group_id, message=text)


batches = BatchReviews(
    settings.batch_directory,
    paipuya,
    reviews,
    online,
    notify_batch,
    superusers=superusers,
    name_resolver=RecordNames(
        db, archive_directory=settings.batch_directory, clients={4: paipuya, 3: sanma}
    ),
)


@scheduler.scheduled_job("interval", seconds=15, id="pupulin_mortal_retry", max_instances=1)
async def retry_mortal_jobs():
    await batches.retry_due(allowed=mortal_allowed)


async def build_monitor_notification(sub, game, event, fallback):
    congratulations = ""
    try:
        client = sanma if int(sub.get("mode", 4)) == 3 else paipuya
        if event == "finish":
            profiles, congratulations = await asyncio.gather(
                collect_profiles(client, game),
                promotion_line(client, game, sub["account_id"]),
            )
        else:
            profiles = await collect_profiles(client, game)
        picture = await asyncio.to_thread(render_monitor_card, game, event, profiles)
        return MessageSegment.image("base64://" + base64.b64encode(picture).decode("ascii")) + (
            "\n" + fallback + ("\n" + congratulations if congratulations else "")
        )
    except Exception:
        logging.getLogger(__name__).exception("监控卡片生成失败，改用文字通知")
        return fallback + ("\n" + congratulations if congratulations else "")


monitor_service = GameMonitor(
    db,
    paipuya,
    live,
    settings.monitor_page_size,
    sanma,
    settings.monitor_catchup_limit,
    notification_builder=build_monitor_notification,
    notify_starts=settings.monitor_notify_start,
)
watch_commands = WatchCommands(
    db,
    live,
    manager=settings.monitor_owner,
    clients={4: paipuya, 3: sanma},
    notify_starts=settings.monitor_notify_start,
)
auto_review_tasks: set[asyncio.Task] = set()
install_commands(
    MahjongQueries(db, paipuya, sanma, live=live, admins=settings.admin_users | superusers)
)

bind_cmd = on_command("msbind", priority=10, block=True)
unbind_cmd = on_command("msunbind", priority=10, block=True)
review_cmd = on_command("mortal", aliases={"msreview"}, priority=10, block=True)
help_cmd = on_command("help", aliases={"mshelp"}, priority=20, block=True)
stats_cmd = on_command("msstats", priority=10, block=True)
rank_cmd = on_command("msrank", priority=10, block=True)
vip2_cmd = on_command("vip2", priority=10, block=True, force_whitespace=True)


@get_driver().on_startup
async def startup() -> None:
    await db.initialize()
    await db.migrate_group_monitor_list()
    await tenhou_store.initialize()
    reviews.start()
    if isinstance(live, BrowserSpectatorClient) and live.enabled:
        try:
            await live.start()
        except Exception:
            logger.warning("雀魂观战浏览器启动失败，结算监控继续运行")


@get_driver().on_shutdown
async def shutdown() -> None:
    await batches.close()
    for task in auto_review_tasks:
        task.cancel()
    await asyncio.gather(*auto_review_tasks, return_exceptions=True)
    await reviews.close()
    await paipuya.close()
    await sanma.close()
    await tenhou_rounds.close()
    await tenhou_client.close()
    if isinstance(live, BrowserSpectatorClient):
        await live.close()


@bind_cmd.handle()
async def bind(event: GroupMessageEvent, args=CommandArg()):
    query = args.extract_plain_text().strip()
    if not query:
        await bind_cmd.finish("用法：msbind <UID或昵称>")
    try:
        if query.startswith("friend "):
            value = query.removeprefix("friend ").strip()
            if not value.isdecimal():
                raise ValueError("用法：msbind friend <游戏内好友码>")
            query = str(friend_to_account(int(value)))
        player = await paipuya.resolve(query)
        await db.bind(str(event.user_id), player.account_id, player.nickname)
        await bind_cmd.finish(f"绑定成功：{player.nickname}（{player.account_id}）")
    except (PaipuyaError, ValueError) as exc:
        await bind_cmd.finish(str(exc))


@unbind_cmd.handle()
async def unbind(event: GroupMessageEvent):
    await unbind_cmd.finish(
        "已解除绑定。" if await db.unbind(str(event.user_id)) else "你还没有绑定雀魂 UID。"
    )


@vip2_cmd.handle()
async def manage_vip2(event: MessageEvent, args=CommandArg()):
    if str(event.user_id) not in superusers:
        await vip2_cmd.finish("仅 Bot 超级管理员可管理二级会员。")
    usage = "用法：vip2 add <QQ号或@群友> / vip2 del <QQ号或@群友> / vip2 list"
    parts = args.extract_plain_text().lower().split()
    mentions = [str(s.data.get("qq", "")) for s in args if s.type == "at"]
    targets = parts[1:] + mentions
    if any(s.type not in {"text", "at"} for s in args):
        await vip2_cmd.finish(usage)
    if parts == ["list"] and not mentions:
        members = await db.vip2_members()
        await vip2_cmd.finish(f"二级会员（{len(members)} 人）\n" + ("、".join(members) or "空"))
    if not parts or parts[0] not in {"add", "del"} or len(targets) != 1:
        await vip2_cmd.finish(usage)
    qq = targets[0]
    if not re.fullmatch(r"[1-9][0-9]{4,11}", qq):
        await vip2_cmd.finish("请指定一个有效 QQ 号或 @一位群友。")
    add = parts[0] == "add"
    changed = await db.set_vip2(qq, add)
    if add:
        await vip2_cmd.finish(f"{qq} 已加入二级会员白名单。" if changed else f"{qq} 已在名单中。")
    await vip2_cmd.finish(f"{qq} 已从二级会员白名单移除。" if changed else f"{qq} 不在名单中。")


@stats_cmd.handle()
async def stats(event: GroupMessageEvent, args=CommandArg()):
    parts = args.extract_plain_text().strip().split()
    use_sanma = bool(parts and parts[-1].lower() in {"3", "sanma"})
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
                await stats_cmd.finish("请先发送：msbind <UID或昵称>")
            player = Player(row["account_id"], row["nickname"])
        summary = await client.stats(player.account_id)
        await stats_cmd.finish(
            format_stats(player.nickname, summary)
            + "\n"
            + "\n".join(style_lines(summary, mode=3 if use_sanma else 4, scope="累计"))
        )
    except PaipuyaError as exc:
        await stats_cmd.finish(str(exc))


@rank_cmd.handle()
async def group_rank(bot: Bot, event: GroupMessageEvent, args=CommandArg()):
    mode = args.extract_plain_text().strip().lower()
    client = sanma if mode in {"3", "sanma"} else paipuya
    try:
        members = await group_member_ids(bot, event.group_id)
    except GroupMembersError as exc:
        await rank_cmd.finish(str(exc))
    bindings = await db.member_bindings(members)
    if not bindings:
        await rank_cmd.finish("本群还没有成员绑定雀魂账号。")
    result = await group_ranking(client, bindings)
    if isinstance(result, str):
        await rank_cmd.finish(result)
    await rank_cmd.finish(await send_ranking_forward(bot, event.group_id, "雀魂群排行", result))


watch_cmd = on_command("mswatch", priority=10, block=True)


@watch_cmd.handle()
async def watch(event: GroupMessageEvent, args=CommandArg()):
    try:
        result = await watch_commands.favorites(
            args.extract_plain_text(), event.user_id, event.group_id, event.sender.role or "member"
        )
    except (ValueError, PaipuyaError) as exc:
        await watch_cmd.finish(str(exc))
    await watch_cmd.finish(result)


def task_start_reply(job_id, *, started=True):
    prefix = "开始处理。" if started else "在线跑谱未开启，任务已保存。"
    return f"{prefix}查看任务状态：mortal status {job_id}"


@review_cmd.handle()
async def mortal(event: GroupMessageEvent, args=CommandArg()):
    text = args.extract_plain_text().strip()
    if not text:
        await review_cmd.finish(
            "用法：mortal <雀魂/天凤链接或一番街ID@座位> [座位0-3]，支持四麻半庄"
        )
    parts = text.split()
    parts[0] = parts[0].lower() if parts[0].lower() in BATCH_ACTIONS else parts[0]
    try:
        if parts[0] in BATCH_ACTIONS:
            await batch_command(event, parts)
            return
        if online or not reviews.client.url:
            seat = int(parts[1]) if len(parts) > 1 else None
            job = await batches.create_links(
                [parts[0]], str(event.group_id), str(event.user_id), seat=seat, resolve_names=False
            )
            if batches.available:
                batches.start(job["id"], str(event.group_id), str(event.user_id))
            await review_cmd.finish(task_start_reply(job["id"], started=batches.available))
        seat = int(parts[1]) if len(parts) > 1 else None
        log = parse_review_link(parts[0], seat)
        await review_cmd.send("已加入 Mortal 分析队列，请稍候……")
        result = await reviews.submit(log.url, log.seat)
        image = base64.b64encode(render_review(result, str(event.user_id), log.game_id)).decode()
        message = mention_requester(event.user_id, MessageSegment.image(f"base64://{image}"))
        if result.details_url:
            message += MessageSegment.text(f"\n详细报告：{result.details_url}")
        await review_cmd.finish(message)
    except (ValueError, ReviewError) as exc:
        await review_cmd.finish(str(exc))


async def delete_batch_task(event, arguments):
    if len(arguments) != 1:
        raise ReviewError("用法：msmortal delete <任务编号>")
    job_id = arguments[0]
    await batches.delete(job_id, str(event.group_id), str(event.user_id))
    return f"任务 {job_id} 已删除。"


async def batch_command(event: GroupMessageEvent, parts: list[str]):
    group_id, owner = str(event.group_id), str(event.user_id)
    action = parts[0]
    if action not in BATCH_ACTIONS:
        raise ReviewError("仅支持英文子命令，请发送 /help 查看用法")
    if action == "delete":
        await review_cmd.finish(await delete_batch_task(event, parts[1:]))
    if action in {"table", "links"}:
        if len(parts) < 2:
            raise ReviewError(
                "用法：mortal table <雀魂/天凤链接或一番街ID>；mortal links <牌谱1> <牌谱2>…"
            )
        job = await batches.create_links(
            parts[1:], group_id, owner, table=action == "table", resolve_names=False
        )
        if batches.available:
            batches.start(job["id"], group_id, owner)
        await review_cmd.finish(task_start_reply(job["id"], started=batches.available))
    if action == "batch":
        if len(parts) > 2:
            raise ReviewError("用法：mortal batch [1–100]，默认 10 场")
        count = batch_count(parts[1] if len(parts) == 2 else None)
        binding = await db.binding(owner)
        if not binding:
            raise ReviewError("请先绑定雀魂账号")
        await review_cmd.send("正在建立批量任务，读取最近四麻半庄牌谱……")
        try:
            job = await batches.create(
                count,
                group_id,
                owner,
                Player(binding["account_id"], binding["nickname"]),
            )
        except PaipuyaError as exc:
            raise ReviewError(str(exc)) from exc
        if batches.available:
            batches.start(job["id"], group_id, owner)
        await review_cmd.finish(task_start_reply(job["id"], started=batches.available))
    if len(parts) < 2:
        raise ReviewError("用法：mortal status/report/pause/resume <任务编号>")
    job = batches.load(parts[1], group_id, owner)
    if action in {"export", "report", "chart", "file"}:
        job = await batches.refresh_names(job["id"], group_id, owner)
    if action == "export":
        from .batch_export import write_export

        fmt = parts[2].lower() if len(parts) > 2 else "csv"
        if fmt not in {"csv", "xlsx"}:
            raise ReviewError("用法：mortal export <任务编号> [csv/xlsx]")
        path = settings.batch_directory / job["id"] / ("results." + fmt)
        await asyncio.to_thread(write_export, job, path)
        try:
            await event_bot_upload(event, path)
        except Exception as exc:
            raise ReviewError("导出已保存在本机，但群文件上传失败") from exc
        await review_cmd.finish(f"已上传{fmt.upper()}结果，列名与本地batchmortal兼容。")
    if action == "import":
        if len(parts) != 4 or not parts[2].isdecimal():
            raise ReviewError("用法：mortal import <任务编号> <场次编号> <作者报告链接>")
        try:
            job = await batches.import_report(parts[1], group_id, owner, int(parts[2]), parts[3])
        except httpx.HTTPError as exc:
            raise ReviewError("读取在线报告失败，请稍后重试") from exc
    elif len(parts) != 2:
        raise ReviewError("此命令只接受一个任务编号")
    if action == "resume":
        batches.start(job["id"], group_id, owner)
        await review_cmd.finish(task_start_reply(job["id"]))
    if action == "pause":
        await batches.pause(job["id"], group_id, owner)
        await review_cmd.finish(
            "任务已暂停，可以开始其他任务。已提交的场次会在继续时尝试找回报告。"
        )
    if action in {"report", "chart"}:
        picture = await asyncio.to_thread(render_batch_chart, job)
        encoded = base64.b64encode(picture).decode("ascii")
        await review_cmd.finish(MessageSegment.image(f"base64://{encoded}"))
    if action == "file":
        path = (settings.batch_directory / job["id"] / "report.html").resolve()
        try:
            await event_bot_upload(event, path)
        except Exception as exc:
            logger.exception("批量报告上传失败：job=%s", job["id"])
            raise ReviewError("报告已保存在本机，但上传群文件失败") from exc
        await review_cmd.finish("已上传 HTML 统计表到群文件，可下载后用浏览器打开。")
    completed = sum(row["status"] == "done" for row in job["rows"])
    text = (
        f"任务 {job['id']}：{STATUS_LABELS.get(job['status'], job['status'])}，"
        f"成功 {completed}/{len(job['rows'])} 场。"
    )
    if job.get("message"):
        text += "\n" + job["message"]
    if action == "status" and job["status"] == "running" and online:
        for progress in online.job_progress(job["id"]):
            text += "\n" + progress
    hits = sum(bool(row.get("review_cache_hit")) for row in job["rows"])
    if hits:
        text += f"\n其中 {hits} 场复用已完成报告，无需再次跑谱。"
    for summary in summaries(job["rows"]):
        rating = summary["mean_rating"]
        text += f"\n模型 {summary['model']}：平均 Rating {rating:.2f}" if rating is not None else ""
    if completed < len(job["rows"]) and not batches.available:
        index, row = next(
            (i, row) for i, row in enumerate(job["rows"], 1) if row["status"] != "done"
        )
        text += f"\n第 {index} 场在线复盘：{row['online_url']}\n座位：{row['seat']}（0–3）"
    await review_cmd.finish(text)


async def event_bot_upload(event, path):
    bot = next(iter(get_driver().bots.values()), None)
    if not isinstance(bot, Bot):
        raise ReviewError("QQ 连接不可用")
    await upload_report(bot, event.group_id, path)


@help_cmd.handle()
async def help_handler(bot: Bot, event: MessageEvent):
    await send_help(bot, event, live_enabled=live.enabled, mortal_enabled=batches.available)
    await help_cmd.finish()


async def send_auto_review(bot: Bot, group_id: int, nickname: str, game, account_id: int):
    if str(group_id) in settings.monitor_only_groups:
        return
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
        try:
            await bot.send_group_msg(group_id=group_id, message=f"Mortal 自动分析失败：{exc}")
        except Exception:
            logger.exception("自动分析错误通知发送失败：group=%s uuid=%s", group_id, game.uuid)
    except Exception:
        logger.exception("自动分析失败：group=%s uuid=%s", group_id, game.uuid)


@scheduler.scheduled_job(
    "interval", seconds=settings.monitor_interval, id="pupulin_monitor", max_instances=1
)
async def poll_games() -> None:
    bot = next(iter(get_driver().bots.values()), None)
    if not isinstance(bot, Bot):
        return

    async def send(sub, message):
        await bot.send_group_msg(group_id=int(sub["group_id"]), message=message, _timeout=90)

    async def review(sub, game):
        if settings.mortal_api_url:
            task = asyncio.create_task(
                send_auto_review(
                    bot, int(sub["group_id"]), sub["nickname"], game, sub["account_id"]
                ),
                name=f"auto-review-{game.uuid}",
            )
            auto_review_tasks.add(task)
            task.add_done_callback(auto_review_tasks.discard)

    await monitor_service.poll(await db.subscriptions(), send, review)


msmortal_cmd = on_command("msmortal", priority=10, block=True)
mortalstatus_cmd = on_command("mortalstatus", priority=10, block=True)


@mortalstatus_cmd.handle()
async def mortal_status(event: GroupMessageEvent, args=CommandArg()):
    text = args.extract_plain_text().strip()
    if text:
        await batch_command(event, ["status", text])
        return
    await mortalstatus_cmd.finish(
        f"在线 Chrome 批跑：{'已启用' if online else '未启用'}\n"
        f"桥接服务：{'已配置' if reviews.client.url else '未配置'}\n"
        f"运行中任务：{', '.join(batches.tasks) or '无'}\n"
        f"在线模式：单浏览器逐项提交，最短提交间隔 {settings.mortal_online_cooldown:g} 秒\n"
        "已完成报告缓存：开启（同牌谱、座位、模型）\n"
        "用法：msmortal [玩家] -l 1–100；mortalstatus <任务编号>；msmortal delete <任务编号>"
    )


@msmortal_cmd.handle()
async def ms_mortal(event: GroupMessageEvent, args=CommandArg()):
    try:
        text = args.extract_plain_text().strip()
        parts = text.split()
        if parts and parts[0].lower() == "delete":
            await msmortal_cmd.finish(await delete_batch_task(event, parts[1:]))
        options = parse_options(text, max_last=MAX_BATCH_COUNT, allow_all=False)
        count = batch_count(options.last if options.last_explicit else None)
        if options.mode != 4:
            raise ValueError("Mortal 批跑仅支持四麻半庄，-l 场数范围为 1–100")
        if options.subscription or options.group or options.image or options.gap != 60:
            raise ValueError("msmortal 支持玩家、-l 场数、-r 房间、start/end、-m/-y 和 -n")
        player = await MahjongQueries(db, paipuya, sanma).player(options, event.user_id)
        selected = [m for m in (options.modes or [9, 12, 16]) if m in {9, 12, 16}]
        if not selected:
            raise ValueError("Mortal 仅支持四麻半庄；请使用 game=南 或 -r 玉南等半庄筛选")
        games, _ = await paipuya.history(
            player.account_id, count, start=options.start, end=options.end, modes=selected
        )
        job = await batches.create(
            count, str(event.group_id), str(event.user_id), player, games=games
        )
        if batches.available:
            try:
                batches.start(job["id"], str(event.group_id), str(event.user_id))
            except ReviewError as exc:
                await msmortal_cmd.finish(
                    f"任务 {job['id']} 已保存。{exc}\n可发送 mortal resume {job['id']} 开始。"
                )
        await msmortal_cmd.finish(
            f"任务 {job['id']}：找到 {len(job['rows'])} 场四麻半庄。\n"
            f"进度：mortal status {job['id']}\n图表：mortal report {job['id']}"
        )
    except (ValueError, ReviewError, PaipuyaError) as exc:
        await msmortal_cmd.finish(str(exc))


health_cmd = on_command("msstatus", priority=10, block=True)


async def runtime_status(group_id):
    from datetime import UTC, datetime

    from .ms_options import TZ

    def local(value):
        if not value:
            return "尚无记录"
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(TZ).strftime("%m-%d %H:%M:%S")

    rows = [r for r in await db.subscriptions() if r["group_id"] == str(group_id)]
    lines = [
        f"雀魂服务状态\n本群有效监控 {len(rows)} 个账号/模式",
        f"轮询间隔 {settings.monitor_interval} 秒；单次补查上限 {monitor_service.catchup_limit} 场",
        watch_commands.boundary(),
        "牌谱屋鉴权："
        + (
            "API Key（标准 Bearer）"
            if paipuya_access.using_api_key
            else "Key已失效，备用 CAP"
            if paipuya_access.key_invalid
            else "未配置 API Key"
        ),
        "API 限速：全局最多 1 QPS（三麻/四麻/监控/查询及重试共用）",
        f"Mortal：{'桥接已配置' if reviews.client.url else '暂未配置桥接'}；"
        f"在线批跑{'开启' if online else '关闭'}",
    ]
    for client in (paipuya, sanma):
        lines.append(
            f"{client.mode}数据源：最近成功响应 "
            f"{local(client.last_success.isoformat() if client.last_success else None)}"
        )
        if client.last_error:
            lines.append("最近请求错误：" + client.last_error)
        lines.append(
            f"查询缓存命中 {client.cache.hits}，实际加载 {client.cache.misses}（缓存最长 5 分钟）"
        )
    for row in rows[:10]:
        health = await db.monitor_health(row["subscriber"])
        label = f"{row['nickname']}（{'三麻' if row['mode'] == 3 else '四麻'}）"
        if not health:
            lines.append(label + "：尚未完成轮询")
            continue
        lines.append(
            label
            + "：最近检查 "
            + local(health["checked_at"])
            + "；最近成功 "
            + local(health["last_success"])
        )
        if health["error"]:
            lines.append(health["error"])
        if health["capped"]:
            lines.append("补查触及上限，可能仍有更早未通知对局；请管理员提高补查上限。")
    if len(rows) > 10:
        lines.append(f"其余 {len(rows) - 10} 个账号/模式已省略")
    return "\n".join(lines)


@health_cmd.handle()
async def health(event: GroupMessageEvent):
    await health_cmd.finish(await runtime_status(event.group_id))


table_review_cmd = on_command("mstable", priority=10, block=True)


@table_review_cmd.handle()
async def table_review(event: GroupMessageEvent, args=CommandArg()):
    try:
        text = args.extract_plain_text().strip()
        if not text:
            raise ReviewError("用法：mstable <雀魂/天凤链接或一番街ID@0>，分析同桌四个座位")
        await batch_command(event, ["table", text])
    except (ValueError, ReviewError) as exc:
        await table_review_cmd.finish(str(exc))


@scheduler.scheduled_job("interval", seconds=60, id="pupulin_tenhou_monitor", max_instances=1)
async def poll_tenhou() -> None:
    bot = next(iter(get_driver().bots.values()), None)
    if not isinstance(bot, Bot):
        return
    await tenhou_monitor.poll(send_tenhou_message)
