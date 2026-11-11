import asyncio
import io
import math
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from PIL import Image, ImageDraw

from .typography import face


class ReviewError(RuntimeError):
    pass


@dataclass(slots=True)
class ReviewResult:
    rating: float
    rank: str
    details_url: str | None
    raw: dict[str, Any]


class MortalClient:
    """Adapter for a self-hosted Mortal conversion/review HTTP service.

    The service receives a Mahjong Soul or Tenhou log URL, converts it to mjai and
    evaluates it with Mortal. Keeping that expensive stack out-of-process makes the
    bot deployable on small hosts and permits GPU-backed analyzers.
    """

    def __init__(self, url: str | None, token: str | None, timeout: float = 180):
        self.url, self.token, self.timeout = url, token, timeout
        self._http = None

    def _connection(self):
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=min(15, self.timeout)),
                follow_redirects=False,
            )
        return self._http

    async def close(self):
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def review(self, paipu_url: str, seat: int | None = None) -> ReviewResult:
        if not self.url:
            raise ReviewError("管理员尚未配置 PUPULIN_MORTAL_API_URL")
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        payload = {"url": paipu_url, "seat": seat}
        try:
            async with asyncio.timeout(self.timeout):
                client = self._connection()
                response = await client.post(
                    f"{self.url.rstrip('/')}/review", json=payload, headers=headers
                )
                response.raise_for_status()
                data = response.json()
                poll_url = None
                while True:
                    if not isinstance(data, dict):
                        raise ReviewError("Mortal 响应必须为 JSON 对象")
                    if data.get("status") not in {"queued", "running", "pending"}:
                        break
                    poll_url = data.get("status_url") or poll_url
                    if not isinstance(poll_url, str) or not poll_url:
                        raise ReviewError("Mortal 排队响应缺少 status_url")
                    poll_url = str(response.url.join(poll_url))
                    endpoint = httpx.URL(self.url)
                    target = httpx.URL(poll_url)
                    if (endpoint.scheme, endpoint.host, endpoint.port) != (
                        target.scheme,
                        target.host,
                        target.port,
                    ) or target.userinfo:
                        raise ReviewError("Mortal status_url 必须与配置的服务同源")
                    await asyncio.sleep(2)
                    response = await client.get(poll_url, headers=headers)
                    response.raise_for_status()
                    data = response.json()
        except TimeoutError as exc:
            raise ReviewError("Mortal 分析等待超时") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ReviewError(f"Mortal 服务请求失败：{exc}") from exc
        if data.get("status") == "error":
            raise ReviewError(str(data.get("error", "Mortal 分析失败")))
        if data.get("status") not in {None, "done", "success", "completed"}:
            raise ReviewError("Mortal 返回未知任务状态")
        try:
            rating = float(data.get("rating", data.get("score")))
            if not math.isfinite(rating):
                raise ValueError("non-finite rating")
        except (ValueError, TypeError) as exc:
            raise ReviewError("Mortal 响应缺少有效评分") from exc
        return ReviewResult(
            rating=rating,
            rank=str(data.get("rank", data.get("grade", "--"))),
            details_url=data.get("url") or data.get("details_url"),
            raw=data,
        )


def render_review(result: ReviewResult, nickname: str, game_uuid: str) -> bytes:
    """Render a dependency-light PNG result card suitable for QQ image messages."""
    image = Image.new("RGB", (1000, 560), "#101827")
    draw = ImageDraw.Draw(image)

    title, regular, huge = face(48, "bold"), face(28), face(96, "number")
    draw.rounded_rectangle((35, 35, 965, 525), 28, fill="#172033", outline="#4fd1c5", width=3)
    draw.text((80, 75), "Mortal Review", font=title, fill="#e7faf7")
    draw.text((80, 160), nickname[:28], font=regular, fill="#9fb3c8")
    draw.text((80, 220), f"{result.rating:.2f}", font=huge, fill="#4fd1c5")
    draw.text((620, 245), f"Rank  {result.rank}", font=regular, fill="#f6c177")
    draw.text((80, 410), f"Game  {game_uuid[:32]}", font=regular, fill="#9fb3c8")
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


class ReviewQueue:
    def __init__(self, client: MortalClient, workers: int = 1, max_size: int = 20):
        self.client, self.workers = client, workers
        self.queue: asyncio.Queue[tuple[str, int | None, asyncio.Future[ReviewResult]]] = (
            asyncio.Queue(maxsize=max_size)
        )
        self.tasks: list[asyncio.Task[None]] = []

    def start(self) -> None:
        if self.tasks:
            return
        self.tasks = [
            asyncio.create_task(self._worker(), name=f"mortal-{i}") for i in range(self.workers)
        ]

    async def close(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        while not self.queue.empty():
            _, _, future = self.queue.get_nowait()
            if not future.done():
                future.set_exception(ReviewError("Mortal 分析队列已关闭"))
            self.queue.task_done()
        close = getattr(self.client, "close", None)
        if close is not None:
            await close()

    async def submit(self, url: str, seat: int | None = None) -> ReviewResult:
        if not self.tasks:
            raise ReviewError("Mortal 分析队列尚未启动或已关闭")
        if not self.client.url:
            raise ReviewError("管理员尚未配置 PUPULIN_MORTAL_API_URL")
        future = asyncio.get_running_loop().create_future()
        try:
            self.queue.put_nowait((url, seat, future))
        except asyncio.QueueFull as exc:
            raise ReviewError("Mortal 分析队列已满，请稍后再试") from exc
        return await future

    async def _worker(self) -> None:
        while True:
            url, seat, future = await self.queue.get()
            try:
                if future.cancelled():
                    continue
                result = await self.client.review(url, seat)
                if not future.done():
                    future.set_result(result)
            except asyncio.CancelledError:
                if not future.done():
                    future.set_exception(ReviewError("Mortal 分析队列已关闭"))
                raise
            except Exception as exc:
                if not future.done():
                    future.set_exception(exc)
            finally:
                self.queue.task_done()


def extract_uuid(text: str) -> str:
    marker = "paipu="
    value = text.split(marker, 1)[1] if marker in text else text.strip()
    value = value.split("&", 1)[0].strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,200}", value):
        raise ValueError("请输入雀魂牌谱 UUID 或完整牌谱链接")
    return value


@dataclass(frozen=True, slots=True)
class ReviewLog:
    source: str
    game_id: str
    url: str
    seat: int

    @property
    def online_url(self):
        key = "rcid" if self.source == "riichi_city" else "url"
        return "https://mjai.ekyu.moe/?" + urlencode({key: self.url})


def parse_review_link(text: str, seat: int | None = None) -> ReviewLog:
    """Normalize official URLs or a Riichi City ID, preserving the target seat."""
    if seat is not None and (type(seat) is not int or seat not in range(4)):
        raise ReviewError("座位必须是0–3")
    text = text.strip().strip(chr(34) + chr(39) + "「」『』<>“”")
    city = re.fullmatch(r"([a-z0-9]{20})(?:@([0-3]))?", text)
    if city:
        target = seat if seat is not None else int(city[2] or 0)
        return ReviewLog("riichi_city", city[1], f"{city[1]}@{target}", target)
    tenhou_id = r"[0-9]{10}gm-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{8}"
    if re.fullmatch(tenhou_id, text, re.IGNORECASE):
        text = "https://tenhou.net/0/?log=" + text
    parsed = urlsplit(text)
    if parsed.scheme or parsed.netloc:
        query = parse_qs(parsed.query, keep_blank_values=True)
        if (
            parsed.scheme in {"http", "https"}
            and parsed.netloc in {"tenhou.net", "www.tenhou.net"}
            and parsed.path in {"/0", "/0/", "/3", "/3/", "/4", "/4/", "/5", "/5/", "/6", "/6/"}
        ):
            logs = query.get("log", [])
            if len(logs) != 1 or not re.fullmatch(tenhou_id, logs[0], re.IGNORECASE):
                raise ReviewError("天凤牌谱 log 编号无效")
            game_id = logs[0].lower()
            if seat is None:
                seats = query.get("tw", ["0"])
                if len(seats) != 1 or seats[0] not in {"0", "1", "2", "3"}:
                    raise ReviewError("天凤链接的 tw 座位必须是0–3")
                seat = int(seats[0])
            url = "https://tenhou.net/0/?" + urlencode({"log": game_id, "tw": seat})
            return ReviewLog("tenhou", game_id, url, seat)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "game.maj-soul.com"
            or parsed.path not in {"/1", "/1/"}
            or len(query.get("paipu", [])) != 1
        ):
            raise ReviewError("只接受雀魂/天凤官方链接或一番街牌谱 ID")
        text = query["paipu"][0]
    try:
        value = extract_uuid(text)
    except ValueError as exc:
        raise ReviewError("请输入雀魂/天凤牌谱链接或一番街牌谱 ID（如 ID@0）") from exc
    game_id = re.sub(r"_a[0-9]+(?:_[0-9]+)?$", "", value)
    if not re.fullmatch(r"[A-Za-z0-9-]{8,100}", game_id):
        raise ReviewError("牌谱编号格式无效")
    return ReviewLog("majsoul", game_id, "https://game.maj-soul.com/1/?paipu=" + value, seat or 0)
