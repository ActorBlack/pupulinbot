"""English-only Tenhou commands, isolated from Mahjong Soul bindings."""

import base64
from datetime import datetime

from nonebot import on_command
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageSegment
from nonebot.params import CommandArg

from .tenhou import CN, TenhouError, validate_name
from .tenhou_queries import TenhouQueries


async def manage_watch(store, client, text, user_id, group_id, *, manager=None):
    parts = text.strip().split(maxsplit=1)
    action = parts[0] if parts else ""
    name = parts[1].strip() if len(parts) > 1 else ""
    if action not in {"on", "off"}:
        raise TenhouError("用法：thwatch on <昵称> / off <昵称> / off all / list")
    if not manager or str(user_id) != str(manager):
        raise TenhouError("仅 Bot 负责人可修改监控名单。")
    if not name:
        raise TenhouError("用法：thwatch on <昵称> / off <昵称>；清空用 off all")
    if action == "off":
        count = await store.remove(group_id, name=None if name == "all" else name)
        return f"已移除本群天凤监控：{count} 项。"
    name = validate_name(name)
    existing = await store.subscriptions(group_id)
    names = {r["name"] for r in existing}
    if name in names:
        return f"本群已监控：{name}"
    if len(names) >= 10:
        raise TenhouError("本群最多监控10位天凤玩家，请先移除不用的监控。")
    if not await client.history(name):
        raise TenhouError("nodocchi 未找到该昵称的公开战绩，请检查大小写和日文字符。")
    await store.add(group_id, manager, name)
    return f"已添加本群天凤监控：{name}"


def install_tenhou_commands(store, client, monitor, *, manager=None):
    watch = on_command("thwatch", priority=10, block=True)
    status = on_command("thstatus", priority=10, block=True)
    queries = TenhouQueries(store, client, manager=manager)

    async def status_text(event):
        rows = await store.subscriptions(event.group_id)
        lines = [
            f"天凤本群监控：{len({r['name'] for r in rows})} 位玩家",
            "公开观战约5分钟延迟；结算等待 nodocchi 收录。",
        ]
        for row in rows:
            checked = (
                datetime.fromtimestamp(row["checked_at"], CN).strftime("%m-%d %H:%M:%S")
                if row["checked_at"]
                else "等待首次检查"
            )
            health = (row["error"] or "结算源正常") if row["checked_at"] else "待检查"
            lines.append(f"{row['name']} · {checked} · {health}")
        lines.append(
            "开局源：" + (monitor.live_error or ("正常" if monitor.live_checked else "待检查"))
        )
        return "\n".join(lines)

    @watch.handle()
    async def handle_watch(event: GroupMessageEvent, args=CommandArg()):
        parts = args.extract_plain_text().strip().split(maxsplit=1)
        action = parts[0] if parts else "status"
        name = parts[1].strip() if len(parts) > 1 else ""
        if action in {"status", "list"} and not name:
            await watch.finish(MessageSegment.text(await status_text(event)))
        try:
            result = await manage_watch(
                store,
                client,
                args.extract_plain_text(),
                event.user_id,
                event.group_id,
                manager=manager,
            )
        except TenhouError as exc:
            result = str(exc)
        await watch.finish(MessageSegment.text(result))

    @status.handle()
    async def handle_status(event: GroupMessageEvent):
        await status.finish(MessageSegment.text(await status_text(event)))

    def register(name):
        matcher = on_command(name, priority=10, block=True, force_whitespace=True)

        @matcher.handle()
        async def handler(event: GroupMessageEvent, args=CommandArg()):
            mentioned = [
                s.data.get("qq") for s in args if s.type == "at" and s.data.get("qq") != "all"
            ]
            if len(mentioned) > 1 or (mentioned and name != "th2"):
                await matcher.finish("只有 th2 支持 @一位群友查询或代绑账号。")
            try:
                result = await queries.query(
                    name,
                    args.extract_plain_text().strip(),
                    event.user_id,
                    event.group_id,
                    role=event.sender.role or "member",
                    mentioned=mentioned[0] if mentioned else None,
                )
            except TenhouError as exc:
                await matcher.finish(str(exc))
            if isinstance(result, bytes):
                result = MessageSegment.image(
                    "base64://" + base64.b64encode(result).decode("ascii")
                )
            await matcher.finish(result)

    for name in (
        "thbind",
        "thunbind",
        "th2",
        "th",
        "th4",
        "th3",
        "thlast",
        "thdetail",
        "thlife",
        "thrank",
    ):
        register(name)
