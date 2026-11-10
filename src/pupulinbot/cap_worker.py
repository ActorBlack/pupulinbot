"""Private solver subprocess; stdout is credentials, never application logs."""

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default="https://akcap.pikapika.me/14f343ec68/")
    parser.add_argument("--cache-dir", type=Path, default=Path("data/cap"))
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--build", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    if args.build:
        from ._cap.native_hashwx import build_native

        print(build_native())
        return
    from ._cap import cap

    args.cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    args.cache_dir.chmod(0o700)
    key = hashlib.sha256(args.endpoint.rstrip("/").encode()).hexdigest()[:16]
    cap._token_cache_path = lambda endpoint: str(args.cache_dir / f"{key}.json")
    request = json.load(sys.stdin)
    rejected = request.get("rejected")
    token, expires = cap._read_cached_token(args.endpoint)
    if (rejected and rejected == token) or (token and expires <= (time.time() + 30) * 1000):
        cap.clear_cap_token(args.endpoint)
    # One attempt, bounded externally; plain 429 and 403 never trigger mirror hopping.
    token = cap.refresh_cap_token(args.endpoint, workers=args.workers, retries=1)
    _, expires = cap._read_cached_token(args.endpoint)
    print(json.dumps({"token": token, "expires": expires}))


if __name__ == "__main__":
    main()
