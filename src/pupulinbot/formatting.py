from .models import GameRecord


def format_records(records: list[GameRecord], account_id: int) -> str:
    if not records:
        return "牌谱屋暂未收录该玩家的对局。"
    lines = ["最近牌谱："]
    for game in records:
        player = game.seat(account_id) or {}
        rank = player.get("grading_score") or player.get("rank") or "?"
        score = player.get("score", "?")
        time = game.started_at.astimezone().strftime("%m-%d %H:%M")
        lines.append(f"{time}｜{game.mode}｜{rank}位 {score}点\n{game.url}")
    return "\n".join(lines)


def finish_message(nickname: str, game: GameRecord, account_id: int) -> str:
    player = game.seat(account_id) or {}
    rank = player.get("grading_score") or player.get("rank") or "?"
    score = player.get("score", "?")
    return f"🏁 {nickname} 对局结束：第 {rank} 位，{score} 点\n{game.url}"
