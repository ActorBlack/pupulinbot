import asyncio
import io
from dataclasses import dataclass
from typing import Any

import httpx
from PIL import Image, ImageDraw, ImageFont


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

    The service receives a Mahjong Soul log URL, converts it to mjai internally and
    evaluates it with Mortal. Keeping that expensive stack out-of-process makes the
    bot deployable on small hosts and permits GPU-backed analyzers.
    """

    def __init__(self, url: str | None, token: str | None, timeout: float = 180):
        self.url, self.token, self.timeout = url, token, timeout

    async def review(self, paipu_url: str, seat: int | None = None) -> ReviewResult:
        if not self.url:
            raise ReviewError("管理员尚未配置 PUPULIN_MORTAL_API_URL")
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        payload = {"url": paipu_url, "seat": seat}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.url.rstrip('/')}/review", json=payload, headers=headers
                )
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ReviewError(f"Mortal 服务请求失败：{exc}") from exc
        if data.get("status") == "error":
            raise ReviewError(str(data.get("error", "Mortal 分析失败")))
        return ReviewResult(
            rating=float(data.get("rating", data.get("score", 0))),
            rank=str(data.get("rank", data.get("grade", "--"))),
            details_url=data.get("url") or data.get("details_url"),
            raw=data,
        )


def render_review(result: ReviewResult, nickname: str, game_uuid: str) -> bytes:
    """Render a dependency-light PNG result card suitable for QQ image messages."""
    image = Image.new("RGB", (1000, 560), "#101827")
    draw = ImageDraw.Draw(image)
    try:
        title = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 48)
        regular = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 28)
        huge = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 96)
    except OSError:
        title = regular = huge = ImageFont.load_default()
    draw.rounded_rectangle((35, 35, 965, 525), 28, fill="#172033", outline="#4fd1c5", width=3)
    draw.text((80, 75), "Mortal Review", font=title, fill="#e7faf7")
    draw.text((80, 160), nickname, font=regular, fill="#9fb3c8")
    draw.text((80, 220), f"{result.rating:.2f}", font=huge, fill="#4fd1c5")
    draw.text((620, 245), f"Rank  {result.rank}", font=regular, fill="#f6c177")
    draw.text((80, 410), f"Game  {game_uuid[:32]}", font=regular, fill="#9fb3c8")
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


class ReviewQueue:
    def __init__(self, client: MortalClient, workers: int = 1):
        self.client, self.workers = client, workers
        self.queue: asyncio.Queue[tuple[str, int | None, asyncio.Future[ReviewResult]]] = (
            asyncio.Queue()
        )
        self.tasks: list[asyncio.Task[None]] = []

    def start(self) -> None:
        self.tasks = [
            asyncio.create_task(self._worker(), name=f"mortal-{i}") for i in range(self.workers)
        ]

    async def close(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def submit(self, url: str, seat: int | None = None) -> ReviewResult:
        future = asyncio.get_running_loop().create_future()
        await self.queue.put((url, seat, future))
        return await future

    async def _worker(self) -> None:
        while True:
            url, seat, future = await self.queue.get()
            try:
                future.set_result(await self.client.review(url, seat))
            except Exception as exc:
                future.set_exception(exc)
            finally:
                self.queue.task_done()


def extract_uuid(text: str) -> str:
    marker = "paipu="
    value = text.split(marker, 1)[1] if marker in text else text.strip()
    value = value.split("&", 1)[0].strip()
    if not value or any(ch.isspace() for ch in value):
        raise ValueError("请输入雀魂牌谱 UUID 或完整牌谱链接")
    return value
