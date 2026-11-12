"""Adapter around the user-provided batchmortal browser backend."""

import hashlib
import json
import logging
import os
import re
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .online_worker import TURNSTILE_TIMEOUT, emit, progress_report_url, trusted_result
from .review_metadata import report_game_result


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix(".tmp")
    with temp.open("w") as stream:
        os.chmod(temp, 0o600)
        json.dump(data, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


def validate_request(request):
    from .review import ReviewError, parse_review_link

    parsed = urlsplit(request["online_url"])
    if parsed.scheme != "https" or parsed.netloc != "mjai.ekyu.moe" or parsed.path != "/":
        raise ValueError("在线入口地址无效")
    query = parse_qs(parsed.query)
    key = "rcid" if "rcid" in query else "url"
    if set(query) != {key} or len(query.get(key, [])) != 1:
        raise ValueError("牌谱输入参数无效")
    link = query[key][0]
    if type(request["seat"]) is not int or request["seat"] not in range(4):
        raise ValueError("座位必须是0–3")
    try:
        log = parse_review_link(link, request["seat"])
        if key == "rcid":
            if log.source != "riichi_city":
                raise ValueError("一番街牌谱 ID 无效")
        elif log.source == "riichi_city" or urlsplit(link).scheme != "https":
            raise ValueError("仅接受正式HTTPS牌谱链接")
        return log.url
    except ReviewError as exc:
        raise ValueError(str(exc)) from exc


def fill_riichi_city_form(browser, identifier):
    from .review import parse_review_link

    log = parse_review_link(identifier)
    if log.source != "riichi_city":
        raise ValueError("一番街牌谱 ID 无效")
    success = browser.execute_script(
        """
        const radio = document.querySelector(
          'input[name="input-method"][value="riichi-city-log-id"]');
        const input = document.querySelector('input[name="riichi-city-log-id"]');
        const player = document.querySelector('select[name="player-id"]');
        const url = document.querySelector('input[name="log-url"]');
        if (!radio || !input || !player || !url) return false;
        url.value = '';
        radio.click();
        input.value = arguments[0];
        input.dispatchEvent(new Event('input', {bubbles:true}));
        input.dispatchEvent(new Event('change', {bubbles:true}));
        player.value = String(arguments[1]);
        player.dispatchEvent(new Event('change', {bubbles:true}));
        return radio.checked && input.value === arguments[0]
          && player.value === String(arguments[1]);
        """,
        log.url,
        log.seat,
    )
    if not success:
        raise RuntimeError("无法填写一番街牌谱表单")


def browser_options(profile, options, platform=None):
    platform = platform or sys.platform
    result = {
        "uc": True,
        "headless": bool(options.get("headless", True)),
        "user_data_dir": str(profile),
        "proxy": options.get("proxy"),
        "page_load_strategy": "none",
        # Explicit form, token and report waits remain; unrelated resources may never finish.
        "skip_js_waits": True,
    }
    if result["headless"] and platform.startswith("linux"):
        result.update(headless=False, xvfb=True)
    if options.get("unique_debug_port"):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            result["chromium_arg"] = f"--remote-debugging-port={sock.getsockname()[1]}"
    return result


def deferred_form_submit(script):
    """Let WebDriver return before the ordinary form navigation starts."""
    if "form.requestSubmit(submit)" in script and "return 'submitted'" in script:
        return script.replace(
            "form.requestSubmit(submit);", "setTimeout(() => form.requestSubmit(submit), 0);"
        ).replace("submit.click();", "setTimeout(() => submit.click(), 0);")
    return script


class SubmissionStopped(RuntimeError):
    pass


class VerificationTimeout(RuntimeError):
    pass


class VerificationRejected(RuntimeError):
    pass


def wait_for_submission_departure(read_state, log_prefix, timeout):
    """An accepted progress page is distinct from a still-unsubmitted form."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = read_state()
        url = state.get("url", "")
        text = state.get("page_text", "")
        if any(
            marker in text
            for marker in (
                "invalid captcha response",
                "timeout-or-duplicate",
                "too many requests",
                "rate limit",
            )
        ) or re.fullmatch(r"task timeout, been running for [0-9.]+s", text.strip()):
            raise RuntimeError(f"{log_prefix} 作者服务返回错误：{text[:160]}")
        if trusted_result(url) or progress_report_url(url):
            return
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))
    raise RuntimeError(f"{log_prefix} 尚未确认提交已受理")


class BrowserWorker:
    def __init__(self, profile, model, timeout, options=None):
        from seleniumbase import SB

        from ._batchmortal.browser import BrowserAutomator, ReviewSubmissionCoordinator

        self.profile = Path(profile).resolve()
        self.profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.profile.chmod(0o700)
        self.journal_path = self.profile / "submission-journal.json"
        self.journal = (
            json.loads(self.journal_path.read_text()) if self.journal_path.exists() else {}
        )
        log_path = self.profile / "worker.log"
        handler = logging.FileHandler(log_path)
        log_path.chmod(0o600)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.INFO)
        self.options = options or {}
        self.model, self.timeout = model, timeout
        self.report_native = None
        self.report_future = None
        self.report_reader = ThreadPoolExecutor(max_workers=1)
        self.key = None
        self.submitted = False
        owner = self

        class Adapter(BrowserAutomator):
            def _populate_form(self, sb, paipu_url, model_tag):
                from .review import parse_review_link

                log = parse_review_link(paipu_url, owner.seat)
                if log.source == "riichi_city":
                    super()._populate_form(sb, "", model_tag)
                    fill_riichi_city_form(sb, log.url)
                else:
                    super()._populate_form(sb, paipu_url, model_tag)

            def _extract_killerducky_data(self, sb, include_entries=False):
                if owner.report_native is not None:
                    return owner.report_native
                if owner.report_future and owner.report_future.done():
                    try:
                        owner.report_native = owner.report_future.result()
                        return owner.report_native
                    except Exception:
                        pass
                data = super()._extract_killerducky_data(sb, include_entries=include_entries)
                if isinstance(data, dict) and include_entries:
                    try:
                        events = sb.execute_script(
                            "return window.MM?.GS?.fullData?.mjai_log || null;"
                        )
                        result = report_game_result({"mjai_log": events})
                        if result:
                            data["game_result"] = result
                    except Exception:
                        logging.debug("Could not extract optional final game result", exc_info=True)
                return data

            def _read_review_state(self, sb):
                state = super()._read_review_state(sb)
                if owner.submitted:
                    owner._remember_remote_url(state.get("url", ""))
                return state

            def _prepare_review_form(self, sb):
                emit(status="verification_wait")
                return super()._prepare_review_form(sb)

            def _poke_captcha(self, sb):
                lock_path = owner.options.get("gui_lock_path")
                if not lock_path:
                    return super()._poke_captcha(sb)
                from filelock import FileLock

                with FileLock(lock_path, timeout=30):
                    return super()._poke_captcha(sb)

            def _wait_for_turnstile_token(self, sb, log_prefix, timeout):
                try:
                    super()._wait_for_turnstile_token(sb, log_prefix, timeout)
                except RuntimeError as exc:
                    if str(exc) == f"{log_prefix} Timed out waiting for Turnstile token":
                        raise VerificationTimeout(str(exc)) from exc
                    if str(exc).startswith(
                        (
                            f"{log_prefix} Turnstile token was rejected",
                            f"{log_prefix} Turnstile challenge failed",
                        )
                    ):
                        raise VerificationRejected(str(exc)) from exc
                    raise

            def _submit_review(self, sb, log_prefix):
                state = self._read_review_state(sb)
                if not state.get("token_length"):
                    raise VerificationRejected("Turnstile token missing before submission")
                if owner.options.get("controlled_gate"):
                    emit(status="ready_to_submit")
                    permit = json.loads(sys.stdin.readline())
                    if permit != {"action": "permit", "key": owner.key}:
                        raise SubmissionStopped("批跑暂停，未取得提交许可")
                    # A queued permit never substitutes for an actual current token.
                    if not self._read_review_state(sb).get("token_length"):
                        raise VerificationRejected(
                            "Token expired while waiting for submission slot"
                        )
                owner._record_submission()
                execute = sb.execute_script

                def deferred(script, *args, **kwargs):
                    return execute(deferred_form_submit(script), *args, **kwargs)

                sb.execute_script = deferred
                try:
                    return super()._submit_review(sb, log_prefix)
                finally:
                    sb.execute_script = execute

            def _wait_for_submission_departure_or_error(self, sb, log_prefix, timeout):
                wait_for_submission_departure(
                    lambda: self._read_review_state(sb), log_prefix, timeout
                )

            def _wait_for_result_or_error(self, sb, log_prefix, timeout):
                from .native_report import read_native_report

                deadline = time.monotonic() + owner.timeout
                report_seen_at = None
                while time.monotonic() < deadline:
                    data = self._extract_killerducky_data(sb)
                    if data and data.get("review"):
                        return
                    state = self._read_review_state(sb)
                    url = state.get("url", "")
                    if trusted_result(url):
                        if report_seen_at is None:
                            report_seen_at = time.monotonic()
                        if owner.report_future is None and time.monotonic() - report_seen_at >= 2:
                            owner.report_future = owner.report_reader.submit(
                                read_native_report, url, owner.seat, owner.model
                            )
                    text = state.get("page_text", "")
                    if any(
                        marker in text
                        for marker in (
                            "invalid captcha response",
                            "timeout-or-duplicate",
                            "too many requests",
                            "rate limit",
                        )
                    ) or re.fullmatch(r"task timeout, been running for [0-9.]+s", text.strip()):
                        raise RuntimeError("作者服务返回错误：" + text[:160])
                    time.sleep(0.5)
                raise RuntimeError(f"{log_prefix} Timed out waiting for review results")

        self.automator = Adapter(
            headless=self.options.get("headless", True),
            proxy=self.options.get("proxy"),
            review_language="zh-CN",
            review_ui="killerducky",
            submission_coordinator=ReviewSubmissionCoordinator(
                base_interval=0
                if self.options.get("controlled_gate")
                else self.options.get("cooldown", 20),
                max_interval=max(60, self.options.get("cooldown", 20)),
                cooldown_seconds=30,
            ),
        )
        self.context = SB(**browser_options(self.profile, self.options))
        self.sb = self.context.__enter__()
        client_config = getattr(self.sb.driver.command_executor, "_client_config", None)
        if client_config:
            client_config.timeout = 30

    def _remember_remote_url(self, url):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "mjai.ekyu.moe":
            return
        field = None
        if trusted_result(url):
            field = "remote_report_url"
        elif progress_report_url(url):
            field = "remote_progress_url"
        if field and self.journal.get(field) != url:
            self.journal[field] = url
            atomic_json(self.journal_path, self.journal)

    def _record_submission(self):
        self.submitted = True
        self.journal = {"key": self.key, "state": "submitted"}
        atomic_json(self.journal_path, self.journal)
        emit(status="submitted")

    def _done(self, url, artifacts=None):
        from .batch_review import report_metrics

        if not trusted_result(url):
            raise RuntimeError("结果不是有效的作者报告链接")
        native = self.automator._extract_killerducky_data(self.sb, include_entries=True)
        if not isinstance(native, dict) or native.get("player_id") != self.seat:
            raise RuntimeError("报告原生数据缺失或座位不符")
        if report_metrics(native)["model"] != self.model:
            raise RuntimeError("报告模型与请求不符")
        # The viewer may omit MJAI after loading. Read its existing report once;
        # this never submits another review, and shares any reader already running.
        reader = getattr(self, "report_reader", None)
        if (
            not native.get("game_result")
            and reader is not None
            and getattr(self, "need_game_result", True)
        ):
            try:
                from .native_report import read_native_report

                if self.report_future is None:
                    self.report_future = reader.submit(
                        read_native_report, url, self.seat, self.model
                    )
                recovered = self.report_future.result(timeout=75)
                if recovered.get("game_result") and report_metrics(recovered) == report_metrics(
                    native
                ):
                    native["game_result"] = recovered["game_result"]
            except Exception:
                logging.warning("Could not recover final game result from existing report")
        artifact = (
            self.profile / "artifacts" / (hashlib.sha256(self.key.encode()).hexdigest() + ".json")
        )
        if artifacts:
            native["batchmortal_artifacts"] = artifacts
        atomic_json(artifact, native)
        self.journal = {"key": self.key, "state": "done", "url": url, "report_file": str(artifact)}
        atomic_json(self.journal_path, self.journal)
        emit(status="done", url=url, report_file=str(artifact))

    def run(self, request):
        link = validate_request(request)
        if self.journal_path.exists():
            self.journal = json.loads(self.journal_path.read_text())
        key = request["key"]
        self.seat = request["seat"]
        self.need_game_result = request.get("need_game_result", True)
        if self.journal.get("state") == "submitted" and self.journal.get("key") != key:
            emit(status="uncertain", message="前一场已提交但未核对，请先继续原任务或导入报告")
            return
        if self.journal.get("key") == key and self.journal.get("state") == "done":
            emit(status="done", url=self.journal["url"], report_file=self.journal["report_file"])
            return
        previously_submitted = request.get("submitted") or (
            self.journal.get("key") == key and self.journal.get("state") == "submitted"
        )
        if previously_submitted:
            if self.key == key:
                try:
                    self.automator._wait_for_result_or_error(self.sb, key, self.timeout)
                    self._done(self.sb.get_current_url())
                except Exception:
                    emit(status="uncertain", message="已提交场次尚未取得报告；不会自动重复提交")
            else:
                emit(status="uncertain", message="已提交场次的浏览器会话已中断，请找回报告并导入")
            return
        self.key, self.submitted = key, False
        self.report_native = self.report_future = None
        task = {
            "uuid": hashlib.sha256(key.encode()).hexdigest()[:24],
            "paipu_url": link,
            "player_id": self.seat,
            "mode_dir": str(self.profile / "artifacts"),
            "model_tag": self.model,
            "save_screenshot": self.options.get("save_screenshot", False),
            "save_local_paipu": self.options.get("save_html", True),
            "analyze_bad_move_rate": True,
            "turnstile_timeout": TURNSTILE_TIMEOUT,
            "log_prefix": key,
        }
        retries = self.options.get("retries", 2)
        for attempt in range(retries + 1):
            try:
                result = self.automator.analyze_single(self.sb, task)
                self._done(
                    result["resultUrl"],
                    {k: result.get(k) for k in ("localPaipuPath", "screenshotPath", "playerNames")},
                )
                return
            except SubmissionStopped:
                emit(status="paused", message="批跑暂停，尚未提交本场")
                return
            except Exception as exc:
                logging.exception(
                    "Browser attempt failed: key=%s submitted=%s", key, self.submitted
                )
                try:
                    self.sb.save_screenshot(str(self.profile / "last-page.png"))
                except Exception:
                    pass
                if self.submitted:
                    try:
                        state = self.automator._read_review_state(self.sb)
                    except Exception:
                        state = {}
                    # Only a visible explicit server rejection permits another submission.
                    page_text = state.get("page_text", "").strip()
                    if re.fullmatch(r"task timeout, been running for [0-9.]+s", page_text):
                        self.journal = {"key": key, "state": "rejected", "reason": page_text}
                        atomic_json(self.journal_path, self.journal)
                        self.submitted = False
                        emit(
                            status="server_timeout",
                            message="作者服务明确返回任务超时，本场没有报告，可稍后重试",
                        )
                        return
                    if page_text == "invalid captcha response":
                        self.journal = {
                            "key": key,
                            "state": "rejected",
                            "reason": "invalid captcha response",
                        }
                        atomic_json(self.journal_path, self.journal)
                        self.submitted = False
                        emit(
                            status="captcha_rejected",
                            message="服务器明确拒绝验证，本场未被接受，可稍后重试",
                        )
                        return
                    try:
                        current = self.sb.get_current_url()
                        if trusted_result(current):
                            self._done(current)
                            return
                    except Exception:
                        pass
                    emit(
                        status="uncertain", message="提交后未取得可验证报告；保留会话，不会重复提交"
                    )
                    return
                if isinstance(exc, VerificationTimeout):
                    emit(
                        status="verification_timeout",
                        message=f"Mortal 网页验证等待超过 {TURNSTILE_TIMEOUT} 秒，本场尚未提交。",
                    )
                    return
                if isinstance(exc, VerificationRejected):
                    emit(
                        status="verification_rejected",
                        message="Mortal 网页明确拒绝验证，本场尚未提交。",
                    )
                    return
                text = str(exc).lower()
                if any(x in text for x in ("rate limit", "too many requests", "access denied")):
                    emit(status="rate_limited", message="在线服务限流或拒绝访问，任务已暂停")
                    return
                if attempt < retries:
                    emit(
                        status="preparing_retry",
                        message=f"提交前失败，自动重试 {attempt + 1}/{retries}",
                    )
                    continue
                emit(
                    status="verification_failed",
                    message="按本地batchmortal自动验证/准备仍失败，任务已保存；请检查浏览器或稍后继续",
                )

    def close(self):
        self.report_reader.shutdown(wait=False, cancel_futures=True)
        self.context.__exit__(None, None, None)
