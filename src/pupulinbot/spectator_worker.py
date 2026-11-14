"""Read-only spectator RPCs through a manually authenticated official web client."""

import json
import sys
import time
from pathlib import Path

PREFIX = "PUPULIN_SPECTATOR "
URL = "https://game.maj-soul.com/1/"
RPC = r"""
const filter = arguments[0], done = arguments[1];
if (location.origin !== 'https://game.maj-soul.com') {
    done({error: 'unexpected_page'}); return;
}
if (!window.__pupulinSpectator) { done({error: 'not_ready'}); return; }
window.__pupulinSpectator.list(filter).then(done).catch(() => done({error: 'browser_error'}));
"""


def emit(value):
    print(PREFIX + json.dumps(value, ensure_ascii=False), flush=True)


def main():
    from seleniumbase import Driver

    from .majsoul_replay import LOBBY_RENDER_ON_READY
    from .spectator_wire import decode_list

    profile = Path(sys.argv[1]).resolve()
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    profile.chmod(0o700)
    driver = None
    records_only = "--records-only" in sys.argv
    chromium_args = ["remote-debugging-port=0"]
    if sys.platform.startswith("linux"):
        chromium_args.extend(("use-gl=angle", "use-angle=swiftshader", "enable-unsafe-swiftshader"))
    try:
        driver = Driver(
            browser="chrome",
            enable_3d_apis=True,
            disable_gpu=False,
            headed=True,
            uc=False,
            enable_ws=True,
            page_load_strategy="eager",
            user_data_dir=str(profile),
            chromium_arg=",".join(chromium_args),
        )
        driver.set_page_load_timeout(40)
        driver.set_script_timeout(10)
        if not records_only:
            driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {
                    "source": Path(__file__).with_name("spectator_bridge.js").read_text()
                    + LOBBY_RENDER_ON_READY
                },
            )
            # Old Unity service-worker caches can stall navigation after upgrades.
            driver.execute_cdp_cmd("Network.enable", {})
            driver.execute_cdp_cmd("Network.setBypassServiceWorker", {"bypass": True})
            driver.get(URL)
        emit({"status": "opened"})
        last_request = 0.0
        for line in sys.stdin:
            try:
                request = json.loads(line)
                heads = []
                filters = request["filters"]
                if not filters or not set(filters) <= {
                    208,
                    209,
                    211,
                    212,
                    215,
                    216,
                    221,
                    222,
                    223,
                    224,
                    225,
                    226,
                }:
                    raise ValueError("invalid filters")
                deadline = time.monotonic() + 40
                for mode in filters:
                    if time.monotonic() > deadline:
                        emit({"error": "rpc_timeout"})
                        break
                    time.sleep(max(0, 1.05 - (time.monotonic() - last_request)))
                    last_request = time.monotonic()
                    response = driver.execute_async_script(RPC, mode)
                    if "wire" in response:
                        response = decode_list(response["wire"])
                    if response.get("error"):
                        emit(response)
                        break
                    heads.extend(response["data"]["live_list"])
                else:
                    emit({"heads": heads})
            except Exception:
                # Do not put browser/session contents or exception details in logs.
                emit({"error": "browser_error"})
    except Exception:
        emit({"error": "browser_start_failed"})
    finally:
        if driver is not None:
            driver.quit()


if __name__ == "__main__":
    main()
