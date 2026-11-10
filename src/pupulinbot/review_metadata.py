"""Recover final scores from the reviewed game, independently of nickname indexes."""


def score_vector(value):
    return (
        isinstance(value, list)
        and len(value) == 4
        and all(type(v) is int and abs(v) <= 10_000_000 for v in value)
    )


def report_game_result(data):
    events = data.get("mjai_log")
    if not isinstance(events, list) or not events:
        saved = data.get("game_result")
        if not isinstance(saved, dict) or not score_vector(saved.get("scores")):
            return None
        scores = saved["scores"]
    else:
        if (
            not all(isinstance(e, dict) for e in events)
            or events[0].get("type") != "start_game"
            or events[-1].get("type") != "end_game"
        ):
            return None
        start = next(
            (i for i in range(len(events) - 1, -1, -1) if events[i].get("type") == "start_kyoku"),
            None,
        )
        if start is None or not score_vector(events[start].get("scores")):
            return None
        scores = list(events[start]["scores"])
        reached = set()
        settled = ended = False
        for event in events[start + 1 : -1]:
            kind = event.get("type")
            if kind == "reach_accepted":
                actor = event.get("actor")
                if type(actor) is not int or actor not in range(4) or actor in reached:
                    return None
                reached.add(actor)
                scores[actor] -= 1000
            elif kind in {"hora", "ryukyoku"}:
                delta = event.get("deltas")
                if not score_vector(delta):
                    return None
                scores = [s + d for s, d in zip(scores, delta, strict=True)]
                settled = True
            elif kind == "end_kyoku":
                ended = True
        if not settled or not ended or not score_vector(scores):
            return None
    order = sorted(range(4), key=lambda i: (-scores[i], i))
    return {"scores": scores, "ranks": [order.index(i) + 1 for i in range(4)]}


def apply_report_result(row, data):
    if row.get("rank") is not None or data.get("player_id") != row["seat"]:
        return
    result = report_game_result(data)
    if result:
        row.update(rank=result["ranks"][row["seat"]], rank_source="mortal_mjai")
