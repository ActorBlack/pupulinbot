"""Read existing public NAGA reports; reproduce the official viewer's metrics."""

import asyncio
import io
import json
import math
import re
import time
import zlib
from collections import OrderedDict
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

import httpx
from PIL import Image, ImageDraw

from .typography import profile_face as face

HOST = "naga.dmv.nico"
MAX_BYTES = 32 * 1024 * 1024
TILES = [f"{n}{s}" for s in "mps" for n in range(1, 10)] + list("ESWNPFC")
DETAIL_TILES = TILES + ["5mr", "5pr", "5sr"]
PALETTE = ("#2563eb", "#0d9488", "#d97706", "#7c3aed", "#be185d", "#475569", "#059669")


class NagaError(ValueError):
    pass


def report_id(url):
    try:
        parts = urlsplit(url.strip())
        if (
            parts.scheme != "https"
            or parts.netloc != HOST
            or parts.path not in {"/htmls/report_viewer.html", "/htmls/report_viewer_v2.html"}
        ):
            raise ValueError
        ids = parse_qs(parts.query).get("report_id", [])
        if len(ids) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]{8,160}", ids[0]):
            raise ValueError
        return ids[0]
    except ValueError:
        raise NagaError("请提供有效的 NAGA 报告链接（包含 report_id）。") from None


def command_url(text):
    args = text.strip().split()
    if len(args) != 2 or args[0] != "-r":
        raise NagaError("用法：naga -r <NAGA报告链接>")
    report_id(args[1])
    return args[1]


def tile_index(tile):
    return TILES.index(tile.removesuffix("r"))


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("invalid number")
    return value


@dataclass
class NagaPlayer:
    name: str
    score: int
    seat: int
    reach: int = 0
    calls: int = 0
    wins: int = 0
    deal_ins: int = 0
    decisions: int = 0
    metrics: list = field(default_factory=list)


@dataclass
class NagaReport:
    identity: str
    game: str
    models: list[str]
    players: list[NagaPlayer]
    rounds: int


def _model_action(step, model, predicted, detail):
    """Official own-turn comparison for newer models with 37-tile details."""
    msg = step["info"]["msg"]
    actions = []
    for key, name in (("hora", "hora"), ("kyushu", "kyushu")):
        values = step.get(key, {})
        if key == "hora":
            values = values.get(str(msg["actor"]), [])
        if values and values[model] is not None:
            actions.append((name, number(values[model])))
    for key in ("ankan_pai", "kakan_pai"):
        values = step.get(key)
        if values and values[model]:
            actions.extend((key, number(v)) for v in values[model].values())
    action = "dahai"
    if detail and predicted in DETAIL_TILES:
        idx = DETAIL_TILES.index(predicted)
        discard, reach = (number(v[idx]) for v in detail)
        if reach > discard:
            action = "reach"
        best = max(discard, reach)
    else:
        best = 10000 - sum(v for _, v in actions)
    for name, value in actions:
        if value > best:
            action, best = name, value
    return action


def _parse(data, identity):
    rounds = data["pred"]
    if not isinstance(rounds, list) or not 1 <= len(rounds) <= 1000:
        raise ValueError("missing rounds")
    names = data["player_info"]["name"]
    if len(names) != 4 or any(
        not isinstance(n, str) or not n.strip() or len(n) > 100 for n in names
    ):
        raise ValueError("missing player names")
    types = data.get("naga_types", {"0": "ω"})
    if not isinstance(types, dict) or not 1 <= len(types) <= 7:
        raise ValueError("unknown models")
    if set(types) != {str(i) for i in range(len(types))}:
        raise ValueError("noncontiguous models")
    models = [str(types[str(i)])[:24] for i in range(len(types))]
    # The official viewer detects and wraps old single-model vectors.
    legacy = next(
        (
            isinstance(v, (int, float))
            for r in rounds
            for s in r
            for v in s.get("dahai_pred", [])
            if v is not None
        ),
        False,
    )
    if legacy and len(models) != 1:
        raise ValueError("legacy model count")
    new_types = [
        any(
            s.get("dahai_detail", [])[m] is not None
            for r in rounds
            for s in r
            if s.get("dahai_detail")
        )
        for m in range(len(models))
    ]
    players = [NagaPlayer(name, 0, seat) for seat, name in enumerate(names)]
    mismatches = [[0] * len(models) for _ in names]
    losses = [[0.0] * len(models) for _ in names]
    bad = [[0] * len(models) for _ in names]
    for events in rounds:
        called = set()
        for step in events:
            msg = step["info"]["msg"]
            kind = msg["type"]
            actor = msg.get("actor")
            if actor is not None and (type(actor) is not int or actor not in range(4)):
                raise ValueError("invalid seat")
            if kind == "reach_accepted":
                players[actor].reach += 1
            if kind in {"chi", "pon", "daiminkan"}:
                called.add(actor)
            real = msg.get("real_dahai")
            if kind not in {"tsumo", "chi", "pon"} or not real or real == "?" or msg.get("reached"):
                continue
            player = players[actor]
            player.decisions += 1
            actual_idx = tile_index(real)
            predictions = msg.get("pred_dahai", [])
            vectors = step.get("dahai_pred", [])
            if legacy:
                predictions, vectors = [predictions], [vectors]
            for model in range(len(models)):
                predicted = predictions[model]
                if predicted is None:
                    continue
                detail = step.get("dahai_detail", [None] * len(models))[model]
                if new_types[model] and _model_action(step, model, predicted, detail) not in {
                    "dahai",
                    "reach",
                }:
                    continue
                predicted_idx = tile_index(predicted)
                if detail:
                    vector = [number(a) + number(b) for a, b in zip(*detail, strict=True)]
                    if len(vector) != 37:
                        raise ValueError("invalid detail vector")
                    for red, normal in ((34, 4), (35, 13), (36, 22)):
                        vector[normal] += vector[red]
                else:
                    vector = vectors[model]
                if not vector or len(vector) < 34:
                    raise ValueError("missing prediction probabilities")
                actual, best = number(vector[actual_idx]), number(vector[predicted_idx])
                if not (0 <= actual <= 10000 and 0 <= best <= 10000):
                    raise ValueError("invalid probability")
                if actual_idx != predicted_idx:
                    mismatches[actor][model] += 1
                    losses[actor][model] += abs(actual - best) / 10000
                    bad[actor][model] += actual < 500
        for actor in called:
            players[actor].calls += 1
        ends = events[0]["info"]["msg"]["end_msgs"]
        if ends and ends[0]["type"] == "hora":
            for end in ends:
                players[end["actor"]].wins += 1
                if end["actor"] != end["target"]:
                    players[end["target"]].deal_ins += 1
    final = rounds[-1][0]["info"]["msg"]["end_msgs"][-1]["scores"]
    if len(final) != 4:
        raise ValueError("missing final scores")
    for player in players:
        player.score = int(number(final[player.seat]))
        n, seat = player.decisions, player.seat
        player.metrics = [
            (
                (n - losses[seat][m]) / n * 100,
                (n - mismatches[seat][m]) / n * 100,
                bad[seat][m] / n * 100,
            )
            if n
            else (None, None, None)
            for m in range(len(models))
        ]
    return NagaReport(
        identity,
        str(data.get("haihu_id") or "自定义牌谱"),
        models,
        sorted(players, key=lambda p: (-p.score, p.seat)),
        len(rounds),
    )


def parse_report(data, identity):
    try:
        return _parse(data, identity)
    except (KeyError, IndexError, TypeError, ValueError, AttributeError, OverflowError):
        raise NagaError("报告数据不完整或格式暂不支持，无法生成分析图。") from None


def render_report(report):
    margin, name_width, score_width, count_width, metric_width = 30, 220, 120, 72, 100
    widths = (
        [name_width, score_width]
        + [count_width] * 4
        + [metric_width] * (3 * len(report.models))
        + [90]
    )
    width = sum(widths) + margin * 2
    image = Image.new("RGB", (width, 570), "#f2f5fa")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((12, 12, width - 12, 558), 20, fill="white")

    def text(value, x, y, size=23, color="#42526b", bold=False, anchor="mm", max_width=None):
        value = str(value)
        size += int(bold)
        font = face(size)
        if max_width:
            while draw.textlength(value, font=font) > max_width and size > 16:
                size -= 1
                font = face(size)
            if draw.textlength(value, font=font) > max_width:
                while value and draw.textlength(value + "…", font=font) > max_width:
                    value = value[:-1]
                value += "…"
        draw.text((x, y), value, font=font, fill=color, anchor=anchor)

    text("NAGA 报告分析", margin, 51, 32, "#2e3b50", True, "lm")
    text(f"牌谱：{report.game}", margin, 96, 21, "#718096", anchor="lm", max_width=width - 60)
    x = margin
    for i, player in enumerate(report.players):
        chip_width = min(320, (width - margin * 2 - 36) // 4)
        draw.rounded_rectangle(
            (x, 125, x + chip_width, 173), 9, fill="#eefbf3" if i == 0 else "#f4f6fa"
        )
        text(
            f"{player.name}  {player.score}",
            x + 14,
            149,
            22,
            "#168348" if i == 0 else "#475569",
            i == 0,
            "lm",
            chip_width - 28,
        )
        x += chip_width + 12
    edges = [margin]
    for w in widths:
        edges.append(edges[-1] + w)
    top, mid, body, row_height = 194, 238, 278, 55
    draw.rectangle((margin, top, edges[-1], body), fill="#334155")
    text("玩家 · 最终得分", (edges[0] + edges[2]) / 2, 236, 23, "white", True)
    for i, label in enumerate(("立直", "副露", "和了", "放铳"), 2):
        text(label, (edges[i] + edges[i + 1]) / 2, 218, 21, "white", True)
        text("次数" if i != 3 else "局数", (edges[i] + edges[i + 1]) / 2, 259, 18, "#cbd5e1")
    for m, model in enumerate(report.models):
        a = 6 + m * 3
        draw.rectangle((edges[a], top, edges[a + 3], mid), fill=PALETTE[m])
        text(model + "模型", (edges[a] + edges[a + 3]) / 2, 216, 23, "white", True, max_width=290)
        draw.rectangle((edges[a], mid, edges[a + 3], body), fill="#edf2fa")
        for j, label in enumerate(("Naga度", "一致率", "恶手率")):
            text(label, (edges[a + j] + edges[a + j + 1]) / 2, 258, 20, bold=True)
    text("择牌数", (edges[-2] + edges[-1]) / 2, 236, 21, "white", True)
    for i, player in enumerate(report.players):
        y = body + i * row_height
        draw.rectangle(
            (margin, y, edges[-1], y + row_height), fill="#ffffff" if i % 2 == 0 else "#f6f8fc"
        )
        values = [
            player.name,
            player.score,
            player.reach,
            player.calls,
            player.wins,
            player.deal_ins,
        ]
        values += [
            "—" if value is None else f"{value:.2f}"
            for metrics in player.metrics
            for value in metrics
        ]
        values += [player.decisions]
        for col, value in enumerate(values):
            color = "#2e3b50" if col == 0 else "#42526b"
            bold = col in {0, 1, len(values) - 1}
            if col == 1 and i == 0:
                color = "#168348"
            if col == 5 and player.deal_ins:
                color = "#dc2626"
            if 6 <= col < len(values) - 1 and (col - 6) % 3 == 0 and value != "—":
                rate = float(value)
                color = (
                    "#cd9200"
                    if rate >= 90
                    else "#168348"
                    if rate >= 85
                    else "#bd550b"
                    if rate >= 75
                    else "#dc2626"
                )
                bold = True
            text(
                value,
                edges[col] + 14 if col == 0 else (edges[col] + edges[col + 1]) / 2,
                y + row_height / 2,
                23,
                color,
                bold,
                "lm" if col == 0 else "mm",
                widths[col] - 24,
            )
        draw.line((margin, y + row_height, edges[-1], y + row_height), fill="#dce4ef")
    for i, edge in enumerate(edges):
        line_top = top
        if i == 1:
            line_top = body
        elif 6 <= i < len(edges) - 2 and (i - 6) % 3:
            line_top = mid
        draw.line((edge, line_top, edge, body + 4 * row_height), fill="#dce4ef")
    text(f"NAGA · {report.rounds} 局", margin, 530, 19, "#94a3b8", anchor="lm")
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


class NagaClient:
    def __init__(self, transport=None):
        self.http = httpx.AsyncClient(
            transport=transport, timeout=httpx.Timeout(60, connect=15), follow_redirects=False
        )
        self.slots = asyncio.Semaphore(2)
        self.cache = OrderedDict()
        self.tasks = {}

    async def picture(self, url):
        identity = report_id(url)
        cached = self.cache.get(identity)
        if cached and cached[0] > time.monotonic():
            self.cache.move_to_end(identity)
            return cached[1]
        task = self.tasks.get(identity)
        if task is None:
            task = asyncio.create_task(self._load(identity))
            self.tasks[identity] = task

            def finished(done):
                if not done.cancelled():
                    done.exception()
                if self.tasks.get(identity) is done:
                    del self.tasks[identity]

            task.add_done_callback(finished)
        try:
            return await asyncio.shield(task)
        finally:
            if task.done() and self.tasks.get(identity) is task:
                del self.tasks[identity]

    async def _load(self, identity):
        try:
            async with self.slots:
                async with (
                    asyncio.timeout(75),
                    self.http.stream(
                        "GET", f"https://{HOST}/reports/{identity}.json.gz"
                    ) as response,
                ):
                    if response.status_code == 404:
                        raise NagaError("报告不存在或已过期，请检查 NAGA 链接。")
                    response.raise_for_status()
                    raw = bytearray()
                    async for chunk in response.aiter_bytes(65536):
                        raw.extend(chunk)
                        if len(raw) > MAX_BYTES:
                            raise NagaError("报告过大，暂不支持生成图片。")
                picture = await asyncio.to_thread(self._decode_render, bytes(raw), identity)
                self.cache[identity] = (time.monotonic() + 3600, picture)
                self.cache.move_to_end(identity)
                while len(self.cache) > 12:
                    self.cache.popitem(last=False)
                return picture
        except NagaError:
            raise
        except (httpx.HTTPError, TimeoutError):
            raise NagaError("NAGA 报告读取失败，请稍后重试。") from None

    @staticmethod
    def _decode_render(raw, identity):
        try:
            if raw.startswith(b"\x1f\x8b"):
                decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
                raw = decoder.decompress(raw, MAX_BYTES + 1)
                if len(raw) > MAX_BYTES or not decoder.eof:
                    raise NagaError("报告过大或压缩数据不完整。")
            data = json.loads(raw)
        except NagaError:
            raise
        except (ValueError, zlib.error, UnicodeDecodeError):
            raise NagaError("NAGA 报告数据无效，请检查链接。") from None
        return render_report(parse_report(data, identity))

    async def close(self):
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        await self.http.aclose()
