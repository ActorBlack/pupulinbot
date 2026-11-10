"""Short English options for independent Tenhou queries."""

import shlex
from dataclasses import dataclass
from datetime import datetime, timedelta

from .tenhou import CN, TenhouError, validate_name

ROOMS = ("般", "上", "特", "鳳")


@dataclass
class TenhouOptions:
    name: str = ""
    mode: int = 4
    last: int = 100
    last_explicit: bool = False
    room: int | None = None
    length: int | None = None
    start: float | None = None
    end: float | None = None
    image: bool = False

    @property
    def filtered(self):
        return (
            self.last_explicit
            or self.room is not None
            or self.length is not None
            or any(v is not None for v in (self.start, self.end))
        )


def room_option(value):
    value = value.replace("东", "東").replace("凤", "鳳")
    length = 1 if value.endswith("東") else 2
    value = value.removesuffix("東").removesuffix("南")
    aliases = {
        "一般": 0,
        "般": 0,
        "上": 1,
        "上级": 1,
        "上級": 1,
        "特": 2,
        "特上": 2,
        "鳳": 3,
        "鳳凰": 3,
    }
    if value not in aliases:
        raise TenhouError("房间请使用 -r 般/般东/上/上东/特/特东/凤/凤东；省略东南默认南。")
    return aliases[value], length


def parse_tenhou_options(text, *, mode=4, max_last=500, now=None):
    options = TenhouOptions(mode=mode)
    try:
        parts = shlex.split(text)
    except ValueError as exc:
        raise TenhouError("昵称引号未闭合。") from exc
    names, seen = [], set()
    now = now or datetime.now(CN)
    index = 0
    period = None
    while index < len(parts):
        word = parts[index]
        index += 1
        if word in {"-i", "-m", "-y"}:
            key, value = word, ""
        elif word in {"-l", "-r"}:
            if index == len(parts):
                raise TenhouError(f"{word} 后需要填写参数。")
            key, value = word, parts[index]
            index += 1
        elif "=" in word:
            key, value = word.split("=", 1)
            key = {"last": "-l", "room": "-r"}.get(key, key)
        elif word.startswith("-"):
            raise TenhouError(f"不支持的参数：{word}")
        else:
            names.append(word)
            continue
        if key in seen:
            raise TenhouError(f"参数重复：{key}")
        seen.add(key)
        if key == "-i":
            options.image = True
        elif key == "-l":
            if not value.isdecimal() or int(value) > max_last:
                raise TenhouError(f"-l 场数范围为 1–{max_last}，0 表示最多 {max_last} 场。")
            options.last, options.last_explicit = int(value) or max_last, True
        elif key == "-r":
            options.room, options.length = room_option(value)
        elif key == "mode":
            if value not in {"3", "4"}:
                raise TenhouError("mode 仅支持 3 或 4。")
            options.mode = int(value)
        elif key == "game":
            if value not in {"东", "東", "南"}:
                raise TenhouError("game 仅支持 东 或 南。")
            length = 2 if value == "南" else 1
            if options.length is not None and options.length != length:
                raise TenhouError("game 与房间的东南设置不一致。")
            options.length = length
        elif key in {"start", "end"}:
            try:
                stamp = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=CN)
            except ValueError as exc:
                raise TenhouError("日期格式：start=2026-11-01 end=2026-11-10") from exc
            if key == "end":
                stamp += timedelta(days=1)
            setattr(options, key, stamp.timestamp())
        elif key in {"-m", "-y"}:
            if period is not None:
                raise TenhouError("-m 和 -y 只能选一个。")
            period = key
        else:
            raise TenhouError(f"不支持的参数：{key}")
    if period:
        if options.start is not None or options.end is not None:
            raise TenhouError("-m/-y 不能与 start/end 同时使用。")
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if period == "-y":
            start = start.replace(month=1)
        options.start, options.end = start.timestamp(), now.timestamp()
    if options.start is not None and options.end is not None and options.start >= options.end:
        raise TenhouError("开始日期不能晚于结束日期。")
    if options.room is not None and "game" in seen:
        game = next(p.split("=", 1)[1] for p in parts if p.startswith("game="))
        if options.length != (2 if game == "南" else 1):
            raise TenhouError("game 与房间的东南设置不一致。")
    options.name = validate_name(" ".join(names)) if names else ""
    return options
