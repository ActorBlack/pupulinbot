"""Follow one public Tenhou spectator stream and publish settled hands."""

import asyncio
import json
import logging
from collections import Counter
from contextlib import aclosing
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlsplit

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from .tenhou import WATCH_ID, TenhouError, rule_name, validate_name, watch_url

log = logging.getLogger(__name__)
SPECTATOR_SOCKET = "wss://b-wk.mjv.jp"
ROUND_TAGS = frozenset({"INIT", "UN", "AGARI", "RYUUKYOKU", "OWARI"})
DRAW_TYPES = {
    "yao9": "九种九牌",
    "kaze4": "四风连打",
    "reach4": "四家立直",
    "kan4": "四杠散了",
    "ron3": "三家和了",
    "nm": "流局满贯",
}
# Tenhou's public AGARI identifiers, shared by yaku and yakuman.
YAKU_NAMES = {
    0: "门前清自摸和",
    1: "立直",
    2: "一发",
    3: "抢杠",
    4: "岭上开花",
    5: "海底摸月",
    6: "河底捞鱼",
    7: "平和",
    8: "断幺九",
    9: "一杯口",
    10: "自风东",
    11: "自风南",
    12: "自风西",
    13: "自风北",
    14: "场风东",
    15: "场风南",
    16: "场风西",
    17: "场风北",
    18: "役牌白",
    19: "役牌发",
    20: "役牌中",
    21: "两立直",
    22: "七对子",
    23: "混全带幺九",
    24: "一气通贯",
    25: "三色同顺",
    26: "三色同刻",
    27: "三杠子",
    28: "对对和",
    29: "三暗刻",
    30: "小三元",
    31: "混老头",
    32: "二杯口",
    33: "纯全带幺九",
    34: "混一色",
    35: "清一色",
    36: "人和",
    37: "天和",
    38: "地和",
    39: "大三元",
    40: "四暗刻",
    41: "四暗刻单骑",
    42: "字一色",
    43: "绿一色",
    44: "清老头",
    45: "九莲宝灯",
    46: "纯正九莲宝灯",
    47: "国士无双",
    48: "国士无双十三面",
    49: "大四喜",
    50: "小四喜",
    51: "四杠子",
    52: "宝牌",
    53: "里宝牌",
    54: "赤宝牌",
}
LIMIT_NAMES = {1: "满贯", 2: "跳满", 3: "倍满", 4: "三倍满", 5: "累计役满"}


def watch_number(value):
    if WATCH_ID.fullmatch(value):
        return value.upper()
    try:
        url = urlsplit(value)
    except ValueError as exc:
        raise TenhouError("请输入天凤观战编号或官方观战链接。") from exc
    ids = parse_qs(url.query).get("wg", [])
    if (
        url.scheme in {"http", "https"}
        and url.netloc == "tenhou.net"
        and url.path in {"/0/", "/3/", "/4/"}
        and len(ids) == 1
        and WATCH_ID.fullmatch(ids[0])
    ):
        return ids[0].upper()
    raise TenhouError("请输入天凤观战编号或官方观战链接。")


async def _heartbeat(socket):
    while True:
        await asyncio.sleep(10)
        await socket.send("<Z/>")


async def spectator_frames(watch_id):
    # The official /4/ viewer uses the same anonymous, delayed WG subscription.
    async with connect(
        SPECTATOR_SOCKET, origin="https://tenhou.net", open_timeout=10, close_timeout=2
    ) as socket:
        await socket.send(json.dumps({"tag": "HELO", "name": "NoName", "sx": "M"}))
        async with asyncio.timeout(12):
            hello = json.loads(await socket.recv())
            if hello.get("tag") != "HELO" or hello.get("closing"):
                raise TenhouError("天凤观战握手失败。")
            await socket.send(json.dumps({"tag": "WG", "id": watch_id, "tw": 0}))
            while True:
                frame = json.loads(await socket.recv())
                if frame.get("tag") == "GO":
                    if str(frame.get("kansen")) != "1":
                        raise TenhouError("天凤未返回公开观战对局。")
                    await socket.send(json.dumps({"tag": "GOK"}))
                    break
        heartbeat = asyncio.create_task(_heartbeat(socket))
        loop = asyncio.get_running_loop()
        play_at = 0.0
        try:
            async for message in socket:
                frame = json.loads(message)
                tag = frame.get("tag")
                if tag == "INITBYLOG":
                    # A mid-game join provides the current hand's original INIT.
                    # Ignore past events in the snapshot.
                    for item in reversed(frame["childNodes"]):
                        if isinstance(item, dict) and item.get("tag") == "INIT":
                            yield item
                            break
                elif tag == "WGC":
                    items = frame["childNodes"]
                    now = loop.time()
                    if play_at <= now:
                        play_at = now
                        if items and type(items[0]) is int:
                            items = items[1:]
                    for item in items:
                        if type(item) is int:
                            play_at += max(0, item) / 1000
                        elif isinstance(item, dict) and item.get("tag") in ROUND_TAGS:
                            await asyncio.sleep(max(0, play_at - loop.time()))
                            yield item
                elif tag in ROUND_TAGS:
                    yield frame
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)


def _numbers(value):
    return [int(part) for part in value.split(",")]


def _tile_name(tile, red_fives):
    if not 0 <= tile < 136:
        return ""
    kind = tile // 4
    if kind >= 27:
        return "东南西北白发中"[kind - 27]
    red = "赤" if red_fives and tile in {16, 52, 88} else ""
    return f"{red}{kind % 9 + 1}{'万筒索'[kind // 9]}"


def _hand_value(frame, *, sanma):
    ten = _numbers(frame["ten"])
    points = f"{ten[1]:,}点"
    try:
        if frame.get("yakuman"):
            kinds = _numbers(frame["yakuman"])
            names = "、".join(YAKU_NAMES.get(kind, f"役种#{kind}") for kind in kinds)
            multiple = f"{len(kinds)}倍" if len(kinds) > 1 else ""
            return [f"役种 {names}", f"合计 {multiple}役满，{points}"]
        if not frame.get("yaku"):
            return [points]
        raw = _numbers(frame["yaku"])
        yaku = list(zip(raw[::2], raw[1::2], strict=True))
    except (AttributeError, TypeError, ValueError):
        return [points]
    if any(han < 0 for _, han in yaku):
        return [points]

    north = 0
    if sanma and frame.get("m"):
        try:
            # Extracted norths are included in the ordinary dora entry.
            north = sum(
                meld & 0x3C == 0x20 and meld >> 8 in range(120, 124)
                for meld in _numbers(frame["m"])
            )
        except (AttributeError, TypeError, ValueError):
            pass

    labels = []
    for kind, han in yaku:
        if not han:
            continue
        if kind == 52 and 0 < north <= han:
            han -= north
            if han:
                labels.append(f"宝牌{han}番")
            labels.append(f"拔北{north}番")
        else:
            labels.append(f"{YAKU_NAMES.get(kind, f'役种#{kind}')}{han}番")
    if not labels:
        return [points]

    han = sum(value for _, value in yaku)
    fu = f"{ten[0]}符" if ten[0] > 0 else ""
    limit = LIMIT_NAMES.get(ten[2]) if len(ten) > 2 else None
    level = f"（{limit}）" if limit else ""
    return [f"役种 {'、'.join(labels)}", f"合计 {han}番{fu}{level}，{points}"]


class RoundResults:
    def __init__(self, game):
        self.info = game["info"]
        self.expected = Counter(player["name"].casefold() for player in game["players"])
        self.size = int(self.info["playernum"])
        self.names = None
        self.round = None
        self.finished = False
        self.outcomes = []
        self.winners = set()

    def _settle(self):
        if not self.outcomes or self.names is None:
            return None
        index, honba = self.round
        title = f"{rule_name(self.info)} {'东南西北'[index // 4]}{index % 4 + 1}局 {honba}本场"
        points = [
            f"{name} {after * 100:,}（{(after - before) * 100:+,}）"
            for name, before, after in zip(self.names, self.start, self.scores, strict=True)
        ]
        text = "\n".join([title, "\n\n".join(self.outcomes), "", *points])
        self.outcomes.clear()
        return f"{index}:{honba}", text

    def feed(self, frame):
        tag = frame["tag"]
        if tag == "UN":
            names = tuple(
                validate_name(unquote(frame[f"n{i}"], errors="strict")) for i in range(self.size)
            )
            if Counter(name.casefold() for name in names) != self.expected:
                raise TenhouError("天凤观战桌与监控名单返回的对局不一致。")
            self.names = names
        elif tag == "INIT":
            seed = _numbers(frame["seed"])
            scores = _numbers(frame["ten"])[: self.size]
            if len(seed) < 2 or not 0 <= seed[0] < 16 or seed[1] < 0 or len(scores) != self.size:
                raise TenhouError("天凤小局起始数据格式异常。")
            notice = self._settle()
            self.round = (seed[0], seed[1])
            self.start = scores
            self.winners.clear()
            return notice
        elif tag == "OWARI":
            self.finished = True
            return self._settle()
        elif tag in {"AGARI", "RYUUKYOKU"} and self.round is not None and self.names:
            scores = _numbers(frame["sc"])[: self.size * 2]
            if len(scores) != self.size * 2:
                raise TenhouError("天凤小局结算分数格式异常。")
            if not self.outcomes:
                self.scores = scores[::2]
            if tag == "AGARI":
                winner, loser = int(frame["who"]), int(frame["fromWho"])
                if not 0 <= winner < self.size or not 0 <= loser < self.size:
                    raise TenhouError("天凤和牌座位格式异常。")
                if winner in self.winners:
                    return None
                self.winners.add(winner)
                how = "自摸" if winner == loser else f"荣和 {self.names[loser]}"
                tile = _tile_name(int(frame["machi"]), self.info.get("akaari"))
                detail = f"（{tile}）" if tile else ""
                self.outcomes.append(
                    "\n".join(
                        [
                            f"{self.names[winner]} {how}{detail}",
                            *_hand_value(frame, sanma=self.size == 3),
                        ]
                    )
                )
            else:
                self.outcomes.append(DRAW_TYPES.get(frame.get("type"), "流局"))
            self.scores = [
                score + delta for score, delta in zip(self.scores, scores[1::2], strict=True)
            ]
            self.finished = bool(frame.get("owari"))
            # Consecutive AGARI frames can describe one double ron.
            if self.finished or tag == "RYUUKYOKU":
                return self._settle()
        return None


@dataclass
class FollowedTable:
    identity: str
    game: dict
    groups: dict[str, str] = field(default_factory=dict)
    task: asyncio.Task | None = None


class TenhouLiveWatch:
    def __init__(self, store, client, monitor, send):
        self.store, self.client, self.monitor, self.send = store, client, monitor, send
        self.tables = {}

    def current(self, group):
        return next((table for table in self.tables.values() if group in table.groups), None)

    def status(self, group):
        table = self.current(group)
        if table is None:
            return "本群未开启天凤逐局播报。"
        info = table.game["info"]
        return f"逐局播报已开启\n{rule_name(info)}\n{watch_url(info['id'])}"

    async def follow(self, group, user, value):
        watch_id = watch_number(value)
        current = self.current(group)
        if current is not None:
            if current.game["info"]["id"].upper() == watch_id:
                return self.status(group)
            raise TenhouError("本群已有逐局播报，请先发送 thlive off。")
        subscribed = {sub["name"] for sub in await self.store.subscriptions(group)}
        if not subscribed:
            raise TenhouError("本群尚未监控天凤玩家。")
        games = await self.client.live()
        game = next(
            (
                g
                for g in games
                if g["info"]["id"].upper() == watch_id
                and any(p["name"] in subscribed for p in g["players"])
            ),
            None,
        )
        if game is None:
            raise TenhouError("本群监控中未找到这桌公开观战，可能已结束。")
        if self.current(group) is not None:
            return self.status(group)
        identity = f"wg:{watch_id}:{game['info']['starttime']}"
        table = self.tables.get(identity)
        if table is None:
            table = FollowedTable(identity, game)
            self.tables[identity] = table
            table.task = asyncio.create_task(self._run(watch_id, table))
        table.groups[group] = user
        return f"已开启逐局播报（约5分钟延迟）。\n{watch_url(watch_id)}"

    async def stop(self, group, user, *, manager=False):
        table = self.current(group)
        if table is None:
            return "本群未开启天凤逐局播报。"
        if table.groups[group] != user and not manager:
            raise TenhouError("只有开启者或管理员可以停止本群逐局播报。")
        del table.groups[group]
        if not table.groups and table.task:
            self.tables.pop(table.identity)
            table.task.cancel()
            await asyncio.gather(table.task, return_exceptions=True)
        return "已停止本群天凤逐局播报。"

    async def _broadcast(self, table, event, message):
        async def deliver(group):
            for delay in (0, 2):
                if delay:
                    await asyncio.sleep(delay)
                if group not in table.groups:
                    return
                try:
                    await self.monitor.deliver(group, table.identity, event, message, self.send)
                    return
                except Exception:
                    log.exception("Tenhou live delivery failed: %s %s", table.identity, event)

        await asyncio.gather(*(deliver(group) for group in tuple(table.groups)))

    async def _run(self, watch_id, table):
        try:
            async with asyncio.timeout(3 * 60 * 60):
                for delay in (0, 3, 8):
                    await asyncio.sleep(delay)
                    results = RoundResults(table.game)
                    try:
                        async with aclosing(spectator_frames(watch_id)) as frames:
                            async for frame in frames:
                                if notice := results.feed(frame):
                                    hand, text = notice
                                    await self._broadcast(table, f"round:{hand}", text)
                                if results.finished:
                                    return
                    except (
                        WebSocketException,
                        OSError,
                        TimeoutError,
                        TenhouError,
                        ValueError,
                        KeyError,
                        TypeError,
                        IndexError,
                        AttributeError,
                    ) as exc:
                        log.warning("Tenhou live stream %s: %s", watch_id, type(exc).__name__)
                await self._broadcast(table, "live_unavailable", "天凤观战已中断，逐局播报已停止。")
        except TimeoutError:
            await self._broadcast(table, "live_expired", "天凤逐局播报已结束。")
        finally:
            if self.tables.get(table.identity) is table:
                self.tables.pop(table.identity)

    async def close(self):
        tasks = [table.task for table in self.tables.values() if table.task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tables.clear()
