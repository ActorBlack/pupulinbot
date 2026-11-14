"""Bound-account Tenhou profiles and filtered public-record queries."""

import asyncio
import time
from datetime import datetime, timedelta

from .models import GameRecord
from .ms_sample import number
from .rank_card import render_rank_pages
from .rate_tags import rate_tag
from .tenhou import (
    CN,
    LOG_ID,
    TenhouError,
    format_result,
    grade_name,
    history_rows,
    record_id,
    rule_name,
    table_scores,
    watch_url,
)
from .tenhou_options import ROOMS, parse_tenhou_options
from .tenhou_profile import history_rank, rank_history


def ranked_rows(data, name, mode):
    return [
        r
        for r in history_rows(data, name)
        if int(r["playernum"]) == mode and r.get("sctype") in {"b", "c"} and not r.get("ron2")
    ]


def select_rows(rows, options, *, recent):
    room, length = options.room, options.length
    if room is None:
        candidates = [r for r in rows if length is None or int(r["playlength"]) == length]
        if candidates:
            room, length = (int(candidates[-1][k]) for k in ("playerlevel", "playlength"))
    matching = [
        r
        for r in rows
        if (room is None or int(r["playerlevel"]) == room)
        and (length is None or int(r["playlength"]) == length)
    ]
    filtered = [
        r
        for r in matching
        if (options.start is None or float(r["starttime"]) >= options.start)
        and (options.end is None or float(r["starttime"]) < options.end)
    ]
    scope = ROOMS[room] if room is not None else "全部房间"
    if length:
        scope += "东" if length == 1 else "南"
    return (filtered[-options.last :] if recent else filtered), matching, scope, room, length


def make_view(
    data, name, options, *, recent, now=None, phoenix=None, phoenix_error=None, percentiles=None
):
    now = now or time.time()
    progress = rank_history(data, name, options.mode, now + 1, include_records=True)
    current_records = progress.pop("current_records", [])
    all_rows = ranked_rows({**data, "list": current_records}, name, options.mode)
    rows, activity_rows, scope, room, length = select_rows(all_rows, options, recent=recent)
    if not rows:
        raise TenhouError(
            f"{name}：筛选范围内没有已收录的{'三麻' if options.mode == 3 else '四麻'}战绩。"
        )
    ranks = [history_rank(r, name) for r in rows]
    counts = [ranks.count(i) for i in range(1, options.mode + 1)]
    results = [float(r[f"player{rank}ptr"]) for r, rank in zip(rows, ranks, strict=True)]
    known_scores = [table_scores(r) for r in rows]
    negative = sum(
        s[rank - 1] < 0 for s, rank in zip(known_scores, ranks, strict=True) if s is not None
    )
    rates = data.get("rate")
    rate = number(rates.get(str(options.mode))) if isinstance(rates, dict) else None
    all_rates = [v for r in all_rows if (v := number(r.get("rate"))) is not None]
    if rate is not None:
        all_rates.append(rate)
    # Some public archives omit seq. Replay their indexed history as an estimate,
    # but suppress PT when a reported sequence exposes a gap.
    complete_pt = all(
        row.get("seq") is None or 1 <= (number(row["seq"]) or 0) <= i
        for i, row in enumerate(all_rows, 1)
    )
    view = dict(
        name=name,
        mode=options.mode,
        scope=scope,
        room=room,
        length=length,
        recent=recent,
        period_filtered=options.start is not None or options.end is not None,
        rows=rows,
        count=len(rows),
        total_count=len(all_rows),
        scope_count=len(activity_rows),
        pt_deltas=[number(r.get("_pt_delta")) if complete_pt else None for r in rows],
        ranks=ranks,
        counts=counts,
        average=sum(ranks) / len(ranks),
        top2=sum(r <= 2 for r in ranks) / len(ranks),
        negative=negative / len(rows) if all(s is not None for s in known_scores) else None,
        average_result=sum(results) / len(results),
        total_result=sum(results),
        rate=rate,
        highest_rate=max(all_rates) if all_rates else None,
        progress=progress,
        activity=[
            sum(0 <= now - float(r["starttime"]) < days * 86400 for r in activity_rows)
            for days in (1, 7, 30)
        ],
        latest=float(activity_rows[-1]["starttime"]),
        phoenix_scope=("三" if options.mode == 3 else "四")
        + "鳳"
        + ("东" if length == 1 else "南"),
    )
    return add_phoenix(view, phoenix, phoenix_error, percentiles)


def add_phoenix(view, phoenix=None, error=None, percentiles=None):
    phoenix = phoenix or {}
    win, deal = (number(phoenix.get(k)) for k in ("agariC", "houjuuC"))
    win_pt, deal_pt = (number(phoenix.get(k)) for k in ("agariVT", "houjuuVT"))
    difference = (
        win * win_pt - deal * deal_pt
        if all(v is not None for v in (win, deal, win_pt, deal_pt))
        else None
    )
    view.update(
        phoenix=phoenix,
        phoenix_error=error,
        percentiles=percentiles or {},
        difference=difference,
        tag=rate_tag(win, deal, mode=view["mode"]),
    )
    return view


def chart_records(view, *, use_pt=False):
    games = []
    for row, rank, delta in zip(view["rows"], view["ranks"], view["pt_deltas"], strict=True):
        start = datetime.fromtimestamp(float(row["starttime"]), CN)
        minutes = number(row.get("during"))
        # For comparison, records are known to have settled even when duration is absent.
        end = (
            start + timedelta(minutes=minutes) if minutes is not None else start if use_pt else None
        )
        scores = table_scores(row) if use_pt else None
        seat = {"account_id": 0, "rank": rank, "score": scores[rank - 1] if scores else None}
        if use_pt:
            seat["gradingScore"] = delta
        games.append(GameRecord(record_id(row), start, end, "", [seat], row))
    return games


def matchup_text(view, opponent):
    n = view["mode"]
    slots = range(1, n + 1)
    common = [row for row in view["rows"] if any(row[f"player{i}"] == opponent for i in slots)]
    mode = "三麻" if n == 3 else "四麻"
    lines = [
        f"{view['name']} vs {opponent}（{mode}{view['scope']}）",
        f"本人查询 {view['count']} 场，同桌 {len(common)} 场",
    ]
    if not common:
        return "\n".join([*lines, "未找到同桌记录。"])
    ranks = [(history_rank(row, view["name"]), history_rank(row, opponent)) for row in common]
    ahead = sum(a < b for a, b in ranks)
    own, other = (sum(values) / len(common) for values in zip(*ranks, strict=True))
    lines += [
        f"顺位领先 {ahead} 场 / 落后 {len(common) - ahead} 场",
        f"本人 / 对手均顺 {own:.2f} / {other:.2f}",
    ]
    scores = [
        score[a - 1] - score[b - 1]
        for row, (a, b) in zip(common, ranks, strict=True)
        if (score := table_scores(row)) is not None
    ]
    if scores:
        lines.append(f"本人平均得点差 {sum(scores) / len(scores):+,.0f}（{len(scores)} 场）")
    for row, (a, b) in reversed(list(zip(common, ranks, strict=True))[-5:]):
        stamp = datetime.fromtimestamp(float(row["starttime"]), CN)
        lines.append(f"{stamp:%m-%d %H:%M}　{a} 位 / {b} 位")
        if match := LOG_ID.search(row.get("url") or ""):
            lines.append(f"https://tenhou.net/4/?log={match[1]}")
    return "\n".join(lines)


def value(number_, *, percent=False, signed=False, decimals=1):
    number_ = number(number_)
    if number_ is None:
        return "—"
    if percent:
        return f"{number_ * 100:.1f}%"
    return f"{number_:+.{decimals}f}" if signed else f"{number_:.{decimals}f}"


def rank_text(progress):
    grade = progress.get("estimated_grade")
    if grade is None:
        return "—"
    label = grade_name(grade)
    if progress.get("pt") is not None and progress.get("pt_max") is not None:
        label += f" {progress['pt']:g} / {progress['pt_max']:g}"
    return label


def summary_text(view, *, detailed=False):
    mode = "三麻" if view["mode"] == 3 else "四麻"
    scope = view["scope"]
    window = "所选日期" if view["period_filtered"] else "最近" if view["recent"] else ""
    scope = f"{scope}{window}{view['count']}战"
    counts = view["counts"]
    room_count, mode_count = view["scope_count"], view["total_count"]
    played = f"对战数: {room_count}"
    if room_count != mode_count:
        played += f"（{mode}总场数 {mode_count}）"
    lines = [
        f"{view['name']}（{rank_text(view['progress'])}）\n{mode}　{scope}",
        "R值: "
        + value(view["rate"], decimals=0)
        + " | 最高R: "
        + value(view["highest_rate"], decimals=0),
        "最近战绩: [" + "".join(str(r) for r in view["ranks"][-10:]) + "]",
        f"{played}; 近1日/7日/30日: " + "/".join(str(n) for n in view["activity"]),
        f"1~{view['mode']}位: "
        + ", ".join(value(n / view["count"], percent=True) for n in counts)
        + f" (均顺{view['average']:.2f})",
        "连对率: "
        + value(view["top2"], percent=True)
        + " | 击飞率: "
        + value(view["negative"], percent=True),
        f"平均结算点: {view['average_result']:+.1f} | 合计: {view['total_result']:+.1f}",
        "最近对战: " + datetime.fromtimestamp(view["latest"], CN).strftime("%m-%d %H:%M"),
    ]
    deltas = view["pt_deltas"]
    if all(d is not None for d in deltas):
        lines.append(f"推算 PT 净变化: {sum(deltas):+g}")
    high = view["progress"]
    if high.get("high_grade") is not None:
        lines.insert(1, f"最高段位: {grade_name(high['high_grade'])} {high['high_pt']:g}")
    if detailed:
        p = view["phoenix"]
        if p:
            lines.extend(
                [
                    f"凤凰桌累计　{view['phoenix_scope']}（{p.get('totalrecord', '—')} 战）",
                    f"和/铳: {value(p.get('agariC'), percent=True)} / "
                    f"{value(p.get('houjuuC'), percent=True)} | "
                    f"副露/立直: {value(p.get('fuuroC'), percent=True)} / "
                    f"{value(p.get('riichC'), percent=True)}",
                    f"平均打点: {value(p.get('agariVT'), decimals=0)} | "
                    f"平均铳点: {value(p.get('houjuuVT'), decimals=0)}",
                    "和铳度差: " + value(view["difference"], signed=True) + " | "
                    "安定段: " + value(p.get("stablerank_phoenix_X"), decimals=2),
                ]
            )
            if view["tag"]:
                lines.append("牌风: #" + view["tag"])
        elif view["room"] == 3:
            lines.append(
                "凤凰桌统计：暂时不可用" if view["phoenix_error"] else "凤凰桌统计：暂无收录"
            )
    return "\n".join(lines)


class TenhouQueries:
    def __init__(self, store, client, *, manager=None, superusers=(), start_card=None):
        self.store, self.client = store, client
        self.managers = {str(user) for user in superusers}
        if manager:
            self.managers.add(str(manager))
        self.start_card = start_card
        self.gate = asyncio.Semaphore(2)

    async def account(self, name, user_id, group_id, *, legacy_last=False):
        if name:
            return name
        bound = await self.store.binding(user_id)
        if bound:
            return bound
        if legacy_last:
            names = {r["name"] for r in await self.store.subscriptions(group_id)}
            if len(names) == 1:
                return names.pop()
        raise TenhouError("请先发送 thbind <天凤昵称>，或在命令后指定昵称。")

    async def observe(self, name, options):
        async with self.gate:
            tables = await self.client.live()
            game = next(
                (
                    table
                    for table in tables
                    if int(table["info"]["playernum"]) == options.mode
                    and any(player["name"] == name for player in table["players"])
                ),
                None,
            )
            if game is None:
                return f"{name}：未在天凤公开观战列表中找到对局"
            if options.image and self.start_card:
                return await self.start_card(game)
        info = game["info"]
        stamp = datetime.fromtimestamp(float(info["starttime"]), CN)
        names = " / ".join(player["name"] for player in game["players"])
        return f"{rule_name(info)}　{stamp:%m-%d %H:%M}\n{names}\n{watch_url(info['id'])}"

    async def query(
        self, command, text, user_id, group_id, *, role="member", mentioned=None, member_ids=()
    ):
        if command == "thunbind":
            if text.strip():
                raise TenhouError("用法：thunbind")
            return "已解除绑定" if await self.store.unbind(user_id) else "尚未绑定天凤账号"
        if command == "thrank":
            return await self.ranking(text, member_ids)
        options = parse_tenhou_options(
            text, mode=3 if command == "th3" else 4, max_last=500 if command == "th" else 2000
        )
        if command in {"th3", "th4"} and options.mode != int(command[-1]):
            raise TenhouError(f"{command} 仅查询{'三麻' if command == 'th3' else '四麻'}。")
        if command in {"thbind", "th2"}:
            usage = (
                "用法：th2 @一位群友 [天凤昵称]" if command == "th2" else "用法：thbind <天凤昵称>"
            )
            if (
                options.filtered
                or options.image
                or options.observe
                or options.mode != 4
                or options.gap != 60
                or (command == "th2" and not mentioned)
                or (command == "thbind" and not options.name)
            ):
                raise TenhouError(usage)
            target = str(mentioned) if command == "th2" else str(user_id)
            if not options.name:
                bound = await self.store.binding(target)
                return f"天凤账号：{bound}" if bound else "该群友尚未绑定天凤账号。"
            if (
                target != str(user_id)
                and role not in {"admin", "owner"}
                and str(user_id) not in self.managers
            ):
                raise TenhouError("只有本人、群管理员或 Bot 管理员可以代绑账号。")
            if command == "th2" and await self.store.binding(target):
                return "该群友已绑定"
            data = await self.client.user_history(options.name)
            if not history_rows(data, options.name):
                raise TenhouError("水表网未找到该昵称的公开战绩，请检查昵称。")
            created = await self.store.bind(target, options.name, if_absent=command == "th2")
            return "绑定成功" if created else "该群友已绑定"
        if options.observe:
            if command not in {"th3", "th4"} or options.filtered or options.gap != 60:
                raise TenhouError("观战用法：th4 [玩家] -o [-i]；三麻使用 th3。")
            name = await self.account(options.name, user_id, group_id)
            return await self.observe(name, options)
        if options.gap != 60 and command != "thsession":
            raise TenhouError("gap 仅用于 thsession。")
        if command == "thstyle" and (options.last_explicit or options.start or options.end):
            raise TenhouError("thstyle 使用水表网凤凰桌累计指标，支持 -r 凤东/凤南 和 mode=3/4。")
        target = str(mentioned) if mentioned else str(user_id)
        if command == "thvs":
            if not options.name or options.image:
                raise TenhouError("用法：thvs <对手昵称> [-l 200] [-r 凤南]")
            name = await self.account("", target, group_id)
            if name == options.name:
                raise TenhouError("请指定另一位玩家。")
        else:
            name = await self.account(
                options.name, target, group_id, legacy_last=command == "thlast"
            )
        if not options.last_explicit:
            if command == "thlast":
                options.last = 1
            elif command in {"thlife", "thheat", "thstat", "thsession", "thvs"}:
                options.last = 200
        if command == "thlast" and options.last > (20 if options.image else 50):
            raise TenhouError("thlast 文本最多50场，图片最多20场。")
        recent = command not in {"th4", "th3", "thstyle"} or options.last_explicit
        async with self.gate:
            data = await self.client.user_history(name)
            view = await asyncio.to_thread(make_view, data, name, options, recent=recent)
            if command == "thlast":
                rows = view["rows"]
                if options.image:
                    from .tenhou_query_card import render_recent

                    return await asyncio.to_thread(
                        render_recent, name, rows, options.mode, pt_deltas=view["pt_deltas"]
                    )
                blocks = []
                for row, delta in reversed(list(zip(rows, view["pt_deltas"], strict=True))):
                    block = format_result(row)
                    if delta is not None:
                        block += f"\n本场推算 PT {delta:+g}"
                    blocks.append(block)
                return blocks
            if command == "thvs":
                return matchup_text(view, options.name)
            if command in {"thheat", "thstat", "thsession", "thcompare"}:
                from .tenhou_query_charts import render_activity, render_comparison

                if command == "thcompare":
                    return await asyncio.to_thread(
                        render_comparison, view, chart_records(view, use_pt=True)
                    )
                return await asyncio.to_thread(
                    render_activity, command, view, chart_records(view), options
                )
            if command == "thlife":
                from .tenhou_query_card import render_life

                return await asyncio.to_thread(render_life, view)
            detailed = options.image or command in {"th4", "th3", "thdetail", "thstyle"}
            if detailed and view["room"] == 3:
                phoenix, error, percentiles = {}, None, {}
                try:
                    phoenix = await self.client.phoenix_stats(name, options.mode, view["length"])
                except TenhouError as exc:
                    error = str(exc)
                if phoenix and (options.image or command in {"thdetail", "thstyle"}):
                    try:
                        percentiles = await self.client.phoenix_percentiles(
                            phoenix, options.mode, view["length"]
                        )
                    except TenhouError:
                        pass
                add_phoenix(view, phoenix, error, percentiles)
            if command == "thstyle":
                if view["room"] != 3:
                    raise TenhouError("水表网牌风指标覆盖凤凰桌，可用 -r 凤南 或 -r 凤东 查询。")
                if not view["phoenix"]:
                    raise TenhouError("该玩家的凤凰桌统计暂不可用。")
                from .tenhou_query_card import render_style

                return await asyncio.to_thread(render_style, view)
            if options.image or command == "thdetail":
                from .tenhou_query_card import render_profile

                return await asyncio.to_thread(render_profile, view)
            return summary_text(view, detailed=detailed)

    async def ranking(self, text, member_ids):
        mode = 3 if text.strip().lower() in {"sanma", "3", "mode=3"} else 4
        if text.strip().lower() not in {"", "4", "mode=4", "sanma", "3", "mode=3"}:
            raise TenhouError("用法：thrank [sanma]")
        names = await self.store.member_names(member_ids)
        if not names:
            return "本群还没有成员绑定天凤账号。"

        async def load(name):
            try:
                async with self.gate:
                    data = await self.client.user_history(name)
                    if not ranked_rows(data, name, mode):
                        return None
                    rank = await asyncio.to_thread(rank_history, data, name, mode, time.time() + 1)
                    if not rank.get("ranked_count"):
                        return None
                    return name, rank
            except TenhouError:
                return None

        ranks = [r for r in await asyncio.gather(*(load(name) for name in names)) if r]
        if not ranks:
            return "水表网暂时没有本群玩家的段位数据。"
        ranks.sort(
            key=lambda r: (r[1].get("estimated_grade", -1), r[1].get("pt", -1)), reverse=True
        )
        standings = [
            (
                name,
                grade_name(rank["estimated_grade"]),
                grade_name(rank.get("high_grade", rank["estimated_grade"])),
                value(rank.get("pt"), decimals=0),
                value(rank.get("pt_max"), decimals=0),
            )
            for name, rank in ranks
        ]
        return await asyncio.to_thread(
            render_rank_pages, "三麻" if mode == 3 else "四麻", standings, tenhou=True
        )
