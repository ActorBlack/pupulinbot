"""Friend IDs are a reversible number encoding, distinct from paipu perspectives.

Checked against game.maj-soul.com/1/version.json 0.11.252.w on 2026-11-07:
Tools rotates the lower 26 bits five times (17 left to encode, 9 to decode),
preserves upper bits, XORs 6139246 and adds/removes 10000000.
Five 17-bit rotations equal one 7-bit rotation modulo 26.
This conversion does not verify that an account exists.
"""

_MASK = (1 << 26) - 1
_XOR = 6139246
_OFFSET = 10_000_000
_MAX_ACCOUNT = (1 << 31) - 1


def account_to_friend(account_id: int) -> int:
    if type(account_id) is not int or not 1 <= account_id <= _MAX_ACCOUNT:
        raise ValueError("account ID 必须是 1–2147483647 的整数")
    value = account_id ^ _XOR
    low = value & _MASK
    rotated = ((low << 7) | (low >> 19)) & _MASK
    return (value & ~_MASK) + rotated + _OFFSET


def friend_to_account(friend_id: int) -> int:
    if type(friend_id) is not int or not _OFFSET <= friend_id <= _MAX_ACCOUNT + _OFFSET:
        raise ValueError("好友码格式无效；请勿使用 account ID 或牌谱视角 ID")
    value = friend_id - _OFFSET
    low = value & _MASK
    rotated = ((low >> 7) | (low << 19)) & _MASK
    account = ((value & ~_MASK) + rotated) ^ _XOR
    account_to_friend(account)  # Also reject the encoding of account zero.
    return account


def account_to_perspective(account_id: int) -> int:
    if type(account_id) is not int or not 1 <= account_id <= _MAX_ACCOUNT:
        raise ValueError("雀魂 account ID 无效")
    return ((7 * account_id + 1117113) ^ 86216345) + 1358437


def perspective_to_account(perspective_id: int) -> int:
    if type(perspective_id) is not int or not 0 < perspective_id < (1 << 35):
        raise ValueError("雀魂牌谱视角 ID 无效")
    value = ((perspective_id - 1358437) ^ 86216345) - 1117113
    account_id, remainder = divmod(value, 7)
    if remainder or account_to_perspective(account_id) != perspective_id:
        raise ValueError("雀魂牌谱视角 ID 无效")
    return account_id
