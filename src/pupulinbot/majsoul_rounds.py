"""Decode public Mahjong Soul round settlements from Liqi observation records."""

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .ms_options import MODE_NAMES
from .spectator_wire import fields, first


@lru_cache(maxsize=1)
def yaku_names():
    # Simplified Chinese names from the public client's lqc.lqbin fan/fan table.
    path = Path(__file__).with_name("assets") / "majsoul_yaku.json"
    return json.loads(path.read_text(encoding="utf-8"))


def int32s(message, key):
    numbers = []
    for value in message.get(key, []):
        if isinstance(value, int):
            numbers.append(value)
        else:
            # Packed protobuf int32s have no field tags. Negative values are
            # sign-extended varints, including when packed.
            start = 0
            for index, byte in enumerate(value):
                if byte < 128:
                    numbers.append(first(fields(b"\x08" + value[start : index + 1]), 1))
                    start = index + 1
            if start != len(value):
                raise ValueError("truncated packed scores")
    return tuple((n & 0xFFFFFFFF) - (0x100000000 if n & 0x80000000 else 0) for n in numbers)


def wrapper(data):
    value = fields(data)
    return first(value, 1, b"").decode().removeprefix(".lq."), first(value, 2, b"")


def hand_label(tile):
    if len(tile) != 2 or tile[0] not in "0123456789" or tile[1] not in "mpsz":
        return ""
    rank, suit = tile
    if suit == "z":
        return "东南西北白发中"[int(rank) - 1] if rank in "1234567" else ""
    return ("赤5" if rank == "0" else rank) + {"m": "万", "p": "筒", "s": "索"}[suit]


@dataclass(frozen=True, slots=True)
class RoundNotice:
    key: str
    text: str


class MajsoulRoundResults:
    def __init__(self, game):
        self.names = tuple(player["nickname"] for player in game.players)
        self.room = MODE_NAMES.get(game.mode, "雀魂")
        self.round = None
        self.start = ()
        self.current = []
        self.finished = False
        self.ron_source = None
        self.pending_riichi = None

    def _settlement(self, scores, details):
        if self.round is None or len(scores) != len(self.names) or not details:
            return None
        wind, hand, honba = self.round
        totals = [f"{score:,}" for score in scores]
        changes = [f"{score - start:+,}" for score, start in zip(scores, self.start, strict=True)]
        total_width = max(map(len, totals))
        change_width = max(map(len, changes))
        lines = [
            f"{self.room}　{'东南西北'[wind]}{hand + 1}局　{honba}本场",
            "",
            *details,
            "",
            "结算点数 / 本局增减",
        ]
        # Figure spaces match digit width and keep padding visible in chat.
        digit_space = "\u2007"
        lines.extend(
            f"{total.rjust(total_width, digit_space)}　"
            f"{change.rjust(change_width, digit_space)}　{name}"
            for name, total, change in zip(self.names, totals, changes, strict=True)
        )
        self.round = None
        return RoundNotice(f"{wind}:{hand}:{honba}", "\n".join(lines))

    def _hule(self, payload):
        scores = int32s(payload, 5)
        if len(scores) != len(self.names):
            old, delta = int32s(payload, 2), int32s(payload, 3)
            if len(old) == len(delta) == len(self.names):
                scores = tuple(a + b for a, b in zip(old, delta, strict=True))
            else:
                scores = int32s(fields(first(payload, 6, b"")), 1)
        details = []
        for data in payload.get(1, []):
            hule = fields(data)
            seat = first(hule, 4)
            if seat not in range(len(self.names)):
                return None
            self_draw = bool(first(hule, 5))
            how = "自摸" if self_draw else "荣和"
            if (
                not self_draw
                and self.ron_source in range(len(self.names))
                and self.ron_source != seat
            ):
                how += f" {self.names[self.ron_source]}"
            tile = hand_label(first(hule, 3, b"").decode())
            if details:
                details.append("")
            details.append(f"和牌　{self.names[seat]} {how}" + (f"（{tile}）" if tile else ""))
            fans = []
            yakuman = bool(first(hule, 10))
            for fan_data in hule.get(12, []):
                fan = fields(fan_data)
                if not first(fan, 2):
                    continue
                fan_id = first(fan, 3)
                name = yaku_names().get(str(fan_id)) or first(fan, 1, b"").decode().strip()
                name = name or f"役种#{fan_id}"
                fans.append(name)
            count = first(hule, 11)
            fu = first(hule, 13)
            if yakuman:
                value = f"{count}倍役满" if count > 1 else "役满"
            else:
                value = f"{count}番" if count else ""
                if count and fu:
                    value += f"{fu}符"
            # point_sum can differ from the hand's payment total (11,800 vs
            # 5,900 in a three-player tsumo). The score lines show net changes.
            if self_draw:
                non_dealer_payment = first(hule, 17)
                if first(hule, 6):
                    points = (len(self.names) - 1) * non_dealer_payment
                else:
                    dealer_payment = first(hule, 16)
                    points = (
                        dealer_payment + (len(self.names) - 2) * non_dealer_payment
                        if dealer_payment and non_dealer_payment
                        else 0
                    )
            else:
                points = first(hule, 15)
            points = points or first(hule, 20)
            total = " / ".join(part for part in (value, f"{points:,}点" if points else "") if part)
            if total:
                details.append("打点　" + total)
            if fans:
                details.append("役种　" + "、".join(fans))
        return self._settlement(scores, details)

    def _draw(self, payload, name):
        scores = self.current
        if name == "NoTile":
            entries = [fields(entry) for entry in payload.get(3, [])]
            if entries:
                old = int32s(entries[0], 2)
                if old and len(old) != len(scores):
                    return None
                if len(old) == len(scores):
                    scores = old
            for entry in entries:
                delta = int32s(entry, 3)
                if not delta:
                    continue
                if len(delta) != len(scores):
                    return None
                scores = tuple(a + b for a, b in zip(scores, delta, strict=True))
            label = "流局满贯" if first(payload, 1) else "荒牌流局"
        else:
            draw_type = first(payload, 1)
            draw_names = {1: "九种九牌", 3: "四杠散了"}
            if len(self.names) == 4:
                draw_names.update({2: "四风连打", 4: "四家立直"})
            label = draw_names.get(draw_type, "流局")
            end = int32s(fields(first(payload, 2, b"")), 1)
            riichi = first(payload, 5, b"")
            if riichi:
                self._riichi(riichi)
            elif draw_type == 4 and len(self.names) == 4:
                if self.pending_riichi in range(len(self.names)):
                    self.current[self.pending_riichi] -= 1000
                elif not end:
                    return None
            scores = self.current
            if len(end) == len(scores):
                scores = end
        return self._settlement(scores, [label])

    def _riichi(self, data):
        if data:
            liqi = fields(data)
            seat = first(liqi, 1)
            if seat in range(len(self.current)) and not first(liqi, 4):
                self.current[seat] = int32s(liqi, 2)[0] if 2 in liqi else self.current[seat] - 1000

    def feed(self, name, data):
        if name == "NotifyGameEndResult":
            self.finished = True
            return None
        action = name.startswith("Action")
        name = name.removeprefix("Record").removeprefix("Action")
        if name not in {
            "NewRound",
            "DiscardTile",
            "AnGangAddGang",
            "BaBei",
            "DealTile",
            "ChiPengGang",
            "Hule",
            "LiuJu",
            "NoTile",
        }:
            return None
        payload = fields(data)
        if name == "NewRound":
            wind, hand, honba = (first(payload, i) for i in (1, 2, 3))
            scores = int32s(payload, 6 if action else 5)
            if (
                wind not in range(4)
                or hand not in range(len(self.names))
                or len(scores) != len(self.names)
            ):
                raise ValueError("invalid observation round")
            self.round, self.start = (wind, hand, honba), scores
            self.current = list(scores)
            self.ron_source = self.pending_riichi = None
        elif self.round is not None:
            if name == "DiscardTile":
                self.ron_source = first(payload, 1)
                self.pending_riichi = (
                    self.ron_source if first(payload, 3) or first(payload, 9) else None
                )
                return None
            if name in {"AnGangAddGang", "BaBei"}:
                self.ron_source = first(payload, 1)
                self.pending_riichi = None
                return None
            if name in {"DealTile", "ChiPengGang"}:
                self.ron_source = self.pending_riichi = None
                self._riichi(first(payload, 5, b""))
                if name == "ChiPengGang":
                    scores = int32s(payload, 11)
                    if len(scores) == len(self.names):
                        self.current = list(scores)
                return None
            if name == "Hule":
                self.finished = bool(first(payload, 6, b""))
                return self._hule(payload)
            self.finished = bool(first(payload, 4 if name == "NoTile" else 2))
            return self._draw(payload, name)
        return None
