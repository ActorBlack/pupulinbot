import pytest

from pupulinbot.batch_limits import batch_count
from pupulinbot.ms_options import parse_options


def test_mortal_default_and_upper_limit():
    assert batch_count() == 10
    assert batch_count("100") == batch_count(100) == 100
    assert batch_count("1") == 1


@pytest.mark.parametrize("value", [0, 101, -1, 1.5, True, "0", "101", "-1", "1.5", "", "all"])
def test_invalid_batch_count_is_rejected(value):
    with pytest.raises(ValueError, match="1–100"):
        batch_count(value)


@pytest.mark.parametrize("text", ["-l 0", "last=0", "-l 101", "last=-1", "last=1000"])
def test_mortal_options_cannot_use_zero_as_an_unlimited_query(text):
    with pytest.raises(ValueError, match="1–100"):
        parse_options(text, max_last=100, allow_all=False)
    assert parse_options("last=0").last == 2000
