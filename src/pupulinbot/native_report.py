"""Read an already generated author report without waiting for the JS viewer."""

import httpx

from .batch_review import report_metrics, report_url
from .review import ReviewError
from .review_metadata import report_game_result


def compact_report(data):
    metric = report_metrics(data)
    if (
        not data["review"].get("model_tag")
        or type(data.get("player_id")) is not int
        or data["player_id"] not in range(4)
    ):
        raise ReviewError("报告模型或座位缺失")
    review = data["review"]
    compact = {k: data.get(k) for k in ("engine", "game_length", "review_time", "player_id")}
    compact["review"] = {
        k: review.get(k)
        for k in ("model_tag", "rating", "temperature", "total_matches", "total_reviewed")
    }
    kyokus = []
    for kyoku in review.get("kyokus") or []:
        if not isinstance(kyoku, dict):
            continue
        entries = []
        for entry in kyoku.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            actual = entry.get("actual_index")
            details = entry.get("details")
            entries.append(
                {
                    "is_equal": entry.get("is_equal"),
                    "actual_index": actual,
                    "details": [
                        (
                            {"prob": item.get("prob")}
                            if isinstance(item, dict) and i == actual
                            else None
                        )
                        for i, item in enumerate(details)
                    ]
                    if isinstance(details, list)
                    else None,
                }
            )
        kyokus.append({"entries": entries})
    compact["review"]["kyokus"] = kyokus
    game_result = report_game_result(data)
    if game_result:
        compact["game_result"] = game_result
    if report_metrics(compact) != metric:
        raise ReviewError("压缩原始报告改变了指标，拒绝保存")
    return compact


def read_native_report(url, seat, model=None):
    endpoint = report_url(url)
    # Use the working direct IPv4 route instead of waiting on a broken IPv6 TLS path.
    transport = httpx.HTTPTransport(local_address="0.0.0.0")
    with httpx.Client(
        transport=transport, timeout=httpx.Timeout(60, connect=15), follow_redirects=False
    ) as client:
        with client.stream("GET", endpoint) as response:
            response.raise_for_status()
            content = bytearray()
            for part in response.iter_bytes():
                content.extend(part)
                if len(content) > 16 * 1024 * 1024:
                    raise ReviewError("报告超过16MiB")
    import json

    data = json.loads(content)
    if type(data.get("player_id")) is not int or data["player_id"] != seat:
        raise ReviewError("原生报告座位不匹配")
    compact = compact_report(data)
    if model is not None and compact["review"]["model_tag"] != model:
        raise ReviewError("原生报告模型不匹配")
    return compact
