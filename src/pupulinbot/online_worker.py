"""Dedicated visible Chrome worker, with a pinned local batchmortal backend."""

import json
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

PREFIX = "PUPULIN_ONLINE "


def emit(**event):
    print(PREFIX + json.dumps(event, ensure_ascii=False), flush=True)


def trusted_result(url):
    from .batch_review import report_url
    from .review import ReviewError

    try:
        report_url(url)
    except (ReviewError, ValueError):
        return False
    return True


class BrowserWorker:
    def __init__(self, profile, model, timeout):
        from seleniumbase import Driver

        self.profile = Path(profile).resolve()
        self.profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.profile.chmod(0o700)
        self.driver = Driver(
            browser="chrome",
            headed=True,
            uc=False,
            enable_ws=True,
            page_load_strategy="eager",
            user_data_dir=str(self.profile),
        )
        self.driver.set_page_load_timeout(45)
        self.model, self.timeout = model, timeout
        self.key = None
        self.submitted = False

    def prepare(self, request):
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.support.ui import Select

        url = request["online_url"]
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "mjai.ekyu.moe" or parsed.path != "/":
            raise ValueError("在线入口地址无效")
        from .batchmortal_worker import validate_request

        paipu = validate_request(request)
        from selenium.common.exceptions import TimeoutException

        try:
            self.driver.get(url)
        except TimeoutException:
            # A slow analytics subresource must not invalidate an already loaded form.
            if not self.driver.find_elements(By.CSS_SELECTOR, "form[name=reviewForm]"):
                raise
        # Select only normal form controls. Do not inject tokens or alter challenge state.
        from .review import parse_review_link

        if parse_review_link(paipu).source == "riichi_city":
            from .batchmortal_worker import fill_riichi_city_form

            fill_riichi_city_form(self.driver, paipu)
        else:
            field = self.driver.find_element(
                By.CSS_SELECTOR, "form[name=reviewForm] input[name=log-url]"
            )
            field.clear()
            field.send_keys(paipu)
        for name, value in (
            ("player-id", str(request["seat"])),
            ("engine", "mortal"),
            ("mortal-model-tag", self.model),
            ("ui", "killerducky"),
            ("lang", "zh-CN"),
        ):
            Select(
                self.driver.find_element(
                    By.CSS_SELECTOR, f"form[name=reviewForm] select[name='{name}']"
                )
            ).select_by_value(value)
        rating = self.driver.find_element(
            By.CSS_SELECTOR, "form[name=reviewForm] input[name=show-rating]"
        )
        if not rating.is_displayed():
            section = rating.find_element(By.XPATH, "ancestor::details[1]")
            section.find_element(By.TAG_NAME, "summary").send_keys(Keys.RETURN)
        if not rating.is_selected():
            rating.send_keys(Keys.SPACE)
        self.key, self.submitted = request["key"], False

    def run(self, request):
        from selenium.webdriver.common.by import By

        key = request["key"]
        if self.key != key:
            if request.get("submitted"):
                emit(
                    status="uncertain",
                    message="上次已提交，浏览器会话已中断；请先找回报告并导入，避免重复提交",
                )
                return
            self.prepare(request)
        if trusted_result(self.driver.current_url):
            emit(status="done", url=self.driver.current_url)
            return
        if not self.submitted:
            button = self.driver.find_element(
                By.CSS_SELECTOR, "form[name=reviewForm] button[name=submitBtn]"
            )
            deadline = time.monotonic() + 15
            while not button.is_enabled() and time.monotonic() < deadline:
                time.sleep(0.5)
            if not button.is_enabled():
                self.driver.save_screenshot(str(self.profile / "last-page.png"))
                emit(
                    status="waiting_human",
                    message="请在专用 Chrome 窗口完成真人验证，然后发送 mortal resume <任务编号>",
                )
                return
            # Record intent before the irreversible external submission.
            emit(status="submitted")
            self.submitted = True
            try:
                from selenium.webdriver.common.keys import Keys

                button.send_keys(Keys.RETURN)
            except Exception:
                # Navigation may time out while the server continues reviewing.
                pass
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            url = self.driver.current_url
            if trusted_result(url):
                emit(status="done", url=url)
                return
            text = self.driver.find_element(By.TAG_NAME, "body").text.lower()
            if any(
                word in text for word in ("too many requests", "rate limit", "429", "access denied")
            ):
                emit(
                    status="rate_limited",
                    message="在线服务限流或拒绝访问，任务已暂停；请稍后检查浏览器",
                )
                return
            if any(
                word in text for word in ("verify you are human", "请验证您是真人", "just a moment")
            ):
                self.driver.save_screenshot(str(self.profile / "last-page.png"))
                emit(
                    status="waiting_human", message="已提交后遇到真人验证，请在浏览器处理并继续任务"
                )
                return
            # Don't automatically retry a POST or reload /review after an error.
            if any(
                word in text
                for word in ("internal server error", "bad request", "service unavailable")
            ):
                emit(
                    status="uncertain",
                    message="提交后服务报错，请检查浏览器并找回报告；不会自动重复提交",
                )
                return
            time.sleep(2)
        emit(
            status="uncertain",
            message="等待报告超时；保留浏览器，再次继续将先检查当前页面，不重复提交",
        )

    def close(self):
        self.driver.quit()


def main():
    worker = None
    try:
        for line in sys.stdin:
            request = json.loads(line)
            if request.get("action") == "close":
                break
            try:
                if worker is None:
                    if request.get("backend", "batchmortal") == "batchmortal":
                        from .batchmortal_worker import BrowserWorker as LocalWorker
                        from .batchmortal_worker import validate_request

                        validate_request(request)

                        worker = LocalWorker(
                            request["profile"],
                            request["model"],
                            request["timeout"],
                            request.get("options"),
                        )
                    else:
                        worker = BrowserWorker(
                            request["profile"], request["model"], request["timeout"]
                        )
                worker.run(request)
            except Exception as exc:
                if worker:
                    try:
                        worker.driver.save_screenshot(str(worker.profile / "last-page.png"))
                    except Exception:
                        pass
                import traceback

                error_path = Path(request["profile"]).resolve().parent / "mortal-online-error.txt"
                error_path.write_text(traceback.format_exc())
                error_path.chmod(0o600)
                emit(
                    status="error",
                    message=f"浏览器操作失败（{type(exc).__name__}），请检查专用 Chrome 窗口",
                )
    finally:
        if worker:
            worker.close()


if __name__ == "__main__":
    main()
