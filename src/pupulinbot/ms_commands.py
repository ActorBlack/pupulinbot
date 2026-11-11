"""Shared Mahjong Soul query handlers, separated from NoneBot registration."""

import asyncio
import base64
from copy import copy
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from nonebot import on_command
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageSegment
from nonebot.params import CommandArg

from .formatting import format_records, format_stats
from .live import LiveServiceError
from .models import Player
from .monitor_card import collect_profiles, render_monitor_card
from .ms_charts import render_history, render_profile
from .ms_extra import matchup, render_comparison
from .ms_ids import account_to_friend, friend_to_account
from .ms_options import MODE_NAMES, TZ, level_label, parse_options
from .ms_recent import render_recent
from .ms_style import style_lines
from .ms_style_chart import render_style
from .ms_summary import format_summary
from .paipuya import PaipuyaError

QUERY_COMMANDS = (
    "ms",
    "ms2",
    "ms3",
    "ms4",
    "mslife",
    "msheat",
    "msstat",
    "mssession",
    "msfri",
    "mssfri",
    "mslast",
    "msstyle",
    "msvs",
    "mscompare",
)
QUERY_ALIASES = {"mslast": ("msrecent",), "msvs": ("mswith",)}


class MahjongQueries:
    def __init__(self, db, four, three, *, live=None, admins=()):
        self.db, self.clients = db, {4: four, 3: three}
        self.gate = asyncio.Semaphore(2)
        self.live = live
        self.admins = frozenset(str(user) for user in admins)

    async def player(self, options, user_id):
        if options.player:
            if options.numeric:
                return Player(int(options.player), options.player)
            if self.db is not None:
                known = await self.db.binding(str(user_id))
                if known and options.player in {known["nickname"], str(known["account_id"])}:
                    return Player(known["account_id"], known["nickname"])
            return await self.clients[options.mode].resolve(options.player)
        row = await self.db.binding(str(user_id))
        if not row:
            raise ValueError("请先发送：msbind <account ID/昵称>，或在命令后指定玩家")
        return Player(row["account_id"], row["nickname"])

    async def query(self, command, text, user_id, group_id, *, role="member", mentioned=None):
        if command not in QUERY_COMMANDS:
            raise ValueError("不支持的雀魂查询命令")
        last_limit = 500 if command == "ms" else 2000
        options = parse_options(text, mode=3 if command == "ms3" else 4, max_last=last_limit)
        if command == "ms2":
            if not mentioned:
                raise ValueError("用法：ms2 @一位群友 [雀魂昵称或 account ID]")
            if options.player:
                if str(mentioned) != str(user_id) and (
                    role not in {"admin", "owner"} and str(user_id) not in self.admins
                ):
                    raise ValueError("只有本人、群管理员或 Bot 管理员可以代绑雀魂账号。")
                if (
                    options.subscription
                    or options.group
                    or options.observe
                    or options.image
                    or options.start
                    or options.end
                    or options.modes
                    or options.last_explicit
                    or options.gap != 60
                ):
                    raise ValueError("代绑用法：ms2 @群友 <雀魂昵称或 account ID>")
                target = str(mentioned)
                if await self.db.binding(target):
                    return "该群友已绑定雀魂账号，请由本人修改。"
                player = await self.clients[options.mode].resolve(options.player)
                if not await self.db.bind_if_absent(target, player.account_id, player.nickname):
                    return "该群友已绑定雀魂账号，请由本人修改。"
                return f"绑定成功：{player.nickname}（{player.account_id}）"
            command, user_id = f"ms{options.mode}", mentioned
        if command == "msstyle" and options.mode != 4:
            raise ValueError("msstyle 的参考模型仅适用于四麻；请使用 mode=4")
        if command == "ms3" and options.mode != 3:
            raise ValueError("ms3 仅查询三麻；四麻请使用 ms4")
        if command == "ms4" and options.mode != 4:
            raise ValueError("ms4 仅查询四麻；三麻请使用 ms3")
        if command == "ms" and options.image:
            raise ValueError("ms 发送文字摘要；资料卡请使用 ms4 -i 或 ms3 -i")
        if options.observe:
            if command not in {"ms4", "ms3"} or options.subscription or options.group:
                raise ValueError("观战用法：ms4 <玩家> -o [-i]，三麻使用 ms3")
            if options.start or options.end or options.modes or options.last != 200:
                raise ValueError("观战不支持历史日期、房间或 last 筛选")
            if not self.live or not self.live.enabled:
                raise ValueError("雀魂观战尚未启用")
            player = await self.player(options, str(user_id))
            try:
                game = await self.live.current_game(player.account_id)
                if not game or len(game.players) != options.mode:
                    return f"{player.nickname}：未在公开观战列表中找到对局"
                if options.image:
                    profiles = await collect_profiles(self.clients[options.mode], game)
                    return await asyncio.to_thread(render_monitor_card, game, "start", profiles)
                names = " / ".join(p.get("nickname", str(p["account_id"])) for p in game.players)
                return (
                    f"{player.nickname} · 正在对局\n"
                    f"{options.mode}麻 · {game.started_at.astimezone(TZ):%m-%d %H:%M}\n"
                    f"{names}\n{game.url}"
                )
            except LiveServiceError as exc:
                return str(exc)
        target = str(user_id)
        client = self.clients[options.mode]
        if command in {"msfri", "mssfri"}:
            if (
                options.subscription
                or options.group
                or options.start
                or options.end
                or options.modes
                or options.last != 200
                or options.gap != 60
            ):
                raise ValueError("好友码命令支持 玩家/好友码、mode=3/4 和 -i")
            return await self.friend(command, options, user_id)
        if options.subscription or options.group:
            raise ValueError(
                "个人关注已取消；使用 mswatch list 查看本群监控，添加和删除仅限负责人。"
            )
        profile_filtered = bool(
            options.start or options.end or options.modes or options.last_explicit
        )
        if command not in {"ms4", "ms3"} or profile_filtered:
            options = parse_options(
                text,
                mode=options.mode,
                single_room=True,
                default_room=None if command == "ms" else "玉",
                max_last=last_limit,
            )
        async with self.gate:
            if command == "msvs":
                if not options.player:
                    raise ValueError("用法：msvs <对手昵称/account ID> [-l 200] [-r 玉南]")
                if options.image:
                    raise ValueError("同桌统计目前发送文字及牌谱链接，请去掉 -i")
                own = await self.player(replace(options, player="", numeric=False), target)
                opponent = (
                    Player(int(options.player), options.player)
                    if options.player.isdecimal()
                    else await self.player(options, target)
                )
                if own.account_id == opponent.account_id:
                    raise ValueError("请指定另一位玩家，不能与本人比较")
                load = getattr(client, "query_history", client.history)
                games, truncated = await load(
                    own.account_id,
                    options.last,
                    start=options.start,
                    end=options.end,
                    modes=options.modes,
                )
                return matchup(own, opponent, games, options, truncated)
            player = await self.player(options, target)
            if command == "ms":
                if command == "ms" and (not options.modes or len(options.modes) > 1):
                    load = getattr(client, "query_history", client.history)
                    latest, _ = await load(player.account_id, 1, modes=options.modes)
                    finished = [g for g in latest if g.ended_at and g.seat(player.account_id)]
                    if not finished:
                        return f"{player.nickname}: 暂无已结算收录对局"
                    mode = max(finished, key=lambda g: g.started_at).mode
                    allowed = options.modes or client.modes
                    if mode not in {str(m) for m in allowed}:
                        raise PaipuyaError("最近对局的房间信息异常，请稍后重试")
                    options.modes = [int(mode)]
                if not options.last_explicit:
                    options.last = 100
                return await self.summary_query(player, options, client)
            if command in {"ms4", "ms3"}:
                if profile_filtered:
                    return await self.profile_query(player, options, client)
                stats = await client.stats(player.account_id)
                if options.image:
                    return await asyncio.to_thread(render_profile, player, stats)
                return (
                    format_stats(player.nickname, stats)
                    + f"\n记录段位：{level_label(stats.raw.get('level'))}｜ID {player.account_id}"
                    + "\n"
                    + "\n".join(style_lines(stats, mode=options.mode, scope="累计"))
                )
            if options.image and command not in {"mslast", "mscompare", "msstyle"}:
                raise ValueError("-i 用于 ms4/ms3 面板或 mslast 牌谱图片；图表命令直接发送图片")
            if command in {"mscompare", "msstyle"} and not options.last_explicit:
                options.last = 100
            limit = options.last
            if command == "mslast":
                limit = 10 if not options.last_explicit else limit
                if options.image and limit > 20:
                    raise ValueError("最近牌谱图片最多 20 场，请使用 -l 1–20")
                if limit > 50:
                    raise ValueError("最近牌谱文本最多 50 场；更长历史请使用图表命令")
            load_history = getattr(client, "query_history", client.history)
            games, truncated = await load_history(
                player.account_id, limit, start=options.start, end=options.end, modes=options.modes
            )
            if command == "mslast":
                if options.image:
                    return await asyncio.to_thread(render_recent, player, games, options.mode)
                return f"{player.nickname} · {options.mode}麻\n" + format_records(
                    games, player.account_id
                )
            if command == "msstyle":
                stats = await self.sample_stats(player, games, options, client)
                return await asyncio.to_thread(render_style, player, stats, games, options)
            if command == "mscompare":
                return await asyncio.to_thread(render_comparison, player, games, truncated, options)
            return await asyncio.to_thread(
                render_history, command, player, games, truncated, options
            )

    async def profile_query(self, player, options, client):
        """A filtered profile uses exact window aggregates, never lifetime rates."""
        try:
            async with asyncio.timeout(210):
                now = datetime.now(TZ)
                games = []
                start, end = options.start or datetime.fromtimestamp(0, UTC), options.end or now
                if options.last_explicit:
                    load = getattr(client, "query_history", client.history)
                    rows, _ = await load(
                        player.account_id,
                        options.last,
                        start=options.start,
                        end=options.end,
                        modes=options.modes,
                    )
                    games = [g for g in rows if g.ended_at and g.seat(player.account_id)]
                    if not games:
                        return f"{player.nickname}: 筛选范围内暂无已结算收录对局"
                    if len({g.uuid for g in games}) != len(games) or any(
                        str(g.mode) not in {str(m) for m in options.modes} for g in games
                    ):
                        raise PaipuyaError("牌谱样本的房间或重复记录异常，请稍后重试")
                    start = min(g.started_at for g in games)
                    end = max(g.started_at for g in games) + timedelta(milliseconds=1)
                value = await client.range_stats(
                    player.account_id,
                    start=start,
                    end=end,
                    modes=options.modes,
                )
                if games and value.games != len(games):
                    raise PaipuyaError("样本统计与牌谱场数不一致，请稍后重试")
                if not value.games:
                    return f"{player.nickname}: 筛选范围内暂无已收录统计"
                stats = copy(value)
                stats.raw = dict(value.raw)
                # Current/highest ranks describe the latest mode-wide identity;
                # all rates, counts and scores below remain the selected sample.
                identity = {}
                if callable(getattr(client, "metadata", None)):
                    try:
                        async with asyncio.timeout(15):
                            latest = await client.metadata(player.account_id)
                            identity = latest.raw if isinstance(latest.raw, dict) else {}
                    except (PaipuyaError, TimeoutError):
                        pass
                for key in ("level", "max_level"):
                    stats.raw[key] = identity.get(key)
        except TimeoutError as exc:
            raise PaipuyaError("统计查询超时，请稍后重试") from exc
        room = MODE_NAMES[str(options.modes[0])].removeprefix("三麻")
        count = (
            f"最近 {stats.games:,} 场" if options.last_explicit else f"已收录 {stats.games:,} 场"
        )
        if options.start or options.end:
            count = f"所选日期 {stats.games:,} 场"
        scope = f"{'三麻' if options.mode == 3 else '四麻'} · {room} · {count}"
        if options.image:
            return await asyncio.to_thread(render_profile, player, stats, scope=scope, games=games)
        return (
            format_stats(player.nickname, stats)
            + "\n"
            + scope
            + "\n"
            + "\n".join(
                style_lines(stats, mode=options.mode, games=games, account_id=player.account_id)
            )
        )

    async def sample_stats(self, player, games, options, client):
        finished = [g for g in games if g.ended_at and g.seat(player.account_id)]
        if not finished or options.mode != 4:
            return None
        try:
            async with asyncio.timeout(210):
                stats = await client.range_stats(
                    player.account_id,
                    start=min(g.started_at for g in finished),
                    end=max(g.started_at for g in finished) + timedelta(milliseconds=1),
                    modes=options.modes or client.modes,
                )
            return stats if stats.games == len(finished) else None
        except (PaipuyaError, TimeoutError, AttributeError):
            return None

    async def summary_query(self, player, options, client):
        load_history = getattr(client, "query_history", client.history)
        games, truncated = await load_history(
            player.account_id,
            options.last,
            start=options.start,
            end=options.end,
            modes=options.modes,
        )
        finished = [g for g in games if g.ended_at and g.seat(player.account_id)]
        if not finished:
            return f"{player.nickname}: 筛选范围内暂无已结算收录对局"
        now = datetime.now(TZ)
        context = {}

        async def total():
            if hasattr(client, "metadata"):
                context["total"] = await client.metadata(player.account_id, modes=options.modes)
            else:
                context["total"] = await client.stats(player.account_id)

        async def sample():
            context["sample"] = await client.range_stats(
                player.account_id,
                start=min(g.started_at for g in finished),
                end=max(g.started_at for g in finished) + timedelta(milliseconds=1),
                modes=options.modes or client.modes,
            )

        async def activity():
            if (
                not options.start
                and not options.end
                and (
                    not truncated or min(g.started_at for g in finished) <= now - timedelta(days=30)
                )
            ):
                context["activity"] = (finished, False)
            else:
                context["activity"] = await load_history(
                    player.account_id,
                    2000,
                    start=now - timedelta(days=30),
                    end=now,
                    modes=options.modes,
                )

        async def optional(load):
            try:
                await load()
            except Exception:
                pass

        try:
            async with asyncio.timeout(210):
                await asyncio.gather(*(optional(f) for f in (total, sample, activity)))
        except TimeoutError:
            pass
        return format_summary(player, games, truncated, options, context, now=now)

    async def friend(self, command, options, user_id):
        async with self.gate:
            client = self.clients[options.mode]
            if command == "msfri":
                if not options.player.isdecimal():
                    raise ValueError("用法：msfri <好友码> [mode=3/4] [-i]")
                account_id = friend_to_account(int(options.player))
                player = Player(account_id, str(account_id))
            elif options.player.isdecimal():
                player = Player(int(options.player), options.player)
            else:
                player = await self.player(options, user_id)
            friend_id = account_to_friend(player.account_id)
            identity = f"account ID：{player.account_id}\n好友码：{friend_id}"
            if command == "mssfri" and not options.image:
                return f"{player.nickname}\n{identity}"
            try:
                stats = await client.stats(player.account_id)
            except PaipuyaError as exc:
                return f"{identity}\n换算完成，但暂时无法查询统计：{exc}"
            nickname = stats.raw.get("nickname")
            if isinstance(nickname, str) and nickname:
                player.nickname = nickname
            elif command == "msfri":
                # Only fetch one latest record if the stats endpoint omits the nickname.
                try:
                    games = await client.records(player.account_id, 1)
                    seat = games[0].seat(player.account_id) if games else None
                    if seat and seat.get("nickname"):
                        player.nickname = str(seat["nickname"])
                except PaipuyaError:
                    pass
            if options.image:
                return await asyncio.to_thread(render_profile, player, stats)
            return f"{identity}\n" + format_stats(player.nickname, stats)


def install_commands(service):
    def register(name, aliases=()):
        matcher = on_command(
            name,
            aliases=set(aliases),
            priority=10,
            block=True,
            force_whitespace=True if name in {"ms", "ms2"} else None,
        )

        @matcher.handle()
        async def handler(event: GroupMessageEvent, args=CommandArg()):
            mentioned = [str(s.data.get("qq", "")) for s in args if s.type == "at"]
            member = {}
            if name == "ms2":
                if len(mentioned) != 1 or not mentioned[0].isdecimal():
                    await matcher.finish("用法：ms2 @一位群友 [雀魂昵称或 account ID]")
                member = {"role": event.sender.role or "member", "mentioned": mentioned[0]}
            elif any(qq != "all" for qq in mentioned):
                await matcher.finish("请直接指定玩家昵称或 account ID")
            try:
                result = await service.query(
                    name,
                    args.extract_plain_text().strip(),
                    event.user_id,
                    event.group_id,
                    **member,
                )
            except (ValueError, PaipuyaError) as exc:
                await matcher.finish(str(exc))
            if isinstance(result, bytes):
                result = MessageSegment.image(
                    "base64://" + base64.b64encode(result).decode("ascii")
                )
            await matcher.finish(result)

    for name in QUERY_COMMANDS:
        register(name, QUERY_ALIASES.get(name, ()))
