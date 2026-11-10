"""One Mahjong Soul monitoring list per group, managed by the configured owner."""

from .models import Player
from .ms_options import parse_options


class WatchCommands:
    def __init__(self, db, live, *, manager=None, clients=None, notify_starts=True):
        self.db, self.live = db, live
        self.manager = str(manager) if manager else None
        self.clients = clients or {}
        self.notify_starts = notify_starts

    def boundary(self):
        if not self.notify_starts:
            return "开局提醒已关闭；仅播报对局结算。"
        if hasattr(self.live, "status") and self.live.enabled:
            return self.live.status() + "；结算来自牌谱屋。"
        return "结算通知已开启。"

    async def favorites(self, text, user_id, group_id, role="member"):
        parts = text.strip().split(maxsplit=1)
        action = parts[0].lower() if parts else "list"
        if action not in {"on", "off", "list", "status"}:
            raise ValueError("用法：mswatch on <昵称> / off <昵称> / list")
        rest = parts[1] if len(parts) > 1 else ""
        options = parse_options(rest)
        if (
            options.group
            or options.subscription
            or options.observe
            or options.numeric
            or options.image
            or options.start
            or options.end
            or options.modes
            or options.last != 200
            or options.gap != 60
        ):
            raise ValueError("mswatch 只接受昵称或 account ID，可加 mode=3 查询三麻")
        group = str(group_id)
        if action in {"list", "status"}:
            if options.player:
                raise ValueError("用法：mswatch list [mode=3/4]")
            modes = [options.mode] if any(t.startswith("mode=") for t in rest.split()) else [4, 3]
            rows = []
            for mode in modes:
                rows.extend(await self.db.follows("group", group, mode, group_id=group))
            return "\n".join(
                label + "：" + ("、".join(r["nickname"] for r in rows if r["mode"] == mode) or "空")
                for mode, label in ((4, "四麻"), (3, "三麻"))
            )
        if not self.manager or str(user_id) != self.manager:
            raise ValueError("仅 Bot 负责人可修改监控名单。")
        if not options.player:
            raise ValueError("用法：mswatch on <昵称> / off <昵称>；清空用 off all")
        rows = await self.db.follows("group", group, options.mode, group_id=group)
        matches = [r for r in rows if options.player in {r["nickname"], str(r["account_id"])}]
        if len(matches) > 1:
            raise ValueError("存在同名玩家，请使用 account ID。")
        if action == "off":
            if options.player == "all":
                count = await self.db.unfollow("group", group, options.mode, group_id=group)
                await self.db.set_follow_monitor("group", group, group, options.mode, False)
                return f"已清空本群{options.mode}麻监控：{count} 项。"
            if not matches:
                return "该玩家不在本群监控名单中。"
            await self.db.unfollow(
                "group", group, options.mode, matches[0]["account_id"], group_id=group
            )
            if len(rows) == 1:
                await self.db.set_follow_monitor("group", group, group, options.mode, False)
            return f"已移除监控：{matches[0]['nickname']}"
        if matches:
            player = Player(matches[0]["account_id"], matches[0]["nickname"])
        else:
            if len(rows) >= 50:
                raise ValueError("本群该模式最多监控50位玩家。")
            client = self.clients.get(options.mode)
            if client is None:
                raise ValueError("雀魂查询服务暂不可用。")
            player = await client.resolve(options.player)
        await self.db.follow("group", group, player, options.mode, group_id=group)
        await self.db.set_follow_monitor("group", group, group, options.mode, True)
        return f"已添加监控：{player.nickname}（{player.account_id}）"
