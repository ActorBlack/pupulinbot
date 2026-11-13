"""Optional Turnstile provider for the Mortal review form."""

import logging
import re
import time
from contextlib import nullcontext
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)
CAPSOLVER = "capsolver"
CAPSOLVER_TIMEOUT = 60
BROWSER = "browser"
PROVIDERS = frozenset({BROWSER, CAPSOLVER})
API = "https://api.capsolver.com"
SITE = "https://mjai.ekyu.moe"


class CapSolverError(RuntimeError):
    def __init__(self, message, *, code=None):
        super().__init__(message)
        self.code = code


class CapSolverTransportError(CapSolverError):
    pass


def api_call(client, key, action, payload, *, timeout):
    started = time.monotonic()
    try:
        response = client.post(
            f"{API}/{action}", json={"clientKey": key, **payload}, timeout=timeout
        )
        response.raise_for_status()
        result = response.json()
    except (httpx.HTTPError, ValueError):
        raise CapSolverTransportError("CapSolver 接口连接或响应失败") from None
    finally:
        logger.info("CapSolver %s 请求耗时 %.3f 秒", action, time.monotonic() - started)
    if not isinstance(result, dict) or result.get("errorId") != 0:
        code = result.get("errorCode") if isinstance(result, dict) else None
        # Never surface vendor response bodies; they can contain secrets.
        if not isinstance(code, str) or not re.fullmatch(r"ERROR_[A-Z0-9_]{1,64}", code):
            code = "未知错误"
        raise CapSolverError(f"CapSolver 求解失败（{code}）", code=code)
    return result


def solve_turnstile(client, key, task, *, timeout=30):
    deadline = time.monotonic() + timeout

    def call(action, payload):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CapSolverError("CapSolver 等待超时，本场未提交")
        return api_call(client, key, action, payload, timeout=min(8, remaining))

    def create():
        result = call("createTask", {"task": task})
        task_id = result.get("taskId")
        if isinstance(task_id, str) and re.fullmatch(r"[\w-]{1,128}", task_id, re.ASCII):
            logger.info("CapSolver 已创建任务 %s", task_id)
        return result

    result = create()
    attempts = 1
    while True:
        if result.get("status") == "ready":
            solution = result.get("solution")
            token = solution.get("token") if isinstance(solution, dict) else None
            if (
                isinstance(token, str)
                and 0 < len(token) <= 2048
                and not any(c.isspace() for c in token)
            ):
                return token
            raise CapSolverError("CapSolver 未返回有效的 Turnstile 令牌")
        task_id = result.get("taskId")
        if (
            result.get("status") not in {"idle", "processing"}
            or not isinstance(task_id, str)
            or not task_id
        ):
            raise CapSolverError("CapSolver 未返回有效的求解任务")
        time.sleep(min(3, max(0, deadline - time.monotonic())))
        try:
            polled = call("getTaskResult", {"taskId": task_id})
        except CapSolverTransportError:
            logger.info("CapSolver 结果查询暂时失败，继续查询同一任务")
            continue
        except CapSolverError as exc:
            # Only a definitive vendor failure may create another paid task.
            # Never recreate on transport errors, local timeout or bad credentials.
            if (
                exc.code != "ERROR_CAPTCHA_SOLVE_FAILED"
                or attempts >= 2
                or deadline - time.monotonic() < 20
            ):
                raise
            logger.info("CapSolver 明确求解失败，5 秒后进行唯一一次重试")
            time.sleep(5)
            attempts += 1
            result = create()
            continue
        result = {"taskId": task_id, **polled}


def review_task(widget):
    if not isinstance(widget, dict):
        raise CapSolverError("未找到 Mortal 复盘表单的 Turnstile 验证")
    parsed = urlsplit(str(widget.get("url", "")))
    if (parsed.scheme, parsed.netloc) != ("https", "mjai.ekyu.moe") or parsed.path not in {
        "/",
        "/index.html",
        "/en.html",
        "/ja.html",
        "/zh-cn.html",
        "/zh-tw.html",
    }:
        raise CapSolverError("CapSolver 仅用于 Mortal 官方复盘表单")
    sitekey = widget.get("sitekey")
    if not isinstance(sitekey, str) or not re.fullmatch(r"[\w-]{10,128}", sitekey, re.ASCII):
        raise CapSolverError("Mortal 复盘表单的 sitekey 无效")
    task = {
        "type": "AntiTurnstileTaskProxyLess",
        "websiteURL": SITE + parsed.path,
        "websiteKey": sitekey,
    }
    metadata = {k: widget[k] for k in ("action", "cdata") if widget.get(k)}
    if metadata:
        task["metadata"] = metadata
    return task


def solve_review_form(browser, key, *, timeout=30, client=None):
    widget = browser.execute_script(
        """
        const w = document.querySelector('form[name="reviewForm"] .cf-turnstile');
        return w ? {url: location.origin + location.pathname,
          sitekey: w.dataset.sitekey, action: w.dataset.action, cdata: w.dataset.cdata,
          hasToken: Boolean(w.closest('form').querySelector(
            'input[name="cf-turnstile-response"]')?.value)} : null;
        """
    )
    task = review_task(widget)
    if widget.get("hasToken"):
        # An already-ready browser token does not need a paid request.
        return
    start = time.monotonic()
    context = (
        nullcontext(client)
        if client is not None
        else httpx.Client(trust_env=False, follow_redirects=False)
    )
    with context as session:
        token = solve_turnstile(session, key, task, timeout=timeout)
    logger.info("CapSolver 已返回令牌，耗时 %.1f 秒", time.monotonic() - start)
    apply_started = time.monotonic()
    try:
        applied = browser.execute_script(
            """
            const form = document.querySelector('form[name="reviewForm"]');
            const w = form?.querySelector('.cf-turnstile');
            if (location.origin + location.pathname !== arguments[0] ||
                !w || w.dataset.sitekey !== arguments[1] ||
                (w.dataset.action || '') !== arguments[2] ||
                (w.dataset.cdata || '') !== arguments[3]) return false;
            let input = form.querySelector('input[name="cf-turnstile-response"]');
            if (!input) {
              input = document.createElement('input');
              input.type = 'hidden';
              input.name = 'cf-turnstile-response';
              form.appendChild(input);
            }
            if (!input.value) {
              input.value = arguments[4];
              const cb = w.dataset.callback;
              if (cb && typeof window[cb] === 'function') window[cb](input.value);
            }
            return Boolean(input.value);
            """,
            task["websiteURL"],
            task["websiteKey"],
            widget.get("action") or "",
            widget.get("cdata") or "",
            token,
        )
    except Exception:
        raise CapSolverError("无法将令牌交给 Mortal 复盘表单，本场未提交") from None
    if not applied:
        raise CapSolverError("等待时 Mortal 复盘表单已变化，本场未提交")
    logger.info("CapSolver 令牌写入表单，耗时 %.3f 秒", time.monotonic() - apply_started)
