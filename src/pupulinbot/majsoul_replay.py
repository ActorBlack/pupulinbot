"""Read the official replay response through a local, signed-in Chrome profile."""

import asyncio
import base64
import json
import re
import time
from contextlib import asynccontextmanager

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from .spectator_wire import fields, first

LOBBY_URL = "https://game.maj-soul.com/1/"
ORIGIN = LOBBY_URL + "?paipu="
RECORD_RPC = b".lq.Lobby.fetchGameRecord"
LOGIN_BEAT_RPC = b".lq.Lobby.loginBeat"

# Stay just inside the canvas corner. The lobby controls and any modal actions
# are inset from this edge. Leave the page alone if its layout changes.
LOBBY_CLICK_POINT = """
(() => {
    if (location.href !== 'https://game.maj-soul.com/1/' || !window.unityInstance) return null;
    const canvas = document.getElementById('unity-canvas');
    if (!canvas) return null;
    const rect = canvas.getBoundingClientRect();
    if (rect.width < 640 || rect.height < 360 || rect.left < 0 || rect.top < 0) return null;
    const x = Math.ceil(rect.left) + 3, y = Math.ceil(rect.top) + 3;
    return document.elementFromPoint(x, y) === canvas ? {x, y} : null;
})();
"""


class ReplayBrowserUnavailable(RuntimeError):
    pass


class MajsoulConnectionLost(RuntimeError):
    pass


def decode_record(wire, uuid):
    response = fields(first(fields(wire), 2, b""))
    if first(fields(first(response, 1, b"")), 1):
        return None
    head = fields(first(response, 3, b""))
    if first(head, 1, b"").decode() != uuid:
        return None
    accounts = [fields(data) for data in head.get(11, [])]
    result = fields(first(head, 12, b""))
    return {
        "uuid": uuid,
        "end_time": first(head, 3),
        "accounts": [
            {
                "account_id": first(account, 1),
                "seat": first(account, 2),
                "nickname": first(account, 3, b"").decode(),
            }
            for account in accounts
        ],
        "result_seats": [first(fields(player), 1) for player in result.get(1, [])],
    }


async def replay_response(page, url, uuid):
    await command(page, "Network.enable")
    await page.send(json.dumps({"id": 2, "method": "Page.navigate", "params": {"url": url}}))
    pending = set()
    expected = uuid.encode()
    async for message in page:
        event = json.loads(message)
        if event.get("id") == 2 and (
            event.get("error") or event.get("result", {}).get("errorText")
        ):
            raise MajsoulConnectionLost
        method = event.get("method")
        if method == "Network.webSocketClosed":
            socket_id = event["params"]["requestId"]
            if any(socket_id == key[0] for key in pending):
                raise MajsoulConnectionLost
        if method not in {"Network.webSocketFrameSent", "Network.webSocketFrameReceived"}:
            continue
        params = event["params"]
        socket_id = params["requestId"]
        frame = params["response"]
        if frame["opcode"] != 2:
            continue
        if method == "Network.webSocketFrameReceived" and not any(
            socket_id == key[0] for key in pending
        ):
            continue
        packet = base64.b64decode(frame["payloadData"])
        if len(packet) < 4:
            continue
        key = socket_id, packet[1:3]
        if method == "Network.webSocketFrameSent":
            if packet[0] == 2 and RECORD_RPC in packet and expected in packet:
                wrapper = fields(packet[3:])
                payload = fields(first(wrapper, 2, b""))
                if first(wrapper, 1, b"") == RECORD_RPC and first(payload, 1, b"") == expected:
                    pending.add(key)
        elif packet[0] == 3 and key in pending:
            return decode_record(packet[3:], uuid)
    raise MajsoulConnectionLost


async def command(connection, method, **params):
    await connection.send(json.dumps({"id": 1, "method": method, "params": params}))
    async with asyncio.timeout(5):
        while True:
            response = json.loads(await connection.recv())
            if response.get("id") == 1:
                if response.get("error"):
                    raise ValueError("Chrome rejected a replay operation")
                return response["result"]


@asynccontextmanager
async def chrome_browser(profile):
    try:
        port, path = (profile / "DevToolsActivePort").read_text().splitlines()[:2]
        if not 0 < int(port) <= 65535 or not re.fullmatch(r"/devtools/browser/[0-9a-f-]+", path):
            raise ValueError("invalid Chrome endpoint")
        origin = f"ws://127.0.0.1:{int(port)}"
        browser = await connect(origin + path, open_timeout=4, close_timeout=2)
    except (OSError, ValueError, WebSocketException, TimeoutError) as exc:
        raise ReplayBrowserUnavailable from exc
    try:
        yield browser, origin
    finally:
        await browser.close()


@asynccontextmanager
async def chrome_page(origin, target, *, max_size=12 * 1024 * 1024):
    if not re.fullmatch(r"[0-9a-fA-F]+", target):
        raise ValueError("invalid Chrome tab")
    async with connect(
        f"{origin}/devtools/page/{target}",
        max_size=max_size,
        open_timeout=4,
        close_timeout=2,
    ) as page:
        yield page


async def official_tab(browser, *, renew=False):
    targets = (await command(browser, "Target.getTargets"))["targetInfos"]
    pages = [t for t in targets if t["type"] == "page"]
    official = [
        t["targetId"]
        for t in pages
        if t["url"] == LOBBY_URL or t["url"].startswith((LOBBY_URL + "?", LOBBY_URL + "#"))
    ]
    blank = [
        t["targetId"] for t in pages if t["url"] in {"about:blank", "data:,", "chrome://newtab/"}
    ]
    candidates = official + blank
    if candidates and not renew:
        target, *old_tabs = candidates
    else:
        # Chrome exits if its final tab is closed. Create a blank replacement
        # first, then close the old game page before starting a new login.
        target = (await command(browser, "Target.createTarget", url="about:blank"))["targetId"]
        old_tabs = candidates
    for old_tab in old_tabs:
        result = await command(browser, "Target.closeTarget", targetId=old_tab)
        if not result.get("success"):
            raise MajsoulConnectionLost
    if old_tabs:
        async with asyncio.timeout(10):
            while True:
                remaining = (await command(browser, "Target.getTargets"))["targetInfos"]
                if all(tab["targetId"] not in old_tabs for tab in remaining):
                    break
                await asyncio.sleep(0.2)
    return target


async def fetch_replay_head(profile, url, uuid, *, wait_seconds=112):
    if not re.fullmatch(r"[A-Za-z0-9-]{8,100}", uuid) or not re.fullmatch(
        re.escape(ORIGIN + uuid) + r"(?:_a[0-9]+(?:_[0-9]+)?)?", url
    ):
        return None
    try:
        async with chrome_browser(profile) as (browser, origin):
            target = await official_tab(browser, renew=True)
            async with asyncio.timeout(wait_seconds), chrome_page(origin, target) as page:
                return await replay_response(page, url, uuid)
    except (OSError, WebSocketException, TimeoutError) as exc:
        raise MajsoulConnectionLost from exc
    except (ValueError, TypeError, IndexError, KeyError):
        return None


async def click_lobby(page):
    result = await command(
        page, "Runtime.evaluate", expression=LOBBY_CLICK_POINT, returnByValue=True
    )
    point = result.get("result", {}).get("value")
    if not point:
        return False
    for event in ("mousePressed", "mouseReleased"):
        await command(
            page, "Input.dispatchMouseEvent", type=event, button="left", clickCount=1, **point
        )
    return True


class LobbyKeepalive:
    def __init__(self, profile):
        self.profile = profile
        self.target = None
        self.watch = None
        self.last_received = 0.0

    async def click(self):
        async with chrome_browser(self.profile) as (browser, origin):
            try:
                target = await official_tab(browser, renew=not self._online())
                if self.target != target or not self._online():
                    await self.close()
                    ready = asyncio.Event()
                    self.target = target
                    self.watch = asyncio.create_task(self._watch_lobby(origin, target, ready))
                    async with asyncio.timeout(115):
                        await ready.wait()
                if self._online():
                    async with chrome_page(origin, target) as page:
                        if await click_lobby(page):
                            return True
            except (
                MajsoulConnectionLost,
                OSError,
                ValueError,
                TypeError,
                KeyError,
                WebSocketException,
                TimeoutError,
            ):
                pass
            await self.close()
            return False

    def _online(self):
        return self.last_received and time.monotonic() - self.last_received < 90

    async def _watch_lobby(self, origin, target, ready):
        pending = set()
        socket = None
        try:
            async with chrome_page(origin, target) as page:
                await command(page, "Network.enable")
                await page.send(
                    json.dumps({"id": 2, "method": "Page.navigate", "params": {"url": LOBBY_URL}})
                )
                async for message in page:
                    event = json.loads(message)
                    if event.get("id") == 2 and (
                        event.get("error") or event.get("result", {}).get("errorText")
                    ):
                        return
                    method = event.get("method")
                    params = event.get("params", {})
                    request = params.get("requestId")
                    if socket is not None and request == socket:
                        if method == "Network.webSocketClosed":
                            return
                        if method == "Network.webSocketFrameReceived":
                            self.last_received = time.monotonic()
                        continue
                    if socket is not None or method not in {
                        "Network.webSocketFrameSent",
                        "Network.webSocketFrameReceived",
                    }:
                        continue
                    frame = params["response"]
                    if frame["opcode"] != 2:
                        continue
                    packet = base64.b64decode(frame["payloadData"])
                    if len(packet) < 4:
                        continue
                    key = request, packet[1:3]
                    if method == "Network.webSocketFrameSent" and packet[0] == 2:
                        if (
                            LOGIN_BEAT_RPC in packet
                            and first(fields(packet[3:]), 1, b"") == LOGIN_BEAT_RPC
                        ):
                            pending.add(key)
                    elif packet[0] == 3 and key in pending:
                        pending.discard(key)
                        reply = fields(first(fields(packet[3:]), 2, b""))
                        if not first(fields(first(reply, 1, b"")), 1):
                            socket = request
                            self.last_received = time.monotonic()
                            ready.set()
        except (
            OSError,
            ValueError,
            TypeError,
            IndexError,
            KeyError,
            WebSocketException,
            TimeoutError,
        ):
            pass
        finally:
            self.last_received = 0.0
            ready.set()

    async def close(self):
        if self.watch:
            self.watch.cancel()
            await asyncio.gather(self.watch, return_exceptions=True)
            self.watch = None
        self.last_received = 0.0
        self.target = None
