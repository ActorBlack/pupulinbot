"""Bound-account Tenhou profiles and filtered public-record queries."""

import asyncio
import time
from datetime import datetime

from .ms_sample import number
from .rate_tags import rate_tag
from .tenhou import CN, TenhouError, format_result, grade_name, history_rows, table_scores
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
    if recent and room is None:
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
    return (filtered[-options.last :] if recent else filtered), matching, scope, length


def make_view(
    data, name, options, *, recent, now=None, phoenix=None, phoenix_error=None, percentiles=None
):
    now = now or time.time()
    progress = rank_history(data, name, options.mode, now + 1, include_records=True)
    current_records = progress.pop("current_records", [])
    all_rows = ranked_rows({**data, "list": current_records}, name, options.mode)
    rows, activity_rows, scope, length = select_rows(all_rows, options, recent=recent)
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
    phoenix = phoenix or {}
    win, deal = (number(phoenix.get(k)) for k in ("agariC", "houjuuC"))
    win_pt, deal_pt = (number(phoenix.get(k)) for k in ("agariVT", "houjuuVT"))
    difference = (
        win * win_pt - deal * deal_pt
        if all(v is not None for v in (win, deal, win_pt, deal_pt))
        else None
    )
    return dict(
        name=name,
        mode=options.mode,
        scope=scope,
        recent=recent,
        rows=rows,
        count=len(rows),
        total_count=len(all_rows),
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
        phoenix=phoenix,
        phoenix_scope=("三" if options.mode == 3 else "四")
        + "鳳"
        + ("东" if length == 1 else "南" if length == 2 else "东/南"),
        phoenix_error=phoenix_error,
        percentiles=percentiles or {},
        difference=difference,
        tag=rate_tag(win, deal, mode=options.mode),
    )


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
    if scope == "全部房间":
        scope = ""
    scope = f"{scope}最近{view['count']}战" if view["recent"] else f"{scope}{view['count']}战"
    counts = view["counts"]
    lines = [
        f"{view['name']}（{rank_text(view['progress'])}）\n{mode}　{scope}",
        "R值: "
        + value(view["rate"], decimals=0)
        + " | 最高R: "
        + value(view["highest_rate"], decimals=0),
        "最近战绩: [" + "".join(str(r) for r in view["ranks"][-10:]) + "]",
        f"对战数: {view['total_count']}; 近1日/7日/30日: "
        + "/".join(str(n) for n in view["activity"]),
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
    high = view["progress"]
    if high.get("high_grade") is not None:
        lines.insert(1, f"最高段位: {grade_name(high['high_grade'])} {high['high_pt']:g}")
    if detailed:
        p = view["phoenix"]
        if p:
            lines.extend(
                [
                    f"凤凰桌统计　{view['phoenix_scope']}（{p.get('totalrecord', '—')} 战）",
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
        else:
            lines.append(
                "凤凰桌统计：暂时不可用" if view["phoenix_error"] else "凤凰桌统计：暂无收录"
            )
    return "\n".join(lines)


class TenhouQueries:
    def __init__(self, store, client, *, manager=None):
        self.store, self.client = store, client
        self.manager = str(manager) if manager else None
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

    async def query(self, command, text, user_id, group_id, *, role="member", mentioned=None):
        if command == "thunbind":
            if text.strip():
                raise TenhouError("用法：thunbind")
            return "已解除绑定" if await self.store.unbind(user_id) else "尚未绑定天凤账号"
        if command == "thrank":
            return await self.ranking(text, group_id)
        options = parse_tenhou_options(text, mode=3 if command == "th3" else 4)
        if command in {"th3", "th4"} and options.mode != int(command[-1]):
            raise TenhouError(f"{command} 仅查询{'三麻' if command == 'th3' else '四麻'}。")
        if command == "thbind" or (command == "th2" and mentioned and options.name):
            if not options.name or options.filtered or options.image or options.mode != 4:
                raise TenhouError(
                    f"用法：{command} " + ("@群友 " if command == "th2" else "") + "<天凤昵称>"
                )
            target = str(mentioned) if mentioned else str(user_id)
            if (
                target != str(user_id)
                and role not in {"admin", "owner"}
                and str(user_id) != self.manager
            ):
                raise TenhouError("只有本人、群管理员或 Bot 负责人可以代绑账号。")
            if command == "th2" and await self.store.binding(target):
                return "该群友已绑定"
            data = await self.client.user_history(options.name)
            if not history_rows(data, options.name):
                raise TenhouError("水表网未找到该昵称的公开战绩，请检查昵称。")
            created = await self.store.bind(target, options.name, if_absent=command == "th2")
            return "绑定成功" if created else "该群友已绑定"
        target = str(mentioned) if mentioned else str(user_id)
        name = await self.account(options.name, target, group_id, legacy_last=command == "thlast")
        if command == "thlast" and not options.last_explicit:
            options.last = 1
        if command == "thlast" and options.last > (20 if options.image else 50):
            raise TenhouError("thlast 文本最多50场，图片最多20场。")
        recent = command not in {"th4", "th3", "th2"} or options.filtered
        async with self.gate:
            data = await self.client.user_history(name)
            if command == "thlast" and not options.filtered:
                # Preserve the existing latest-game command across room changes.
                rows = ranked_rows(data, name, options.mode)[-1:]
            else:
                rows = None
            view = await asyncio.to_thread(make_view, data, name, options, recent=recent)
            if command == "thlast":
                rows = rows if rows is not None else view["rows"]
                if not rows:
                    raise TenhouError("没有收录战绩。")
                if options.image:
                    from .tenhou_query_card import render_recent

                    return await asyncio.to_thread(render_recent, name, rows, options.mode)
                return "\n\n".join(format_result(r) for r in reversed(rows))
            if command == "thlife":
                from .tenhou_query_card import render_life

                return await asyncio.to_thread(render_life, view)
            detailed = command in {"th4", "th3", "th2"} or options.image or command == "thdetail"
            if detailed:
                length = (
                    None
                    if not options.filtered and not recent
                    else (
                        int(view["rows"][-1]["playlength"])
                        if options.length is None
                        else options.length
                    )
                )
                phoenix, error, percentiles = {}, None, {}
                try:
                    phoenix = await self.client.phoenix_stats(name, options.mode, length)
                except TenhouError as exc:
                    error = str(exc)
                if phoenix and (options.image or command == "thdetail"):
                    try:
                        percentiles = await self.client.phoenix_percentiles(
                            phoenix, options.mode, length
                        )
                    except TenhouError:
                        pass
                view = await asyncio.to_thread(
                    make_view,
                    data,
                    name,
                    options,
                    recent=recent,
                    phoenix=phoenix,
                    phoenix_error=error,
                    percentiles=percentiles,
                )
            if options.image or command == "thdetail":
                from .tenhou_query_card import render_profile

                return await asyncio.to_thread(render_profile, view)
            return summary_text(view, detailed=detailed)

    async def ranking(self, text, group_id):
        mode = 3 if text.strip().lower() in {"sanma", "3", "mode=3"} else 4
        if text.strip().lower() not in {"", "4", "mode=4", "sanma", "3", "mode=3"}:
            raise TenhouError("用法：thrank [sanma]")
        names = list(dict.fromkeys(r["name"] for r in await self.store.subscriptions(group_id)))
        if not names:
            return "本群天凤监控名单为空。"
        limit = asyncio.Semaphore(2)

        async def load(name):
            try:
                async with limit:
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
        return "\n".join(
            [f"本群天凤{'三麻' if mode == 3 else '四麻'}排行"]
            + [f"{name} {rank_text(rank)}" for name, rank in ranks[:20]]
        )
