"""Shared limits for Mortal batch entry points."""

DEFAULT_BATCH_COUNT = 10
MAX_BATCH_COUNT = 100


def batch_count(value=None):
    if value is None:
        return DEFAULT_BATCH_COUNT
    if isinstance(value, str) and value.isdecimal():
        value = int(value)
    if type(value) is not int or not 1 <= value <= MAX_BATCH_COUNT:
        raise ValueError(f"Mortal 场数范围为 1–{MAX_BATCH_COUNT}，默认 {DEFAULT_BATCH_COUNT} 场")
    return value
