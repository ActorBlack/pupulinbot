"""Minimal protobuf decoder for public GameLiveHead fields, not authentication."""

from .live import LiveServiceError


def fields(data):
    if len(data) > 4 * 1024 * 1024:
        raise ValueError("oversized response")
    index = 0

    def integer():
        nonlocal index
        value = 0
        for shift in range(0, 70, 7):
            byte = data[index]
            index += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value
        raise ValueError("invalid varint")

    result = {}
    while index < len(data):
        tag = integer()
        field, kind = tag >> 3, tag & 7
        if not field:
            raise ValueError("invalid field")
        if kind == 0:
            value = integer()
        elif kind in (1, 2, 5):
            size = integer() if kind == 2 else 8 if kind == 1 else 4
            if index + size > len(data):
                raise ValueError("truncated data")
            value = bytes(data[index : index + size])
            index += size
        else:
            raise ValueError("unsupported wire type")
        result.setdefault(field, []).append(value)
    return result


def first(data, key, default=0):
    return data.get(key, [default])[0]


def level(data):
    parsed = fields(data)
    return {"id": first(parsed, 1), "score": first(parsed, 2)}


def decode_head(data):
    parsed = fields(data)
    config = fields(first(parsed, 3, b""))
    meta = fields(first(config, 3, b""))
    players = []
    for item in parsed.get(4, []):
        player = fields(item)
        players.append(
            {
                "account_id": first(player, 1),
                "nickname": first(player, 4, b"").decode(),
                "level": level(first(player, 5, b"")),
                "level3": level(first(player, 7, b"")),
            }
        )
    seats = []
    for value in parsed.get(5, []):
        if isinstance(value, int):
            seats.append(value)
        else:
            # Packed repeated uint32 has no field tags; reuse the wire parser
            # by prefixing the uint32 tag before each encoded integer.
            start = 0
            for end, byte in enumerate(value):
                if byte < 128:
                    seats.append(first(fields(b"\x08" + value[start : end + 1]), 1))
                    start = end + 1
            if start != len(value):
                raise ValueError("truncated seats")
    return {
        "uuid": first(parsed, 1, b"").decode(),
        "start_time": first(parsed, 2),
        "game_config": {"meta": {"mode_id": first(meta, 2)}},
        "players": players,
        "seat_list": seats,
    }


def decode_list(wire):
    try:
        wrapper = fields(bytes(wire))
        response = fields(first(wrapper, 2, b""))
        if 1 in response:
            code = first(fields(first(response, 1)), 1)
            if code:
                return {"error": "rpc_rejected", "code": code}
        return {"data": {"live_list": [decode_head(data) for data in response.get(2, [])]}}
    except (ValueError, KeyError, TypeError, IndexError, UnicodeError) as exc:
        raise LiveServiceError("雀魂观战协议响应异常") from exc
