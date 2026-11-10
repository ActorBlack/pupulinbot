"""Persistent 1–100 game review manifests and honest, model-separated reports."""

import asyncio
import base64
import html
import json
import logging
import math
import os
import re
import statistics
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from zoneinfo import ZoneInfo

import httpx

from ._batchmortal.metrics import parse_killerducky_bad_move_stats
from .ms_ids import account_to_perspective
from .review import ReviewError, parse_review_link
from .review_metadata import apply_report_result

logger = logging.getLogger(__name__)


async def upload_report(bot, group_id: int, path: Path):
    """Transfer bytes instead of requiring QQ/NapCat to read the Bot filesystem."""

    def read_bounded():
        with path.open("rb") as stream:
            return stream.read(2 * 1024 * 1024 + 1)

    content = await asyncio.to_thread(read_bounded)
    if len(content) > 2 * 1024 * 1024:
        raise ReviewError("报告超过 2 MiB 群文件传输限制，请在本机打开报告")
    return await bot.call_api(
        "upload_group_file",
        group_id=group_id,
        file="base64://" + base64.b64encode(content).decode("ascii"),
        name=f"mortal-{path.parent.name}{path.suffix}",
    )


STATUS_LABELS = {
    "waiting_human": "等待真人验证",
    "server_timeout": "分析服务任务超时",
    "captcha_rejected": "服务器拒绝验证，待重试",
    "uncertain": "等待核对已提交场次",
    "retry_wait": "等待自动重试",
    "names_missing": "等待读取天凤昵称",
    "rate_limited": "服务限流已暂停",
    "error": "浏览器错误已暂停",
    "verification_failed": "自动验证/准备失败，已保存任务",
    "pending": "待复盘",
    "waiting_service": "等待分析服务",
    "ready": "待开始",
    "running": "进行中",
    "paused": "已暂停",
    "done": "完成",
    "partial": "部分完成",
    "failed": "失败",
}


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def report_metrics(data: dict) -> dict:
    """Read the author's native JSON; percentages are reported on a 0–100 scale."""
    review = data.get("review")
    if not isinstance(review, dict) or data.get("engine") != "Mortal":
        raise ReviewError("不是有效的 Mortal 结构化报告")
    rating = _number(review.get("rating"))
    if rating is not None and not 0 <= rating <= 1:
        raise ReviewError("Mortal 原生 rating 超出 0–1 范围")
    total = review.get("total_reviewed")
    matches = review.get("total_matches")
    if type(total) is not int or type(matches) is not int or total < 1 or not 0 <= matches <= total:
        raise ReviewError("Mortal 报告缺少有效决策数与一致数")
    bad = parse_killerducky_bad_move_stats(data)
    bad5, bad10 = int(bad["badMoveCount5"]), int(bad["badMoveCount10"])
    return {
        "rating": rating * 100 if rating is not None else None,
        "model": str(review.get("model_tag") or "未标注模型"),
        "temperature": _number(review.get("temperature")),
        "matches": matches,
        "decisions": total,
        "agreement": matches / total * 100,
        "badmove_decisions": total,
        "badmove_unparsed": int(bad["badMoveUnparsedCount"]),
        "metric_schema": "batchmortal-local-20261107",
        "game_length": data.get("game_length"),
        "review_duration": data.get("review_time"),
        "bad5": bad5,
        "bad10": bad10,
        "bad5_rate": bad5 / total * 100,
        "bad10_rate": bad10 / total * 100,
    }


def summaries(rows: list[dict]) -> list[dict]:
    groups = {}
    for row in rows:
        if row.get("status") != "done":
            continue
        metric = row["metrics"]
        key = (
            metric.get("model", "未标注模型"),
            metric.get("temperature"),
            metric.get("metric_schema", "legacy"),
        )
        groups.setdefault(key, []).append(metric)
    result = []
    for (model, temperature, schema), metrics in groups.items():
        ratings = [m["rating"] for m in metrics if m.get("rating") is not None]
        decisions = sum(m.get("decisions", 0) for m in metrics)
        matches = sum(m.get("matches", 0) for m in metrics)
        bad_denominator = sum(m.get("badmove_decisions", 0) for m in metrics)
        result.append(
            {
                "model": model,
                "temperature": temperature,
                "metric_schema": schema,
                "games": len(metrics),
                "rated_games": len(ratings),
                "mean_rating": statistics.mean(ratings) if ratings else None,
                "median_rating": statistics.median(ratings) if ratings else None,
                "min_rating": min(ratings) if ratings else None,
                "max_rating": max(ratings) if ratings else None,
                "agreement": matches / decisions * 100 if decisions else None,
                "decisions": decisions,
                "bad5_rate": sum(m.get("bad5") or 0 for m in metrics) / bad_denominator * 100
                if bad_denominator
                else None,
                "bad10_rate": sum(m.get("bad10") or 0 for m in metrics) / bad_denominator * 100
                if bad_denominator
                else None,
            }
        )
    return result


def report_url(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "mjai.ekyu.moe"
        or parsed.port
        or parsed.username
        or parsed.password
    ):
        raise ReviewError("仅支持作者 mjai.ekyu.moe 的 HTTPS 报告链接")
    path = parse_qs(parsed.query).get("data", [parsed.path])[0]
    if not re.fullmatch(r"/report/[0-9a-f]{16}\.json", path):
        raise ReviewError("请提供复盘完成后的报告链接")
    return "https://mjai.ekyu.moe" + path


async def fetch_report(url: str) -> dict:
    endpoint = report_url(url)
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        async with client.stream("GET", endpoint) as response:
            response.raise_for_status()
            chunks = bytearray()
            async for part in response.aiter_bytes():
                chunks.extend(part)
                if len(chunks) > 16 * 1024 * 1024:
                    raise ReviewError("报告超过 16 MiB 限制")
    try:
        return json.loads(chunks)
    except ValueError as exc:
        raise ReviewError("报告不是有效 JSON") from exc


class BatchReviews:
    def __init__(
        self,
        directory: Path,
        records,
        reviews,
        online=None,
        notify=None,
        name_resolver=None,
        *,
        superusers=(),
    ):
        self.directory, self.records, self.reviews = directory, records, reviews
        self.online, self.notify = online, notify
        self.name_resolver = name_resolver
        self.superusers = frozenset(str(user) for user in superusers)
        self.lock = asyncio.Lock()
        self.tasks: dict[str, asyncio.Task] = {}

    @property
    def available(self):
        return bool(self.online or self.reviews.client.url)

    def _path(self, job_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{12}", job_id):
            raise ReviewError("批量任务编号无效")
        return self.directory / job_id / "job.json"

    def load(self, job_id: str, group_id: str, owner: str) -> dict:
        try:
            job = json.loads(self._path(job_id).read_text())
        except (OSError, ValueError) as exc:
            raise ReviewError("批量任务不存在") from exc
        if str(owner) not in self.superusers and (
            job["group_id"] != group_id or job["owner"] != owner
        ):
            raise ReviewError("只能操作本人在本群创建的批量任务")
        if job["status"] == "running" and job_id not in self.tasks:
            job["status"] = "paused"
        return job

    def save(self, job: dict) -> None:
        path = self._path(job["id"])
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        path.parent.chmod(0o700)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(job, ensure_ascii=False, indent=2) + "\n")
        temporary.chmod(0o600)
        os.replace(temporary, path)
        (path.parent / "report.html").write_text(render_report(job))

    async def create(self, count: int, group_id: str, owner: str, player, *, games=None) -> dict:
        if type(count) is not int or not 1 <= count <= 100:
            raise ReviewError("批量场数必须是 1–100")
        games = (
            games
            if games is not None
            else await self.records.review_records(player.account_id, count)
        )
        games = games[:count]
        rows = []
        for game in games:
            if game.mode not in {"9", "12", "16"} or not game.ended_at:
                continue
            seat = next(
                (i for i, p in enumerate(game.players) if p["account_id"] == player.account_id),
                None,
            )
            if seat is None:
                continue
            # The official page supports ?url=; encode the target account perspective.
            perspective = account_to_perspective(player.account_id)
            url = f"{game.url}_a{perspective}"
            rows.append(
                {
                    "uuid": game.uuid,
                    "started_at": game.started_at.isoformat(),
                    "ended_at": game.ended_at.isoformat(),
                    "mode": game.mode,
                    "seat": seat,
                    "player_name": game.players[seat].get("nickname"),
                    "player_name_source": "original",
                    "rank": game.seat(player.account_id).get("rank"),
                    "url": game.url,
                    "online_url": "https://mjai.ekyu.moe/?" + urlencode({"url": url}),
                    "source_verification": "在线引擎将按本场次提交并核对报告座位",
                    "status": "pending",
                    "metrics": None,
                    "report_url": None,
                    "error": None,
                }
            )
        if not rows:
            raise ReviewError("没有可分析的四麻半庄牌谱（金南／玉南／王座南）")
        job = {
            "id": uuid.uuid4().hex[:12],
            "group_id": group_id,
            "owner": owner,
            "account_id": player.account_id,
            "nickname": player.nickname,
            "requested": count,
            "created_at": datetime.now(UTC).isoformat(),
            "status": "ready" if self.available else "waiting_service",
            "rows": rows,
        }
        async with self.lock:
            self.save(job)
        return job

    async def create_links(self, links, group_id, owner, *, seat=None, table=False):
        if seat is not None and (type(seat) is not int or seat not in range(4)):
            raise ReviewError("座位必须是0–3")
        if not 1 <= len(links) <= 100 or (table and len(links) != 1):
            raise ReviewError("链接任务支持1–100条；全桌任务只接受1条链接")
        rows, seen = [], set()
        for link in links:
            log = parse_review_link(link, seat)
            identity = (log.source, log.game_id) if table else (log.source, log.game_id, log.seat)
            if identity in seen:
                continue
            seen.add(identity)
            for target in range(4) if table else [log.seat]:
                target_log = parse_review_link(log.url, target)
                canonical = target_log.url
                rows.append(
                    {
                        "uuid": log.game_id,
                        "source": log.source,
                        "started_at": None,
                        "ended_at": None,
                        "mode": "file",
                        "seat": target,
                        "rank": None,
                        "url": canonical,
                        "online_url": target_log.online_url,
                        "status": "pending",
                        "metrics": None,
                        "report_url": None,
                        "error": None,
                        "source_verification": "用户导入链接；时间/房间未知，报告核对座位",
                    }
                )
        job = {
            "id": uuid.uuid4().hex[:12],
            "group_id": group_id,
            "owner": owner,
            "account_id": None,
            "nickname": "同桌四人" if table else "导入牌谱",
            "kind": "table" if table else "links",
            "requested": len(rows),
            "created_at": datetime.now(UTC).isoformat(),
            "rows": rows,
            "status": "ready" if self.available else "waiting_service",
        }
        await self._fill_names(rows)
        async with self.lock:
            self.save(job)
        return job

    async def import_report(self, job_id, group_id, owner, ordinal, url):
        if job_id in self.tasks:
            raise ReviewError("请先暂停任务，再手动导入报告")
        # Snapshot account/seat before I/O, then re-load under the mutation lock.
        job = self.load(job_id, group_id, owner)
        if not 1 <= ordinal <= len(job["rows"]):
            raise ReviewError("场次编号超出任务范围")
        data = await fetch_report(url)
        if data.get("player_id") != job["rows"][ordinal - 1]["seat"]:
            raise ReviewError("报告座位与目标玩家不符，不能导入")
        metrics = report_metrics(data)
        async with self.lock:
            job = self.load(job_id, group_id, owner)
            row = job["rows"][ordinal - 1]
            row.update(
                status="done",
                metrics=metrics,
                report_url=url,
                error=None,
                completed_at=datetime.now(UTC).isoformat(),
            )
            apply_report_result(row, data)
            row["source_verification"] = "用户指定场次；报告座位已核对，报告未包含可校验的对局 UUID"
            raw = self._path(job_id).parent / f"{ordinal}.json"
            raw.write_text(json.dumps(data, ensure_ascii=False))
            raw.chmod(0o600)
            job["status"] = "done" if all(r["status"] == "done" for r in job["rows"]) else "partial"
            self.save(job)
            if self.online and hasattr(self.online, "acknowledge_import"):
                await self.online.acknowledge_import(f"{job_id}:{ordinal - 1}", url, data)
            if (
                self.online
                and getattr(self.online, "active_submitted_key", None) == f"{job_id}:{ordinal - 1}"
            ):
                self.online.active_submitted_key = None
        return job

    def start(self, job_id, group_id, owner, *, automatic=False):
        if job_id in self.tasks:
            raise ReviewError("此批量任务正在运行")
        if self.tasks:
            raise ReviewError("已有批量任务运行，请完成或暂停后再开始")
        job = self.load(job_id, group_id, owner)
        if all(row["status"] == "done" for row in job["rows"]):
            if not missing_tenhou_names(job):
                raise ReviewError("该任务已经全部完成，可直接查看报告")
            if not automatic:
                job["retry_attempts"] = 0
            job["retry_phase"] = "names"
            job.pop("next_retry_at", None)
            self.save(job)
            task = asyncio.create_task(self._recover_names(job_id, group_id, owner))
            self.tasks[job_id] = task
            task.add_done_callback(lambda _: self.tasks.pop(job_id, None))
            return
        if not self.available:
            raise ReviewError("自动批跑需要启用在线 Chrome 引擎或 Mortal 桥接服务")
        if self.online and not self.reviews.client.url:
            for manifest in self.directory.glob("*/job.json"):
                if manifest.parent.name == job_id:
                    continue
                other = json.loads(manifest.read_text())
                if any(r.get("online_submitted") and r["status"] != "done" for r in other["rows"]):
                    raise ReviewError(
                        f"任务 {other['id']} 有已提交但未取回的结果。"
                        f"请发送 mortal resume {other['id']} 恢复；已完成场次会跳过。"
                    )
        if self.online and getattr(self.online, "pending_key", None):
            if not self.online.pending_key.startswith(job_id + ":"):
                raise ReviewError("上一个任务仍有场次等待浏览器结果，请先继续该任务")
        if not automatic:
            job["retry_attempts"] = 0
        job.pop("next_retry_at", None)
        self.save(job)
        task = asyncio.create_task(self._run(job_id, group_id, owner))
        self.tasks[job_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(job_id, None))

    def schedule_retry(self, job):
        """Persist bounded backoff; ambiguous submissions are only read/recovered."""
        if job["status"] not in {"uncertain", "server_timeout", "error", "names_missing"}:
            return
        attempts = job.get("retry_attempts", 0)
        if attempts >= 5:
            job.pop("next_retry_at", None)
            job["message"] = (
                "天凤昵称自动读取已尝试5次；分析结果已保存，"
                f"发送 mortal resume {job['id']} 继续读取。"
                if job.get("retry_phase") == "names"
                else "自动恢复已尝试5次，仍未取得结果；已保存进度，请检查网络后继续。"
            )
            return
        delay = min(30 * 2**attempts, 300)
        job.update(
            status="retry_wait",
            retry_attempts=attempts + 1,
            next_retry_at=(datetime.now(UTC) + timedelta(seconds=delay)).isoformat(),
            message=(
                f"{delay}秒后重试读取天凤昵称（{attempts + 1}/5）；分析结果已保存，不会重新跑谱。"
                if job.get("retry_phase") == "names"
                else f"{delay}秒后自动恢复（{attempts + 1}/5）；已提交场次优先找回结果。"
            ),
        )

    async def retry_due(self, now=None):
        if self.tasks:
            return
        now = now or datetime.now(UTC)
        for path in sorted(self.directory.glob("*/job.json")):
            try:
                job = json.loads(path.read_text())
                if job["status"] != "retry_wait" or not job.get("next_retry_at"):
                    continue
                if datetime.fromisoformat(job["next_retry_at"]) > now:
                    continue
                self.start(job["id"], job["group_id"], job["owner"], automatic=True)
                return
            except ReviewError:
                # Another unresolved job may own the browser. Keep this job queued.
                continue
            except (OSError, ValueError, KeyError, TypeError):
                logger.warning("无法读取自动恢复任务：%s", path.parent.name)

    async def _analyze_row(self, job_id, group_id, owner, index, row):
        if self.online and not self.reviews.client.url:

            async def on_submitted(url=None, index=index):
                async with self.lock:
                    current = self.load(job_id, group_id, owner)
                    current["rows"][index]["online_submitted"] = True
                    if url:
                        current["rows"][index]["online_report_url"] = url
                    self.save(current)

            data, details_url = await self.online.submit(f"{job_id}:{index}", row, on_submitted)
            metric = report_metrics(data)
            if data.get("batchmortal_artifacts"):
                row["backend_artifacts"] = data["batchmortal_artifacts"]
                names = data["batchmortal_artifacts"].get("playerNames")
                if isinstance(names, list) and len(names) > row["seat"]:
                    if row.get("player_name_source") != "original" and not anonymous_names(names):
                        row["player_name"] = str(names[row["seat"]])
            raw = self._path(job_id).parent / f"{index + 1}.json"
            raw.write_text(json.dumps(data, ensure_ascii=False))
            raw.chmod(0o600)
        else:
            result = await self.reviews.submit(row["url"], row["seat"])
            data = result.raw
            details_url = result.details_url
            if "review" in result.raw:
                metric = report_metrics(result.raw)
            else:
                metric = {
                    "rating": result.rating,
                    "model": result.raw.get("model_tag", "未标注模型"),
                }
        await self._fill_names([row])
        apply_report_result(row, data)
        async with self.lock:
            job = self.load(job_id, group_id, owner)
            job["rows"][index].update(
                status="done",
                metrics=metric,
                report_url=details_url,
                error=None,
                backend_artifacts=row.get("backend_artifacts", {}),
                player_name=row.get("player_name"),
                player_name_source=row.get("player_name_source"),
                rank=row.get("rank"),
                rank_source=row.get("rank_source"),
                completed_at=datetime.now(UTC).isoformat(),
                review_cache_hit=bool(row.get("review_cache_hit")),
            )
            self.save(job)

    async def _fill_names(self, rows):
        if not self.name_resolver:
            return

        async def fill(row):
            original_name = row.get("player_name_source") == "original" and row.get("player_name")
            if original_name and row.get("rank") is not None:
                return
            try:
                async with asyncio.timeout(75):
                    if not original_name:
                        names = await self.name_resolver(row)
                        if len(names) > row["seat"] and names[row["seat"]]:
                            row.update(
                                player_name=names[row["seat"]], player_name_source="original"
                            )
                    result_loader = getattr(type(self.name_resolver), "seat_result", None)
                    if row.get("rank") is None and result_loader:
                        result = await self.name_resolver.seat_result(row)
                        rank = result.get("rank")
                        if type(rank) is int and 1 <= rank <= 4:
                            row.update(rank=rank, rank_source="original")
            except Exception as exc:
                logger.warning(
                    "原始牌谱昵称读取失败：source=%s game=%s error=%s: %s",
                    row.get("source", "majsoul"),
                    row.get("uuid"),
                    type(exc).__name__,
                    exc,
                )

        await asyncio.gather(*(fill(row) for row in rows))

    async def refresh_names(self, job_id, group_id, owner, *, strict=True):
        snapshot = self.load(job_id, group_id, owner)
        await self._fill_names(snapshot["rows"])
        async with self.lock:
            current = self.load(job_id, group_id, owner)
            for row, updated in zip(current["rows"], snapshot["rows"], strict=True):
                if updated.get("player_name_source") == "original":
                    row.update(player_name=updated["player_name"], player_name_source="original")
                if updated.get("rank_source") == "original":
                    row.update(rank=updated["rank"], rank_source="original")
            if all(row["status"] == "done" for row in current["rows"]):
                if current["status"] == "done" and missing_tenhou_names(current):
                    current.update(status="names_missing", retry_phase="names", retry_attempts=0)
                    self.schedule_retry(current)
                elif (
                    current["status"] in {"retry_wait", "names_missing"}
                    and current.get("retry_phase") == "names"
                    and not missing_tenhou_names(current)
                ):
                    current["status"] = "done"
                    for key in ("next_retry_at", "retry_phase", "message"):
                        current.pop(key, None)
            self.save(current)
        if strict:
            require_tenhou_names(current)
        return current

    async def _recover_names(self, job_id, group_id, owner):
        """Retry metadata independently; completed analysis rows are never resubmitted."""
        try:
            job = await self.refresh_names(job_id, group_id, owner, strict=False)
            job.pop("next_retry_at", None)
            if missing_tenhou_names(job):
                job["status"] = "names_missing"
                self.schedule_retry(job)
            else:
                job["status"] = "done"
                job.pop("retry_phase", None)
                job.pop("message", None)
            self.save(job)
            await self._notify(job)
        except asyncio.CancelledError:
            job = self.load(job_id, group_id, owner)
            job["status"] = "paused"
            job.pop("next_retry_at", None)
            self.save(job)
            raise

    async def _run(self, job_id, group_id, owner):
        job = self.load(job_id, group_id, owner)
        job["status"] = "running"
        job.pop("message", None)
        self.save(job)
        if self.online and hasattr(self.online, "begin"):
            self.online.begin()
        stopped = asyncio.Event()
        consumers = []

        def halt():
            stopped.set()
            if self.online and hasattr(self.online, "halt"):
                self.online.halt()

        async def analyze(index, row, preferred_slot=None):
            try:
                if self.online and hasattr(self.online, "concurrency"):
                    async with self.lock:
                        current = self.load(job_id, group_id, owner)
                        slot = row.get(
                            "online_slot",
                            index % self.online.concurrency
                            if preferred_slot is None
                            else preferred_slot,
                        )
                        current["rows"][index]["online_slot"] = row["online_slot"] = slot
                        self.save(current)
                await self._analyze_row(job_id, group_id, owner, index, row)
            except Exception as exc:
                first = not stopped.is_set()
                halt()
                state = getattr(exc, "status", "error" if self.online else "paused")
                message = str(exc).replace("<任务编号>", job_id)
                if not isinstance(exc, ReviewError):
                    logger.exception("批量复盘异常，已暂停：job=%s index=%s", job_id, index)
                    message = "任务执行异常，请检查本机日志；已完成结果保留"
                async with self.lock:
                    current = self.load(job_id, group_id, owner)
                    current["rows"][index].update(
                        status="waiting_human" if state == "waiting_human" else "failed",
                        error=message,
                    )
                    if state in {"captcha_rejected", "server_timeout"}:
                        current["rows"][index]["online_submitted"] = False
                    if first:
                        current.update(status=state, message=message)
                    self.save(current)

        try:
            # Recover outstanding submissions before allowing any fresh task to start.
            pending_keys = getattr(self.online, "pending_keys", set())
            pending_key = getattr(self.online, "pending_key", None)
            if pending_key:
                pending_keys = set(pending_keys) | {pending_key}
            for index, row in enumerate(job["rows"]):
                if row["status"] != "done" and (
                    row.get("online_submitted") or f"{job_id}:{index}" in pending_keys
                ):
                    await analyze(index, row)
                    # A failed recovery must not hide another already submitted result.
                    # Fresh dispatch below remains stopped after any recovery failure.
            current = self.load(job_id, group_id, owner)
            pending = iter((i, r) for i, r in enumerate(current["rows"]) if r["status"] != "done")

            async def consume(slot):
                while not stopped.is_set():
                    item = next(pending, None)
                    if item is None:
                        return
                    await analyze(*item, preferred_slot=slot)

            count = (
                getattr(self.online, "concurrency", 1)
                if self.online and not self.reviews.client.url
                else 1
            )
            consumers = [asyncio.create_task(consume(slot)) for slot in range(count)]
            await asyncio.gather(*consumers)
        except asyncio.CancelledError:
            halt()
            for task in consumers:
                task.cancel()
            await asyncio.gather(*consumers, return_exceptions=True)
            current = self.load(job_id, group_id, owner)
            current["status"] = "paused"
            self.save(current)
            raise
        job = self.load(job_id, group_id, owner)
        if not stopped.is_set():
            job["status"] = "done"
            job.pop("message", None)
            if missing_tenhou_names(job):
                job.update(status="names_missing", retry_phase="names", retry_attempts=0)
                self.schedule_retry(job)
            self.save(job)
        else:
            self.schedule_retry(job)
            self.save(job)
        await self._notify(job)

    async def _notify(self, job):
        if self.notify:
            try:
                await self.notify(job)
            except Exception:
                logger.exception("批跑状态通知失败：job=%s", job["id"])

    async def close(self):
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)
        if self.online:
            await self.online.close()

    async def pause(self, job_id, group_id, owner):
        job = self.load(job_id, group_id, owner)
        task = self.tasks.get(job_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        else:
            job["status"] = "paused"
            job.pop("next_retry_at", None)
            self.save(job)


def anonymous_names(names):
    return isinstance(names, list) and names in (
        ["Aさん", "Bさん", "Cさん", "Dさん"],
        ["A", "B", "C", "D"],
    )


def missing_tenhou_names(job):
    return any(
        row.get("source") == "tenhou"
        and not (
            row.get("player_name_source") == "original"
            and isinstance(row.get("player_name"), str)
            and row["player_name"].strip()
        )
        for row in job["rows"]
    )


def require_tenhou_names(job):
    if missing_tenhou_names(job):
        raise ReviewError(
            "天凤座位昵称尚未读取完整，请稍后重试；已完成的分析结果保留，无需重新跑谱。"
        )


def seat_nickname(row):
    """Use the analyzed seat's game nickname, including previously saved artifacts."""
    name = row.get("player_name")
    if row.get("source") == "tenhou" and (
        row.get("player_name_source") != "original" or not isinstance(name, str) or not name.strip()
    ):
        return "昵称读取中"
    names = (row.get("backend_artifacts") or {}).get("playerNames")
    if isinstance(name, str) and name.strip():
        if row.get("player_name_source") == "original" or not anonymous_names(names):
            return name.strip()
    if anonymous_names(names):
        return "—"
    seat = row.get("seat")
    if isinstance(names, list) and type(seat) is int and 0 <= seat < len(names):
        name = names[seat]
        if isinstance(name, str) and name.strip():
            return name.strip()
    return "—"


def render_report(job: dict) -> str:
    def esc(value):
        return html.escape(str(value), quote=True)

    def fmt(value):
        return "—" if value is None else f"{value:.2f}"

    body = []
    for index, row in enumerate(job["rows"], 1):
        m = row.get("metrics") or {}
        values = [
            index,
            (
                datetime.fromisoformat(row["started_at"])
                .astimezone(ZoneInfo("Asia/Shanghai"))
                .strftime("%Y-%m-%d %H:%M")
                if row.get("started_at")
                else "未知（导入链接）"
            ),
            {"9": "金南", "12": "玉南", "16": "王座南"}.get(row["mode"], row["mode"]),
            (
                "东南西北"[row["seat"]] + "起"
                if job.get("kind") == "table"
                else row.get("rank", "—")
            ),
            seat_nickname(row)
            if job.get("kind") == "table"
            else STATUS_LABELS.get(row["status"], row["status"]),
            m.get("model", "—"),
            fmt(m.get("rating")),
            fmt(m.get("agreement")),
            fmt(m.get("bad5_rate")),
            fmt(m.get("bad10_rate")),
            m.get("decisions", "—"),
        ]
        cells = "".join(f"<td>{esc(v)}</td>" for v in values)
        report = row.get("report_url")
        # Only trusted author report URLs are clickable; generic service URLs stay as text.
        link = f'<a href="{esc(row["online_url"])}" target="_blank" rel="noopener">在线复盘</a>'
        if report and report.startswith("https://mjai.ekyu.moe/"):
            link += f' · <a href="{esc(report)}" target="_blank" rel="noopener">报告</a>'
        body.append(f"<tr>{cells}<td>{link}<br>{esc(row.get('error') or '')}</td></tr>")
    aggregates = []
    for metric in [] if job.get("kind") == "table" else summaries(job["rows"]):
        aggregates.append(
            f"<li>{esc(metric['model'])} / T={esc(metric['temperature'])} "
            f"/ {esc(metric['metric_schema'])}："
            f"成功 {metric['games']} 场，平均 Rating {fmt(metric['mean_rating'])}，"
            f"中位数 {fmt(metric['median_rating'])}，"
            f"按决策数加权一致率 {fmt(metric['agreement'])}%，"
            f"5% / 10% 恶手率 {fmt(metric['bad5_rate'])}% / {fmt(metric['bad10_rate'])}%</li>"
        )
    done = sum(r["status"] == "done" for r in job["rows"])
    headers = [
        "编号",
        "开始时间（北京时间）",
        "模式",
        "起始座位" if job.get("kind") == "table" else "顺位",
        "游戏昵称" if job.get("kind") == "table" else "状态",
        "模型",
        "Rating",
        "一致率 %",
        "5% 恶手率",
        "10% 恶手率",
        "决策数",
        "操作",
    ]
    unit = "个座位" if job.get("kind") == "table" else "场"
    aggregate_note = (
        "同桌四人独立展示，不合并为同一玩家均值"
        if job.get("kind") == "table"
        else "尚无成功分析结果"
    )
    header_cells = "".join("<th>" + value + "</th>" for value in headers)
    return f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(job["nickname"])} · Mortal 批量报告</title>
<style>body{{font:15px system-ui;margin:32px;color:#172033;background:#f5f7fb}}
table{{border-collapse:collapse;background:white;white-space:nowrap}}
td,th{{padding:10px;border:1px solid #ddd}}
th{{background:#e5edf9}}.scroll{{overflow:auto}}a{{color:#1766ba}}</style>
<h1>{esc(job["nickname"])} · Mortal 批量报告</h1>
<p>任务 {esc(job["id"])} · 请求 {job["requested"]} {unit}
· 找到 {len(job["rows"])} {unit} · 成功 {done} {unit}
· 状态 {esc(STATUS_LABELS.get(job["status"], job["status"]))}</p>
<ul>{"".join(aggregates) or f"<li>{aggregate_note}</li>"}</ul>
<div class="scroll"><table><thead><tr>{header_cells}</tr></thead>
<tbody>{"".join(body)}</tbody></table></div></html>"""
