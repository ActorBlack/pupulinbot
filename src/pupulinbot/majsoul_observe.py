"""Mahjong Soul's delayed public observer transport."""

import asyncio
import base64
import json
import re
import struct
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from .live import LiveServiceError
from .majsoul_rounds import wrapper
from .spectator_wire import fields, first

REPLAY_ID = re.compile(r"([0-9]{6}-[A-Za-z0-9-]{12,80})(?:_a[0-9]+(?:_[0-9]+)?)?")
ORIGIN = "https://game.maj-soul.com"
HEADER = struct.Struct("<HIHHI")


def watch_uuid(value):
    value = value.removeprefix("paipu=")
    if match := REPLAY_ID.fullmatch(value):
        return match[1]
    parsed = urlsplit(value)
    if parsed.scheme == "https" and parsed.netloc == "game.maj-soul.com" and parsed.path == "/1/":
        ids = parse_qs(parsed.query).get("paipu", [])
        if len(ids) == 1 and (match := REPLAY_ID.fullmatch(ids[0])):
            return match[1]
    return None


@dataclass(frozen=True, slots=True)
class Observation:
    route: str
    token: str = field(repr=False)
    delay: int = 0
    start: int = 0


def decode_observation(result):
    if "wire" not in result:
        raise LiveServiceError("雀魂观战连接暂不可用，请检查专用浏览器登录。")
    payload = fields(first(fields(bytes(result["wire"])), 2, b""))
    code = first(fields(first(payload, 1, b"")), 1)
    if code:
        raise LiveServiceError(f"雀魂未开放该桌的公开观战，或对局已结束（{code}）。")
    route = urlsplit(result["route"])
    if (
        route.scheme != "wss"
        or not route.hostname
        or not route.hostname.endswith(".maj-soul.com")
        or route.username is not None
        or route.password is not None
        or route.path
        or route.query
        or route.fragment
    ):
        raise ValueError("untrusted observer route")
    token = first(payload, 2, b"").decode()
    delay, start = first(payload, 4), first(payload, 5)
    if (
        not token
        or len(token) > 8192
        or type(delay) is not int
        or type(start) is not int
        or not start
    ):
        raise ValueError("invalid observation authorization")
    return Observation(route.geturl() + "/ob", token, delay, start)


def observer_frame(message):
    if len(message) < HEADER.size:
        raise ValueError("invalid observer frame")
    sequence, stamp, _, _, _ = HEADER.unpack_from(message)
    return sequence, stamp, *wrapper(message[HEADER.size :])


class ObserverSocket:
    def __init__(self, socket):
        self.socket = socket
        self.ordinal = 0
        self.frames = []
        self.last_sequence = None

    async def request(self, method, payload):
        self.ordinal += 1
        await self.socket.send(f"<= {method} {self.ordinal} " + json.dumps(payload))
        async with asyncio.timeout(10):
            while True:
                message = await self.socket.recv()
                if isinstance(message, bytes):
                    self.frames.append(observer_frame(message))
                    continue
                prefix, ordinal, body = message.split(" ", 2)
                if prefix == "=>" and ordinal == str(self.ordinal):
                    response = json.loads(body)
                    if response.get("error"):
                        raise LiveServiceError("雀魂观战服务拒绝连接，请确认对局仍开放观战。")
                    return response

    async def _ordered(self, frame):
        sequence = frame[0]
        if self.last_sequence is not None:
            distance = (sequence - self.last_sequence) % 65536
            if distance == 0 or distance > 32768:
                return
            if distance > 128:
                raise LiveServiceError("雀魂观战数据分段缺失。")
            for _ in range(1, distance):
                missing = (self.last_sequence + 1) % 65536
                response = await self.request("FetchSegment", {"seq": missing})
                recovered = observer_frame(base64.b64decode(response["segments"], validate=True))
                if recovered[0] != missing:
                    raise LiveServiceError("雀魂观战数据分段编号不一致。")
                self.last_sequence = missing
                yield recovered
        self.last_sequence = sequence
        yield frame

    async def events(self):
        while True:
            await self.request("FetchSequence", {"id": self.ordinal})
            buffered, self.frames = self.frames, []
            for frame in buffered:
                async for record in self._ordered(frame):
                    yield record
            end = time.monotonic() + 5
            while (remaining := end - time.monotonic()) > 0:
                try:
                    async with asyncio.timeout(remaining):
                        message = await self.socket.recv()
                except TimeoutError:
                    break
                if isinstance(message, bytes):
                    async for record in self._ordered(observer_frame(message)):
                        yield record

    async def start(self, authorization):
        while (remaining := authorization.start + authorization.delay - time.time()) > 0:
            await self.request("FetchSequence", {"id": self.ordinal})
            await asyncio.sleep(min(4, remaining))
        await self.request("StartOb", {})


@asynccontextmanager
async def observe(authorization):
    try:
        async with connect(
            authorization.route, origin=ORIGIN, open_timeout=12, close_timeout=2, ping_interval=None
        ) as socket:
            connection = ObserverSocket(socket)
            hello = await connection.request("Auth", {"token": authorization.token})
            head = hello["head"]
            if isinstance(head, str):
                head = json.loads(head)
            if (
                int(hello["start_time"]) != authorization.start
                or int(hello["delay"]) != authorization.delay
            ):
                raise ValueError("observation timing changed")
            yield head, connection
    except (OSError, ValueError, KeyError, TypeError, TimeoutError, WebSocketException) as exc:
        raise LiveServiceError("雀魂官方观战连接中断或协议异常。") from exc
