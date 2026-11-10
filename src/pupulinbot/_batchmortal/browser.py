import logging
import os
import queue
import threading
import time
from collections import deque
from contextlib import ExitStack
from copy import deepcopy

from seleniumbase import SB

# SeleniumBase GUI/display and driver installation state are process-global.
# Real parallelism uses separate processes; legacy thread callers are serialized.
BROWSER_SESSION_LOCK = threading.RLock()

REVIEW_BASE_URL = "https://mjai.ekyu.moe"
DEFAULT_REVIEW_LANGUAGE = "zh-CN"
DEFAULT_REVIEW_UI = "classic"

# Mirrors the reviewer form field `select[name="lang"]`.
# Supported values on mjai.ekyu.moe are:
#   zh-CN -> Simplified Chinese, page /zh-cn.html
#   en    -> English, page /
#   ja    -> Japanese, page /ja.html
#   ko    -> Korean, page /ko.html
REVIEW_LANGUAGE_URL_PATHS = {
    "zh-CN": "/zh-cn.html",
    "en": "/",
    "ja": "/ja.html",
    "ko": "/ko.html",
}
REVIEW_LANGUAGE_ALIASES = {
    "zh": "zh-CN",
    "zh-cn": "zh-CN",
    "zh_cn": "zh-CN",
    "cn": "zh-CN",
    "en-us": "en",
    "english": "en",
    "jp": "ja",
    "ja-jp": "ja",
    "japanese": "ja",
    "kr": "ko",
    "ko-kr": "ko",
    "korean": "ko",
}
INPUT_SELECTOR = 'input[name="log-url"]'
SUBMIT_SELECTOR = 'button[name="submitBtn"]'
FORM_SELECTOR = 'form[name="reviewForm"]'
TURNSTILE_RESPONSE_SELECTOR = 'input[name="cf-turnstile-response"]'
RESULT_SELECTOR = "details > dl"
REPORT_URL_FRAGMENT = "/report/"
BAD_MOVE_STRICT_LIMIT = 5
BAD_MOVE_LOOSE_LIMIT = 10

REVIEW_UI_ALIASES = {
    "classic": "classic",
    "killerducky": "killerducky",
    "killer-ducky": "killerducky",
    "kd": "killerducky",
}


def normalize_review_ui(review_ui):
    if review_ui is None:
        return DEFAULT_REVIEW_UI

    key = str(review_ui).strip().lower().replace("_", "-")
    normalized = REVIEW_UI_ALIASES.get(key)
    if normalized:
        return normalized

    raise ValueError(
        f"Unsupported review UI '{review_ui}'. Supported values: classic, killerducky"
    )


def parse_killerducky_metadata(data):
    """Convert KillerDucky's report JSON into the metadata shape used by results.py."""
    data = data if isinstance(data, dict) else {}
    review = data.get("review")
    review = review if isinstance(review, dict) else {}

    total_matches = review.get("total_matches")
    total_reviewed = review.get("total_reviewed")
    matches_total = ""
    if (
        isinstance(total_matches, (int, float))
        and isinstance(total_reviewed, (int, float))
        and total_reviewed > 0
    ):
        rate = 100 * total_matches / total_reviewed
        matches_total = f"{int(total_matches)}/{int(total_reviewed)} = {rate:.3f}%"

    rating = review.get("rating")
    formatted_rating = ""
    if isinstance(rating, (int, float)):
        formatted_rating = f"{rating * 100:.3f}"

    def text(value):
        return "" if value is None else str(value)

    return {
        "engine": text(data.get("engine")),
        "model tag": text(review.get("model_tag")),
        "rating": formatted_rating,
        "matches/total": matches_total,
        "temperature": text(review.get("temperature")),
        "game length": text(data.get("game_length")),
        "player id": text(data.get("player_id")),
        "review duration": text(data.get("review_time")),
    }


def parse_killerducky_bad_move_stats(
    data,
    strict_limit=BAD_MOVE_STRICT_LIMIT,
    loose_limit=BAD_MOVE_LOOSE_LIMIT,
):
    """Calculate bad-move rates from KillerDucky's structured Mortal decisions."""
    data = data if isinstance(data, dict) else {}
    review = data.get("review")
    review = review if isinstance(review, dict) else {}
    total_reviewed = review.get("total_reviewed")
    denominator = total_reviewed if isinstance(total_reviewed, int) and total_reviewed >= 0 else None

    strict_count = 0
    loose_count = 0
    mismatch_count = 0
    unparsed_count = 0

    kyokus = review.get("kyokus")
    for kyoku in kyokus if isinstance(kyokus, list) else []:
        if not isinstance(kyoku, dict):
            continue
        entries = kyoku.get("entries")
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict) or entry.get("is_equal") is not False:
                continue

            mismatch_count += 1
            actual_index = entry.get("actual_index")
            details = entry.get("details")
            if (
                not isinstance(actual_index, int)
                or not isinstance(details, list)
                or actual_index < 0
                or actual_index >= len(details)
                or not isinstance(details[actual_index], dict)
            ):
                unparsed_count += 1
                continue

            probability = details[actual_index].get("prob")
            if not isinstance(probability, (int, float)):
                unparsed_count += 1
                continue

            probability_percent = probability * 100
            if probability_percent <= strict_limit:
                strict_count += 1
            if probability_percent <= loose_limit:
                loose_count += 1

    def format_rate(count):
        if denominator is None or denominator <= 0:
            return ""
        return f"{100 * count / denominator:.3f}%"

    return {
        "badMoveRate5": format_rate(strict_count),
        "badMoveCount5": str(strict_count),
        "badMoveRate10": format_rate(loose_count),
        "badMoveCount10": str(loose_count),
        "badMoveDenominator": "" if denominator is None else str(denominator),
        "badMoveOrderLossCount": str(mismatch_count),
        "badMoveUnparsedCount": str(unparsed_count),
    }


def normalize_review_language(language):
    if language is None:
        return DEFAULT_REVIEW_LANGUAGE

    text = str(language).strip()
    if not text:
        return DEFAULT_REVIEW_LANGUAGE
    if text in REVIEW_LANGUAGE_URL_PATHS:
        return text

    normalized = text.lower().replace("_", "-")
    if normalized in REVIEW_LANGUAGE_ALIASES:
        return REVIEW_LANGUAGE_ALIASES[normalized]

    supported = ", ".join(REVIEW_LANGUAGE_URL_PATHS)
    raise ValueError(f"Unsupported review language '{language}'. Supported values: {supported}")


def build_review_url(language):
    normalized_language = normalize_review_language(language)
    return f"{REVIEW_BASE_URL}{REVIEW_LANGUAGE_URL_PATHS[normalized_language]}"


class ReviewSubmissionCoordinator:
    def __init__(
        self,
        base_interval=6.0,
        max_interval=20.0,
        cooldown_seconds=30.0,
        failure_threshold=2,
    ):
        self.base_interval = base_interval
        self.current_interval = base_interval
        self.max_interval = max_interval
        self.cooldown_seconds = cooldown_seconds
        self.failure_threshold = failure_threshold
        self.condition = threading.Condition()
        self.active_uuid = None
        self.next_submit_time = 0.0
        self.cooldown_until = 0.0
        self.consecutive_failures = 0

    def wait_for_submit_slot(self, uuid):
        with self.condition:
            while True:
                now = time.monotonic()
                if self.active_uuid is None and now >= self.next_submit_time and now >= self.cooldown_until:
                    self.active_uuid = uuid
                    return

                wake_at = max(self.next_submit_time, self.cooldown_until)
                if self.active_uuid is not None:
                    timeout = 0.5
                else:
                    timeout = max(0.2, wake_at - now)
                self.condition.wait(timeout=min(timeout, 1.0))

    def release_submit_slot(self, uuid, token_wait_seconds=0.0):
        with self.condition:
            if self.active_uuid is None or self.active_uuid != uuid:
                return
            self.active_uuid = None

            self._adapt_interval_from_token(token_wait_seconds)
            self.next_submit_time = max(time.monotonic(), self.next_submit_time) + self.current_interval
            self.condition.notify_all()

    def report_outcome(
        self,
        uuid,
        success,
        error_text="",
        token_wait_seconds=0.0,
        submit_wait_seconds=0.0,
        result_wait_seconds=0.0,
    ):
        del uuid
        with self.condition:
            if success:
                self.consecutive_failures = 0
                if token_wait_seconds <= 8 and submit_wait_seconds <= 8 and result_wait_seconds <= 18:
                    self.current_interval = max(self.base_interval, self.current_interval - 0.5)
                elif token_wait_seconds >= 6 or submit_wait_seconds >= 10 or result_wait_seconds >= 18:
                    self.current_interval = min(self.max_interval, self.current_interval + 1.5)
            else:
                normalized = (error_text or "").lower()
                if any(
                    marker in normalized
                    for marker in (
                        "captcha",
                        "turnstile",
                        "rate limit",
                        "stalled before token issuance",
                        "timed out waiting for turnstile token",
                        "review submission never left the form page",
                        "timed out waiting for review results",
                    )
                ):
                    self.consecutive_failures += 1
                    self.current_interval = min(self.max_interval, self.current_interval + 2.5)
                    if self.consecutive_failures >= self.failure_threshold:
                        self.cooldown_until = max(
                            self.cooldown_until,
                            time.monotonic() + self.cooldown_seconds,
                        )
                        logging.warning(
                            "[Throttle] Consecutive review failures detected. "
                            f"Cooling down submissions for {self.cooldown_seconds:.0f}s."
                        )
                else:
                    self.consecutive_failures = 0

            self.condition.notify_all()

    def _adapt_interval_from_token(self, token_wait_seconds):
        if token_wait_seconds <= 0:
            return
        if token_wait_seconds >= 20:
            self.current_interval = min(self.max_interval, self.current_interval + 2.0)
        elif token_wait_seconds >= 12:
            self.current_interval = min(self.max_interval, self.current_interval + 1.0)
        elif token_wait_seconds <= 6:
            self.current_interval = max(self.base_interval, self.current_interval - 0.25)


class BrowserAutomator:
    def __init__(
        self,
        headless=True,
        proxy=None,
        submission_coordinator=None,
        controlled_submission=True,
        review_language=DEFAULT_REVIEW_LANGUAGE,
        review_ui=DEFAULT_REVIEW_UI,
    ):
        self.headless = headless
        self.proxy = proxy
        self.review_language = normalize_review_language(review_language)
        self.review_ui = normalize_review_ui(review_ui)
        self.review_url = build_review_url(self.review_language)
        self.controlled_submission = controlled_submission
        if controlled_submission:
            self.submission_coordinator = submission_coordinator or ReviewSubmissionCoordinator()
        else:
            self.submission_coordinator = None

    def run_worker(self, task_queue, result_queue, max_retries=3):
        """Drain a prefilled queue; own each task until one terminal event is emitted.

        Each invocation owns its browser. Selenium GUI workers should run in
        separate processes, with one consumer responsible for table output.
        """
        stack = ExitStack()
        sb = None
        completed = 0

        def recycle():
            nonlocal stack, sb, completed
            try:
                stack.close()
            except Exception:
                logging.exception("Failed to close worker browser")
            stack = ExitStack()
            sb = None
            completed = 0

        try:
            while True:
                try:
                    queued_task = task_queue.get_nowait()
                except queue.Empty:
                    return
                try:
                    task = deepcopy(queued_task)
                    terminal = None
                    for attempt in range(max(0, max_retries) + 1):
                        try:
                            if sb is None:
                                stack.enter_context(BROWSER_SESSION_LOCK)
                                sb = stack.enter_context(SB(uc=True, headless=self.headless, proxy=self.proxy))
                            result = self.analyze_single(sb, task)
                            if not result:
                                raise RuntimeError("Review returned no result")
                            terminal = deepcopy({"status": "success", "task": task, "result": result})
                            break
                        except Exception as exc:
                            logging.exception("%s attempt %s failed", task.get("log_prefix", task["uuid"]), attempt + 1)
                            task["retries"] = attempt + 1
                            if sb is None or any(marker in str(exc).lower() for marker in
                                                  ("no such window", "closed", "invalid session", "disconnected")):
                                recycle()
                    if terminal is None:
                        terminal = {"status": "fail", "task": deepcopy(task)}
                    result_queue.put(terminal)
                    completed += 1
                    if completed >= 10:
                        recycle()
                finally:
                    task_queue.task_done()
        finally:
            recycle()

    def iter_alternating_windows(self, tasks, max_retries=3):
        pending = deque(deepcopy(task) for task in tasks)

        with BROWSER_SESSION_LOCK, SB(uc=True, headless=self.headless, proxy=self.proxy) as sb:
            slots = [
                {
                    "name": "window-a",
                    "handle": sb.driver.current_window_handle,
                    "ready": False,
                },
                {
                    "name": "window-b",
                    "handle": None,
                    "ready": False,
                },
            ]

            slot_index = 0
            while pending:
                task = pending.popleft()
                slot = slots[slot_index]
                slot_index = (slot_index + 1) % len(slots)

                try:
                    started_at = time.perf_counter()
                    self._ensure_rotation_slot_ready(sb, slot, task.get("log_prefix", '[' + task["uuid"] + ']'))
                    result = self._analyze_loaded_form(
                        sb,
                        task,
                        started_at=started_at,
                        ready_message=f"{slot['name']} ready for assigned task",
                    )
                    yield deepcopy({"status": "success", "task": task, "result": result})
                except Exception as exc:
                    failure_event = self._handle_rotation_failure(
                        sb,
                        slot,
                        task,
                        exc,
                        max_retries,
                        pending,
                    )
                    if failure_event:
                        yield deepcopy(failure_event)
                finally:
                    slot["ready"] = False

    def iter_dual_window_pipeline(self, tasks, max_retries=3):
        pending = deque(deepcopy(task) for task in tasks)

        with BROWSER_SESSION_LOCK, SB(uc=True, headless=self.headless, proxy=self.proxy) as sb:
            active_slot = {
                "name": "active",
                "handle": sb.driver.current_window_handle,
                "task": None,
                "prepared": False,
                "started_at": 0.0,
                "submitted_at": 0.0,
            }
            standby_slot = {
                "name": "standby",
                "handle": self._open_pipeline_window(sb),
                "task": None,
                "prepared": False,
                "started_at": 0.0,
                "submitted_at": 0.0,
            }

            while pending or active_slot["task"] or standby_slot["task"]:
                if active_slot["task"] is None:
                    if standby_slot["task"] and standby_slot["prepared"]:
                        active_slot, standby_slot = standby_slot, active_slot
                    elif standby_slot["task"] and not standby_slot["prepared"]:
                        pending.appendleft(standby_slot["task"])
                        self._reset_pipeline_slot(standby_slot)
                        continue
                    elif pending:
                        task = pending.popleft()
                        failure_event = self._prepare_pipeline_slot(sb, active_slot, task, max_retries, pending)
                        if failure_event:
                            yield deepcopy(failure_event)
                            continue
                    else:
                        break

                if active_slot["task"] is None:
                    break

                active_task = active_slot["task"]

                try:
                    token_wait_seconds = self._submit_pipeline_slot(sb, active_slot, active_task)

                    if standby_slot["task"] is None and pending:
                        next_task = pending.popleft()
                        failure_event = self._prepare_pipeline_slot(sb, standby_slot, next_task, max_retries, pending)
                        if failure_event:
                            yield deepcopy(failure_event)

                    result = self._collect_pipeline_result(sb, active_slot, active_task)
                    yield deepcopy({"status": "success", "task": active_task, "result": result})
                except Exception as exc:
                    failure_event = self._handle_pipeline_failure(
                        sb,
                        active_slot,
                        active_task,
                        exc,
                        max_retries,
                        pending,
                    )
                    if failure_event:
                        yield deepcopy(failure_event)
                finally:
                    active_slot["task"] = None
                    active_slot["prepared"] = False
                    active_slot["started_at"] = 0.0
                    active_slot["submitted_at"] = 0.0

                if standby_slot["task"] and standby_slot["prepared"]:
                    active_slot, standby_slot = standby_slot, active_slot

    def analyze_single(self, sb, task):
        uuid = task["uuid"]
        log_prefix = task.get("log_prefix", f"[{uuid}]")
        started_at = time.perf_counter()

        logging.info(f"{log_prefix} Opening fresh review form")
        self._open_fresh_review_page(sb, log_prefix)
        return self._analyze_loaded_form(
            sb,
            task,
            started_at=started_at,
            ready_message="Form ready and waiting for submit slot",
        )

    def _analyze_loaded_form(self, sb, task, started_at, ready_message):
        paipu_url = task["paipu_url"]
        uuid = task["uuid"]
        log_prefix = task.get("log_prefix", f"[{uuid}]")
        model_tag = task["model_tag"]
        output_dir = task["mode_dir"]
        save_screenshot = task.get("save_screenshot", False)

        os.makedirs(output_dir, exist_ok=True)
        screenshot_path = os.path.join(output_dir, f"{uuid}.png")
        local_paipu_path = os.path.join(output_dir, f"{uuid}.html")
        save_local_paipu = task.get("save_local_paipu", False)
        token_wait_seconds = 0.0
        submit_wait_seconds = 0.0
        result_wait_seconds = 0.0
        submit_slot_held = False
        submit_slot_released = False
        submit_owner = object()

        self._populate_form(sb, paipu_url, model_tag)
        if "player_id" in task:
            self._select_player(sb, task["player_id"])
        logging.info(f"{log_prefix} {ready_message}")

        try:
            if self.submission_coordinator is not None:
                self.submission_coordinator.wait_for_submit_slot(submit_owner)
                submit_slot_held = True
                logging.info(f"{log_prefix} Submit slot granted, starting Turnstile")
            else:
                logging.info(f"{log_prefix} Unthrottled mode, starting Turnstile")
            self._prepare_review_form(sb)
            self._poke_captcha(sb)

            token_started_at = time.perf_counter()
            self._wait_for_turnstile_token(
                sb, log_prefix, timeout=max(10, int(task.get("turnstile_timeout", 35)))
            )
            token_wait_seconds = time.perf_counter() - token_started_at
            logging.info(f"{log_prefix} Turnstile token ready in {token_wait_seconds:.1f}s")

            submit_started_at = time.perf_counter()
            self._submit_review(sb, log_prefix)
            self._wait_for_submission_departure_or_error(sb, log_prefix, timeout=15)
            submit_wait_seconds = time.perf_counter() - submit_started_at
            if self.submission_coordinator is not None:
                self.submission_coordinator.release_submit_slot(submit_owner, token_wait_seconds=token_wait_seconds)
                submit_slot_released = True

            logging.info(f"{log_prefix} Review submitted, waiting for final result")
            result_started_at = time.perf_counter()
            self._wait_for_result_or_error(sb, log_prefix, timeout=45)
            result_wait_seconds = time.perf_counter() - result_started_at
            result_url = sb.get_current_url()
            metadata = self._extract_metadata(sb)
            self._validate_report_player(task, metadata)
            bad_move_stats = {}
            if task.get("analyze_bad_move_rate", False):
                bad_move_stats = self._extract_bad_move_stats(sb, log_prefix)
            if self.submission_coordinator is not None:
                self.submission_coordinator.report_outcome(
                    log_prefix,
                    success=True,
                    token_wait_seconds=token_wait_seconds,
                    submit_wait_seconds=submit_wait_seconds,
                    result_wait_seconds=result_wait_seconds,
                )
            logging.info(
                f"{log_prefix} Result ready in {time.perf_counter() - started_at:.1f}s: {result_url}"
            )

            saved_screenshot_path = ""
            if save_screenshot:
                self._expand_metadata_panel(sb, log_prefix)
                sb.save_screenshot(screenshot_path)
                saved_screenshot_path = screenshot_path
                logging.info(f"{log_prefix} Screenshot saved to {screenshot_path}")

            saved_local_paipu_path = ""
            if save_local_paipu:
                saved_local_paipu_path = self._save_local_paipu(
                    sb,
                    local_paipu_path,
                    result_url,
                    log_prefix,
                )

            return {
                "resultUrl": result_url,
                "localPaipuPath": saved_local_paipu_path,
                "screenshotPath": saved_screenshot_path,
                "metadata": metadata,
                "badMoveStats": bad_move_stats,
                "playerNames": self._extract_player_names(sb),
            }
        except Exception as exc:
            if self.submission_coordinator is not None:
                self.submission_coordinator.report_outcome(
                    log_prefix,
                    success=False,
                    error_text=str(exc),
                    token_wait_seconds=token_wait_seconds,
                    submit_wait_seconds=submit_wait_seconds,
                    result_wait_seconds=result_wait_seconds,
                )
            raise
        finally:
            if self.submission_coordinator is not None and submit_slot_held and not submit_slot_released:
                self.submission_coordinator.release_submit_slot(
                    submit_owner,
                    token_wait_seconds=token_wait_seconds,
                )

    def _handle_rotation_failure(self, sb, slot, task, exc, max_retries, pending):
        err_str = str(exc).lower()
        logging.error(f"{task.get('log_prefix', '[' + task['uuid'] + ']')} ERROR exception: {exc}")

        try:
            self._switch_to_slot(sb, slot)
            error_screenshot = os.path.join(task["mode_dir"], f"{task['uuid']}_error.png")
            sb.save_screenshot(error_screenshot)
        except Exception:
            pass

        if any(marker in err_str for marker in ("no such window", "closed", "invalid session", "disconnected")):
            raise exc

        task["retries"] = task.get("retries", 0) + 1
        if task["retries"] <= max_retries:
            logging.warning(
                f"{task.get('log_prefix', '[' + task['uuid'] + ']')} RETRY Analysis failed. "
                f"Retrying ({task['retries']}/{max_retries}) on the next window turn."
            )
            pending.append(task)
            return None

        logging.error(
            f"{task.get('log_prefix', '[' + task['uuid'] + ']')} SKIP Analysis permanently failed after {max_retries} retries."
        )
        return {"status": "fail", "task": task}

    def _open_pipeline_window(self, sb):
        current_handle = sb.driver.current_window_handle
        try:
            sb.driver.switch_to.new_window("window")
            new_handle = sb.driver.current_window_handle
            sb.driver.switch_to.window(current_handle)
            return new_handle
        except Exception:
            existing_handles = set(sb.driver.window_handles)
            sb.execute_script("window.open('about:blank', '_blank');")

            deadline = time.time() + 5
            while time.time() < deadline:
                current_handles = set(sb.driver.window_handles)
                new_handles = current_handles - existing_handles
                if new_handles:
                    new_handle = new_handles.pop()
                    sb.driver.switch_to.window(current_handle)
                    return new_handle
                time.sleep(0.2)

        raise RuntimeError("Could not open standby browser window")

    def _prepare_pipeline_slot(self, sb, slot, task, max_retries, pending):
        try:
            self._switch_to_slot(sb, slot)
            self._prepare_task_in_current_tab(sb, task)
            self._refresh_slot_handle(sb, slot)
            slot["task"] = task
            slot["prepared"] = True
            slot["started_at"] = time.perf_counter()
            slot["submitted_at"] = 0.0
            logging.info(f"{task.get('log_prefix', '[' + task['uuid'] + ']')} Form prewarmed in {slot['name']} tab")
            return None
        except Exception as exc:
            return self._handle_pipeline_failure(sb, slot, task, exc, max_retries, pending)

    def _submit_pipeline_slot(self, sb, slot, task):
        self._switch_to_slot(sb, slot)
        log_prefix = task.get("log_prefix", f"[{task['uuid']}]")
        owner = object()
        held = False
        token_wait_seconds = 0.0
        try:
            if self.submission_coordinator is not None:
                self.submission_coordinator.wait_for_submit_slot(owner)
                held = True
            logging.info(f"{log_prefix} {slot['name']} tab entering Turnstile")
            token_started_at = time.perf_counter()
            self._wait_for_turnstile_token(sb, log_prefix, timeout=max(10, int(task.get("turnstile_timeout", 35))))
            token_wait_seconds = time.perf_counter() - token_started_at
            self._submit_review(sb, log_prefix)
            self._wait_for_submission_departure_or_error(sb, log_prefix, timeout=15)
            self._refresh_slot_handle(sb, slot)
            slot["submitted_at"] = time.perf_counter()
            return token_wait_seconds
        finally:
            if held:
                self.submission_coordinator.release_submit_slot(owner, token_wait_seconds=token_wait_seconds)

    def _collect_pipeline_result(self, sb, slot, task):
        self._switch_to_slot(sb, slot)
        uuid = task["uuid"]
        log_prefix = task.get("log_prefix", f"[{uuid}]")
        screenshot_path = os.path.join(task["mode_dir"], f"{uuid}.png")
        local_paipu_path = os.path.join(task["mode_dir"], f"{uuid}.html")
        save_screenshot = task.get("save_screenshot", False)
        save_local_paipu = task.get("save_local_paipu", False)

        logging.info(f"{log_prefix} Waiting for result page")
        self._wait_for_result_or_error(sb, log_prefix, timeout=45)
        result_url = sb.get_current_url()
        metadata = self._extract_metadata(sb)
        self._validate_report_player(task, metadata)
        bad_move_stats = {}
        if task.get("analyze_bad_move_rate", False):
            bad_move_stats = self._extract_bad_move_stats(sb, log_prefix)

        total_elapsed = time.perf_counter() - slot["started_at"] if slot["started_at"] else 0.0
        logging.info(f"{log_prefix} Result ready in {total_elapsed:.1f}s: {result_url}")

        saved_screenshot_path = ""
        if save_screenshot:
            self._expand_metadata_panel(sb, log_prefix)
            sb.save_screenshot(screenshot_path)
            saved_screenshot_path = screenshot_path
            logging.info(f"{log_prefix} Screenshot saved to {screenshot_path}")

        saved_local_paipu_path = ""
        if save_local_paipu:
            saved_local_paipu_path = self._save_local_paipu(
                sb,
                local_paipu_path,
                result_url,
                log_prefix,
            )

        return {
            "resultUrl": result_url,
            "localPaipuPath": saved_local_paipu_path,
            "screenshotPath": saved_screenshot_path,
            "metadata": metadata,
            "badMoveStats": bad_move_stats,
            "playerNames": self._extract_player_names(sb),
        }

    def _handle_pipeline_failure(self, sb, slot, task, exc, max_retries, pending):
        err_str = str(exc).lower()
        logging.error(f"{task.get('log_prefix', '[' + task['uuid'] + ']')} ERROR exception: {exc}")

        try:
            self._switch_to_slot(sb, slot)
            error_screenshot = os.path.join(task["mode_dir"], f"{task['uuid']}_error.png")
            sb.save_screenshot(error_screenshot)
        except Exception:
            pass

        if any(marker in err_str for marker in ("no such window", "closed", "invalid session", "disconnected")):
            raise exc

        self._reset_pipeline_slot(slot)
        task["retries"] = task.get("retries", 0) + 1
        if task["retries"] <= max_retries:
            logging.warning(
                f"{task.get('log_prefix', '[' + task['uuid'] + ']')} RETRY Analysis failed. "
                f"Retrying ({task['retries']}/{max_retries}) with a fresh page load."
            )
            pending.append(task)
            return None

        logging.error(
            f"{task.get('log_prefix', '[' + task['uuid'] + ']')} SKIP Analysis permanently failed after {max_retries} retries."
        )
        return {"status": "fail", "task": task}

    def _reset_pipeline_slot(self, slot):
        slot["task"] = None
        slot["prepared"] = False
        slot["started_at"] = 0.0
        slot["submitted_at"] = 0.0

    def _switch_to_slot(self, sb, slot):
        handles = list(sb.driver.window_handles)
        if slot["handle"] not in handles:
            raise RuntimeError(f"{slot['name']} assigned browser window is closed")

        sb.driver.switch_to.window(slot["handle"])
        try:
            sb.wait_for_ready_state_complete()
        except Exception:
            pass

    def _refresh_slot_handle(self, sb, slot):
        try:
            slot["handle"] = sb.driver.current_window_handle
        except Exception:
            pass

    def _prepare_task_in_current_tab(self, sb, task):
        uuid = task["uuid"]
        log_prefix = task.get("log_prefix", f"[{uuid}]")
        logging.info(f"{log_prefix} Opening fresh review form")
        self._open_fresh_review_page(sb, log_prefix)
        self._populate_form(sb, task["paipu_url"], task["model_tag"])
        if "player_id" in task:
            self._select_player(sb, task["player_id"])
        self._prepare_review_form(sb)
        self._poke_captcha(sb)

    def _prime_rotation_slot(self, sb, slot, label):
        if not slot.get("handle"):
            self._spawn_rotation_window(sb, slot, label)
        self._switch_to_slot(sb, slot)
        logging.info(f"{label} Refreshing {slot['name']} back to review form")
        self._open_fresh_review_page(sb, label)
        self._refresh_slot_handle(sb, slot)
        slot["ready"] = True

    def _ensure_rotation_slot_ready(self, sb, slot, label):
        if not slot.get("handle"):
            self._spawn_rotation_window(sb, slot, label)
        self._switch_to_slot(sb, slot)
        if not slot.get("ready", False) or not self._is_review_form_ready(sb):
            logging.info(f"{label} Preparing {slot['name']} for the next assigned task")
            self._prime_rotation_slot(sb, label=label, slot=slot)
            slot["ready"] = True
        self._refresh_slot_handle(sb, slot)

    def _is_review_form_ready(self, sb):
        try:
            return bool(
                sb.execute_script(
                    """
                    const input = document.querySelector(arguments[0]);
                    return !!(input && document.readyState !== 'loading');
                    """,
                    INPUT_SELECTOR,
                )
            )
        except Exception:
            return False

    def _spawn_rotation_window(self, sb, slot, label):
        current_handle = sb.driver.current_window_handle
        existing_handles = set(sb.driver.window_handles)
        logging.info(f"{label} Spawning {slot['name']} from the active review context")

        try:
            sb.execute_script("window.open(arguments[0], '_blank');", self.review_url)
        except Exception:
            sb.driver.switch_to.new_window("window")
            slot["handle"] = sb.driver.current_window_handle
            self._open_fresh_review_page(sb, label)
            self._refresh_slot_handle(sb, slot)
            sb.driver.switch_to.window(current_handle)
            return

        deadline = time.time() + 8
        while time.time() < deadline:
            current_handles = set(sb.driver.window_handles)
            new_handles = list(current_handles - existing_handles)
            if new_handles:
                slot["handle"] = new_handles[-1]
                self._switch_to_slot(sb, slot)
                logging.info(f"{label} {slot['name']} spawned successfully")
                self._refresh_slot_handle(sb, slot)
                try:
                    sb.driver.switch_to.window(current_handle)
                except Exception:
                    pass
                return
            time.sleep(0.2)

        raise RuntimeError(f"{label} Could not spawn {slot['name']}")

    def _open_fresh_review_page(self, sb, label):
        last_exc = None
        for attempt in range(2):
            current_url = ""
            try:
                current_url = sb.get_current_url()
            except Exception:
                current_url = ""

            try:
                if "mjai.ekyu.moe" in current_url:
                    sb.execute_script("window.location.replace(arguments[0]);", self.review_url)
                else:
                    sb.uc_open_with_reconnect(self.review_url, reconnect_time=2)
            except Exception:
                sb.open(self.review_url)

            try:
                sb.wait_for_ready_state_complete()
            except Exception:
                pass

            try:
                sb.wait_for_element(INPUT_SELECTOR, timeout=20)
                return
            except Exception as exc:
                last_exc = exc
                if attempt == 0:
                    logging.warning(f"{label} Review page not ready, retrying open once...")
                    try:
                        sb.execute_script("window.location.replace(arguments[0]);", self.review_url)
                    except Exception:
                        try:
                            sb.refresh()
                        except Exception:
                            pass
                    time.sleep(2)

        raise last_exc

    def _prepare_review_form(self, sb):
        sb.execute_script(
            r"""
            if (!window.__batchmortalTurnstileErrors) {
              window.__batchmortalTurnstileErrors = [];
              window.addEventListener('error', event => {
                const match = String(event.message || '').match(/Turnstile.*Error:\s*(\d+)/i);
                if (match) window.__batchmortalTurnstileErrors.push(match[1]);
              });
            }
            const submit = document.querySelector(arguments[0]);
            if (submit) {
              submit.classList.remove('is-loading');
              submit.disabled = false;
              submit.style.pointerEvents = '';
            }

            const form = document.querySelector(arguments[1]);
            if (form) {
              form.target = '_self';
            }
            """,
            SUBMIT_SELECTOR,
            FORM_SELECTOR,
        )

    def _validate_report_player(self, task, metadata):
        if "player_id" not in task:
            return
        from .results import parse_metadata
        actual = parse_metadata(metadata).get("playerId", "")
        if str(actual) != str(task["player_id"]):
            raise RuntimeError(f"Report player mismatch: expected {task['player_id']}, got {actual}")

    def _select_player(self, sb, player_id):
        if type(player_id) is not int or player_id not in range(4):
            raise ValueError("player_id must be an integer from 0 to 3")
        selected = sb.execute_script(
            """
            const el = document.querySelector('select[name="player-id"]');
            if (!el) return false;
            el.value = String(arguments[0]);
            el.dispatchEvent(new Event('change', {bubbles: true}));
            return el.value === String(arguments[0]);
            """, player_id,
        )
        if not selected:
            raise RuntimeError("Could not select review player")

    def _extract_player_names(self, sb):
        try:
            return sb.execute_script(
                """
                const data = window.MM?.GS?.fullData;
                const start = Array.isArray(data?.mjai_log)
                  ? data.mjai_log.find(e => e.type === 'start_game') : null;
                return Array.isArray(start?.names) ? start.names
                  : (Array.isArray(data?.names) ? data.names : []);
                """
            ) or []
        except Exception:
            return []

    def _populate_form(self, sb, paipu_url, model_tag):
        success = sb.execute_script(
            """
            const paipuUrl = arguments[0];
            const modelTag = arguments[1];
            const reviewLanguage = arguments[2];
            const reviewUi = arguments[3];

            const dispatch = (el) => {
              el.dispatchEvent(new Event('input', { bubbles: true }));
              el.dispatchEvent(new Event('change', { bubbles: true }));
            };

            const radio = document.querySelector('input[name="input-method"][value="log-url"]');
            if (radio && !radio.checked) {
              radio.click();
            }

            const input = document.querySelector('input[name="log-url"]');
            if (!input) {
              return false;
            }
            if (input.value !== paipuUrl) {
              input.value = paipuUrl;
              dispatch(input);
            }

            const setSelect = (selector, value) => {
              const el = document.querySelector(selector);
              if (!el || el.value === value) {
                return;
              }
              el.value = value;
              dispatch(el);
            };

            setSelect('select[name="engine"]', 'mortal');
            setSelect('select[name="mortal-model-tag"]', modelTag);
            setSelect('select[name="ui"]', reviewUi);
            setSelect('select[name="lang"]', reviewLanguage);

            const details = document.querySelector('details.details.mb-3');
            if (details) {
              details.open = true;
            }

            const showRating = document.querySelector('input[name="show-rating"]');
            if (showRating && !showRating.checked) {
              showRating.click();
            }

            const form = document.querySelector(arguments[4]);
            if (form) {
              form.target = '_self';
            }

            return true;
            """,
            paipu_url,
            model_tag,
            self.review_language,
            self.review_ui,
            FORM_SELECTOR,
        )

        if not success:
            raise RuntimeError("Could not populate review form")

    def _wait_for_turnstile_token(self, sb, log_prefix, timeout):
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            state = self._read_review_state(sb)
            if state["token_length"] > 0:
                return

            if state["page_text"] and (
                "invalid captcha response" in state["page_text"]
                or "timeout-or-duplicate" in state["page_text"]
            ):
                raise RuntimeError(f"{log_prefix} Turnstile token was rejected before submission")

            if state.get("turnstile_error"):
                raise RuntimeError(f"{log_prefix} Turnstile challenge failed (code {state['turnstile_error']})")

            # Resetting every eight seconds interrupted slow verification and
            # GUI calls could overrun the configured timeout. A failed job's
            # bounded retry opens a fresh page instead.
            time.sleep(min(0.5, max(0, deadline - time.monotonic())))

        raise RuntimeError(f"{log_prefix} Timed out waiting for Turnstile token")

    def _submit_review(self, sb, log_prefix):
        submitted = sb.execute_script(
            """
            const form = document.querySelector(arguments[0]);
            const submit = document.querySelector(arguments[1]);
            const token = document.querySelector(arguments[2]);
            if (!form || !submit) {
              return 'missing-form';
            }
            if (!token || !token.value) {
              return 'missing-token';
            }

            form.target = '_self';
            submit.disabled = false;
            submit.classList.remove('is-loading');
            submit.style.pointerEvents = '';

            if (typeof form.requestSubmit === 'function') {
              form.requestSubmit(submit);
            } else {
              submit.click();
            }
            return 'submitted';
            """,
            FORM_SELECTOR,
            SUBMIT_SELECTOR,
            TURNSTILE_RESPONSE_SELECTOR,
        )

        if submitted != "submitted":
            raise RuntimeError(f"{log_prefix} Review form submission failed before navigation: {submitted}")

        time.sleep(0.2)

    def _wait_for_submission_departure_or_error(self, sb, log_prefix, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self._read_review_state(sb)
            current_url = state["url"]
            page_text = state["page_text"]

            if REPORT_URL_FRAGMENT in current_url and current_url != self.review_url:
                return

            if "invalid captcha response" in page_text or "timeout-or-duplicate" in page_text:
                raise RuntimeError(f"{log_prefix} Turnstile token was rejected")

            if "too many requests" in page_text or "rate limit" in page_text:
                raise RuntimeError(f"{log_prefix} Review site rate limited this request")

            time.sleep(0.5)

        raise RuntimeError(f"{log_prefix} Review submission never left the form page")

    def _extract_metadata(self, sb):
        killerducky_data = self._extract_killerducky_data(sb)
        if killerducky_data:
            return parse_killerducky_metadata(killerducky_data)
        if self.review_ui == "killerducky":
            raise RuntimeError("Could not extract KillerDucky report JSON")

        metadata = sb.execute_script(
            """
            const data = {};
            for (const dl of document.querySelectorAll('details > dl')) {
              const dts = dl.querySelectorAll('dt');
              const dds = dl.querySelectorAll('dd');
              const count = Math.min(dts.length, dds.length);
              for (let i = 0; i < count; i += 1) {
                data[dts[i].textContent.trim()] = dds[i].textContent.trim();
              }
            }
            return data;
            """
        )
        return metadata or {}

    def _extract_killerducky_data(self, sb, include_entries=False):
        try:
            data = sb.execute_script(
                """
                if (!(window.MM && window.MM.GS && window.MM.GS.fullData)) {
                  return null;
                }

                const source = window.MM.GS.fullData;
                const review = source.review || {};
                const includeEntries = arguments[0];
                return {
                  engine: source.engine,
                  game_length: source.game_length,
                  review_time: source.review_time,
                  player_id: source.player_id,
                  review: {
                    model_tag: review.model_tag,
                    rating: review.rating,
                    temperature: review.temperature,
                    total_matches: review.total_matches,
                    total_reviewed: review.total_reviewed,
                    kyokus: includeEntries && Array.isArray(review.kyokus)
                      ? review.kyokus.map((kyoku) => ({
                          entries: Array.isArray(kyoku.entries) ? kyoku.entries.map((entry) => {
                            const actualIndex = entry.actual_index;
                            const details = Array.isArray(entry.details)
                              ? entry.details.map((detail, index) => (
                                  index === actualIndex ? {prob: detail.prob} : null
                                ))
                              : null;
                            return {
                              is_equal: entry.is_equal,
                              actual_index: actualIndex,
                              details,
                            };
                          }) : [],
                        }))
                      : [],
                  },
                };
                """,
                include_entries,
            )
            return data if isinstance(data, dict) else None
        except Exception as exc:
            if self.review_ui == "killerducky":
                raise RuntimeError("Could not read KillerDucky report JSON") from exc
            return None

    def _save_local_paipu(self, sb, filepath, source_url, log_prefix):
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        html = sb.execute_script(
            """
            const sourceUrl = arguments[0];
            const clone = document.documentElement.cloneNode(true);
            const absolutize = (selector, attr) => {
              for (const el of clone.querySelectorAll(selector)) {
                const value = el.getAttribute(attr);
                if (!value || value.startsWith('#') || value.startsWith('data:') || value.startsWith('blob:')) {
                  continue;
                }
                try {
                  el.setAttribute(attr, new URL(value, document.baseURI).href);
                } catch (e) {
                }
              }
            };

            absolutize('a[href]', 'href');
            absolutize('link[href]', 'href');
            absolutize('script[src]', 'src');
            absolutize('img[src]', 'src');
            absolutize('iframe[src]', 'src');

            const meta = clone.ownerDocument.createElement('meta');
            meta.setAttribute('name', 'batchmortal-source-url');
            meta.setAttribute('content', sourceUrl);
            const head = clone.querySelector('head');
            if (head) {
              head.insertBefore(meta, head.firstChild);
            }

            return `<!doctype html>\\n<!-- Saved from ${sourceUrl} -->\\n${clone.outerHTML}`;
            """,
            source_url,
        )
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(html or "")
        logging.info(f"{log_prefix} Local paipu saved to {filepath}")
        return filepath

    def _extract_bad_move_stats(self, sb, log_prefix):
        try:
            killerducky_data = self._extract_killerducky_data(sb, include_entries=True)
            if killerducky_data:
                return parse_killerducky_bad_move_stats(killerducky_data)

            stats = sb.execute_script(
                """
                const strictLimit = arguments[0];
                const looseLimit = arguments[1];

                const parseFirstNumber = (text) => {
                  const match = String(text || '').replace(',', '.').match(/-?\\d+(?:\\.\\d+)?/);
                  return match ? Number.parseFloat(match[0]) : NaN;
                };

                const parseFirstInteger = (text) => {
                  const match = String(text || '').match(/\\d+/);
                  return match ? Number.parseInt(match[0], 10) : NaN;
                };

                const extractTotalChoices = () => {
                  for (const dl of document.querySelectorAll('details > dl')) {
                    const dts = dl.querySelectorAll('dt');
                    const dds = dl.querySelectorAll('dd');
                    const count = Math.min(dts.length, dds.length);
                    for (let i = 0; i < count; i += 1) {
                      const key = dts[i].textContent.trim().toLowerCase();
                      if (!key.includes('一致率') && !key.includes('match')) {
                        continue;
                      }
                      const match = dds[i].textContent.match(/\\d+\\s*\\/\\s*(\\d+)/);
                      if (match) {
                        return Number.parseInt(match[1], 10);
                      }
                    }
                  }
                  return null;
                };

                const extractChosenWeight = (orderLoss) => {
                  const chosenIndex = parseFirstInteger(orderLoss.textContent);
                  if (!Number.isFinite(chosenIndex) || chosenIndex < 1) {
                    return NaN;
                  }

                  let collapseEntry = orderLoss.closest('.collapse.entry');
                  if (!collapseEntry) {
                    const turnInfo = orderLoss.parentElement;
                    const summary = turnInfo ? turnInfo.parentElement : null;
                    collapseEntry = summary ? summary.parentElement : null;
                  }
                  if (!collapseEntry) {
                    return NaN;
                  }

                  const rows = collapseEntry.querySelectorAll('table tbody tr');
                  const chosenRow = rows[chosenIndex - 1];
                  if (!chosenRow) {
                    return NaN;
                  }

                  const cells = chosenRow.querySelectorAll('td, th');
                  const weightCell = cells.length ? cells[cells.length - 1] : chosenRow.lastElementChild;
                  return weightCell ? parseFirstNumber(weightCell.textContent) : NaN;
                };

                let countStrict = 0;
                let countLoose = 0;
                let unparsed = 0;
                const orderLosses = Array.from(document.getElementsByClassName('order-loss'));

                for (const orderLoss of orderLosses) {
                  const chosenWeight = extractChosenWeight(orderLoss);
                  if (!Number.isFinite(chosenWeight)) {
                    unparsed += 1;
                    continue;
                  }
                  if (chosenWeight <= strictLimit) {
                    countStrict += 1;
                  }
                  if (chosenWeight <= looseLimit) {
                    countLoose += 1;
                  }
                }

                const totalChoices = extractTotalChoices();
                const formatRate = (count) => (
                  Number.isFinite(totalChoices) && totalChoices > 0
                    ? `${(100 * count / totalChoices).toFixed(3)}%`
                    : ''
                );

                return {
                  badMoveRate5: formatRate(countStrict),
                  badMoveCount5: String(countStrict),
                  badMoveRate10: formatRate(countLoose),
                  badMoveCount10: String(countLoose),
                  badMoveDenominator: Number.isFinite(totalChoices) ? String(totalChoices) : '',
                  badMoveOrderLossCount: String(orderLosses.length),
                  badMoveUnparsedCount: String(unparsed),
                };
                """,
                BAD_MOVE_STRICT_LIMIT,
                BAD_MOVE_LOOSE_LIMIT,
            )
            return stats or {}
        except Exception as exc:
            logging.warning(f"{log_prefix} Could not extract bad move stats: {exc}")
            return {}

    def _wait_for_result_or_error(self, sb, log_prefix, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.review_ui == "killerducky":
                try:
                    killerducky_ready = sb.execute_script(
                        """
                        return !!(
                          window.MM && window.MM.GS && window.MM.GS.fullData
                          && window.MM.GS.fullData.review
                          && document.querySelector('.about-metadata table')
                        );
                        """
                    )
                except Exception:
                    killerducky_ready = False
                if killerducky_ready:
                    return
            elif sb.is_element_present(RESULT_SELECTOR):
                return

            state = self._read_review_state(sb)
            page_text = state["page_text"]

            if "invalid captcha response" in page_text or "timeout-or-duplicate" in page_text:
                raise RuntimeError(f"{log_prefix} Turnstile token was rejected")

            if "too many requests" in page_text or "rate limit" in page_text:
                raise RuntimeError(f"{log_prefix} Review site rate limited this request")

            time.sleep(0.5)

        raise RuntimeError(f"{log_prefix} Timed out waiting for review results")

    def _read_review_state(self, sb):
        return sb.execute_script(
            """
            const submit = document.querySelector(arguments[0]);
            const token = document.querySelector(arguments[1]);
            return {
              url: window.location.href,
              token_length: token && token.value ? token.value.length : 0,
              turnstile_error: window.__batchmortalTurnstileErrors?.at(-1) || '',
              page_text: document.body ? document.body.innerText.toLowerCase() : '',
              submit_disabled: submit ? !!submit.disabled : true,
              submit_busy: submit ? submit.classList.contains('is-loading') : false,
            };
            """,
            SUBMIT_SELECTOR,
            TURNSTILE_RESPONSE_SELECTOR,
        )

    def _expand_metadata_panel(self, sb, log_prefix):
        try:
            if self.review_ui == "killerducky":
                sb.execute_script(
                    """
                    const modal = document.getElementById('about-modal');
                    if (modal && !modal.open) {
                      modal.showModal();
                    }
                    """
                )
                time.sleep(0.5)
                return

            is_open = sb.execute_script(
                """
                const details = document.querySelector('body > details:nth-child(6)');
                return details ? details.open : false;
                """
            )
            if not is_open:
                sb.click("body > details:nth-child(6) > summary")
                time.sleep(0.5)
        except Exception as exc:
            logging.warning(f"{log_prefix} Could not expand metadata menu: {exc}")

    def _poke_captcha(self, sb):
        self._captcha_gui_error = None
        widget = 'form[name="reviewForm"] .cf-turnstile'
        review_widget = False
        try:
            review_widget = sb.execute_script(
                """return (() => {
                  const widget = document.querySelector('form[name="reviewForm"] .cf-turnstile');
                  if (!widget) return false;
                  widget.scrollIntoView({block: 'center', inline: 'nearest'});
                  return true;
                })();"""
            )
        except Exception:
            logging.debug("Could not scroll review CAPTCHA into view", exc_info=True)
        if review_widget:
            # This page contains both review and bot widgets. Auto-detection
            # can choose a generic form div or the other widget. Target the
            # review widget's on-screen checkbox with a normal GUI click.
            try:
                sb.wait_for_element(widget, timeout=10)
                rect = sb.get_gui_element_rect(widget)
                if rect['width'] < 60 or rect['height'] < 48:
                    raise RuntimeError("Review CAPTCHA is not visible")
                sb.disconnect()
                try:
                    sb.uc_gui_click_x_y(rect['x'] + 28, rect['y'] + 32)
                    time.sleep(5)
                finally:
                    sb.connect()
                return
            except Exception as exc:
                self._captcha_gui_error = type(exc).__name__
                logging.debug("Review widget GUI interaction failed", exc_info=True)
                return
        try:
            sb.uc_gui_click_captcha()
        except Exception as exc:
            self._captcha_gui_error = type(exc).__name__
            logging.debug("Review CAPTCHA GUI interaction failed", exc_info=True)

    def _recover_turnstile_widget(self, sb):
        try:
            sb.execute_script(
                """
                const token = document.querySelector(arguments[0]);
                if (token) {
                  token.value = '';
                }

                const submit = document.querySelector(arguments[1]);
                if (submit) {
                  submit.disabled = false;
                  submit.classList.remove('is-loading');
                  submit.style.pointerEvents = '';
                }

                if (window.turnstile) {
                  const widgets = Array.from(document.querySelectorAll('.cf-turnstile'));
                  for (const widget of widgets) {
                    const widgetId = widget.getAttribute('data-widget-id');
                    try {
                      if (widgetId) {
                        window.turnstile.reset(widgetId);
                      } else {
                        window.turnstile.reset();
                      }
                    } catch (e) {
                    }
                  }
                }
                """,
                TURNSTILE_RESPONSE_SELECTOR,
                SUBMIT_SELECTOR,
            )
        except Exception:
            pass
