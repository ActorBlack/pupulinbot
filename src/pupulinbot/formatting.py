from zoneinfo import ZoneInfo

from .models import GameRecord, PlayerStats
from .ms_level import delta_text


def format_records(records: list[GameRecord], account_id: int) -> str:
    if not records:
        return "牌谱屋暂未收录该玩家的对局。"
    lines = ["最近牌谱："]
    for game in records:
        player = game.seat(account_id) or {}
        rank = player.get("rank") or "?"
        score = player.get("score", "?")
        time = game.started_at.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M")
        mode = {
            "8": "金东",
            "9": "金南",
            "11": "玉东",
            "12": "玉南",
            "15": "王座东",
            "16": "王座南",
            "21": "三麻金东",
            "22": "三麻金南",
            "23": "三麻玉东",
            "24": "三麻玉南",
            "25": "三麻王东",
            "26": "三麻王南",
        }.get(game.mode, game.mode)
        delta = delta_text(player)
        pt = "" if delta == "—" else "｜" + delta
        lines.append(f"{time}｜{mode}｜{rank}位 {score}点{pt}\n{game.url}")
    return "\n".join(lines)


def finish_message(nickname: str, game: GameRecord, account_id: int) -> str:
    player = game.seat(account_id) or {}
    rank = player.get("rank") or "?"
    score = player.get("score", "?")
    return f"🏁 {nickname} 对局结束：第 {rank} 位，{score} 点\n{game.url}"


def percent(value: float | None) -> str:
    if value is None:
        return "暂无数据"
    # The API has historically returned both fractions and percentages.
    return f"{value * 100 if abs(value) <= 1 else value:.2f}%"


def format_stats(nickname: str, stats: PlayerStats) -> str:
    ranks = [stats.first_rate, stats.second_rate, stats.third_rate]
    if stats.mode == "四麻":
        ranks.append(stats.fourth_rate)
    rank_text = " / ".join(f"{index + 1}位 {percent(value)}" for index, value in enumerate(ranks))
    return (
        f"{nickname}｜{stats.mode}统计（{stats.games} 场）\n"
        f"平均顺位：{stats.average_rank:.2f}\n{rank_text}\n"
        f"和牌 {percent(stats.win_rate)}｜放铳 {percent(stats.deal_in_rate)}\n"
        f"立直 {percent(stats.riichi_rate)}｜副露 {percent(stats.call_rate)}｜"
        f"击飞 {percent(stats.negative_rate)}"
    )
