"""Shared, validated Mahjong Soul command options."""

import shlex
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
ROOMS = {
    4: {"金": [8, 9], "玉": [11, 12], "王": [15, 16]},
    3: {"金": [21, 22], "玉": [23, 24], "王": [25, 26]},
}


@dataclass
class MSOptions:
    player: str = ""
    mode: int = 4
    last: int = 200
    start: datetime | None = None
    end: datetime | None = None
    modes: list[int] | None = None
    image: bool = False
    observe: bool = False
    numeric: bool = False
    year: bool = False
    gap: int = 60
    subscription: str | None = None
    group: bool = False
    last_explicit: bool = False


def parse_options(
    text: str,
    *,
    mode: int = 4,
    now: datetime | None = None,
    single_room: bool = False,
    default_room: str | None = "玉",
    max_last: int = 2000,
    allow_all: bool = True,
) -> MSOptions:
    result = MSOptions(mode=mode)
    now = (now or datetime.now(TZ)).astimezone(TZ)
    period = None
    room = None
    length = None
    name = []
    tokens = iter(shlex.split(text))
    for token in tokens:
        if token in {"-l", "-r"}:
            value = next(tokens, None)
            if not value or value.startswith("-"):
                example = "-l 100" if token == "-l" else "-r 玉南"
                raise ValueError(f"{token} 缺少参数，例如 {example}")
            token = ("last=" if token == "-l" else "room=") + value
        if token in {"-m", "-y"}:
            if period:
                raise ValueError("-m 与 -y 只能选择一个")
            period = token
            result.year = token == "-y"
        elif token in {"-i", "-n", "-g", "-o"}:
            setattr(
                result,
                {"-i": "image", "-n": "numeric", "-g": "group", "-o": "observe"}[token],
                True,
            )
        elif token in {"-a", "-d", "-c"}:
            if result.subscription:
                raise ValueError("每次只能执行一个关注列表操作")
            result.subscription = token
        elif "=" in token:
            key, value = token.lstrip("-").split("=", 1)
            if key in {"mode", "last", "gap"}:
                number = int(value)
                if key == "mode" and number not in {3, 4}:
                    raise ValueError("mode 只能为 3 或 4")
                if key == "last" and not (0 if allow_all else 1) <= number <= max_last:
                    hint = f"；0 表示最多查询 {max_last} 场" if allow_all else ""
                    raise ValueError(f"last 范围为 1–{max_last}" + hint)
                if key == "gap" and not 1 <= number <= 1440:
                    raise ValueError("gap 范围为 1–1440 分钟")
                setattr(result, key, (number or max_last) if key == "last" else number)
                if key == "last":
                    result.last_explicit = True
            elif key in {"start", "end"}:
                date = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=TZ)
                setattr(result, key, date + timedelta(days=1) if key == "end" else date)
            elif key == "game":
                if value not in {"东", "南", "东风", "半庄"}:
                    raise ValueError("game 可选东/东风或南/半庄")
                length = "东" if value in {"东", "东风"} else "南"
            elif key in {"room", "r"}:
                room = value
            else:
                raise ValueError(f"未知选项：{key}")
        elif token.startswith("-"):
            raise ValueError(f"不支持选项：{token}")
        else:
            name.append(token)
    result.player = " ".join(name)
    if result.numeric and (not result.player.isdecimal() or int(result.player) <= 0):
        raise ValueError("-n 后的玩家参数必须是正整数 account ID")
    if period and (result.start or result.end):
        raise ValueError("-m/-y 不能与 start/end 同时使用")
    if period:
        result.start = now.replace(
            month=1 if period == "-y" else now.month,
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        result.end = now + timedelta(microseconds=1)
    if result.start and result.end and result.start >= result.end:
        raise ValueError("开始日期不能晚于结束日期")
    expanded = dict(ROOMS[result.mode])
    for base, ids in ROOMS[result.mode].items():
        expanded[base + "东"] = [ids[0]]
        expanded[base + "南"] = [ids[1]]
    if single_room:
        for base, ids in ROOMS[result.mode].items():
            expanded[base] = [ids[0 if length == "东" else 1]]
        room = room or default_room
    aliases = {
        "金之间": "金",
        "玉之间": "玉",
        "王座": "王",
        "王座之间": "王",
        "王座东": "王东",
        "王座南": "王南",
    }
    if room:
        names = [aliases.get(n, n) for n in room.replace("，", ",").split(",")]
        if single_room and len(names) != 1:
            raise ValueError("每次只能查询一个房间，例如 room=玉南 或 room=王座南")
        if any(value not in expanded for value in names):
            hint = "room 可选金南/玉南/王座南或对应东风房间"
            raise ValueError(hint if single_room else hint + "，多个房间用逗号分隔")
        result.modes = sorted({m for name in names for m in expanded[name]})
    if length:
        allowed = {ids[0 if length == "东" else 1] for ids in ROOMS[result.mode].values()}
        result.modes = sorted(set(result.modes or allowed) & allowed)
        if not result.modes:
            raise ValueError("room 与 game 的东风/半庄筛选冲突")
    if result.start:
        result.start = result.start.astimezone(UTC)
    if result.end:
        result.end = result.end.astimezone(UTC)
    return result


def level_label(value) -> str:
    from .ms_level import rank_identity, rank_label, rank_progress

    return rank_label(rank_progress(value) or rank_identity(value), full=True)
