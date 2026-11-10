from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from nonebot.adapters.onebot.v11 import Message, MessageSegment

from pupulinbot.qq_delivery import send_verified_image


def bot_with_history(*messages):
    return SimpleNamespace(
        send_group_msg=AsyncMock(return_value={"message_id": 123}),
        get_msg=AsyncMock(side_effect=messages),
    )


@pytest.mark.asyncio
async def test_acknowledged_empty_image_is_not_success_and_does_not_resend_immediately():
    bot = bot_with_history(*[{"message": []}] * 3)
    with patch("pupulinbot.qq_delivery.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(RuntimeError, match="未找到图片"):
            await send_verified_image(bot, "42", Message(MessageSegment.image("base64://aA==")))
    bot.send_group_msg.assert_awaited_once()
    assert bot.get_msg.await_count == 3


@pytest.mark.asyncio
async def test_delayed_cache_and_wrong_group_are_rechecked_without_duplicate_send():
    image = [{"type": "image", "data": {"file": "qq-image.png"}}]
    bot = bot_with_history(
        RuntimeError("cache not ready"),
        {"group_id": 99, "message": image},
        {"group_id": 42, "message": image},
    )
    with patch("pupulinbot.qq_delivery.asyncio.sleep", new=AsyncMock()):
        assert await send_verified_image(bot, "42", Message()) == {"message_id": 123}
    bot.send_group_msg.assert_awaited_once()


@pytest.mark.asyncio
async def test_text_only_or_missing_image_file_is_not_a_delivered_picture():
    bot = bot_with_history(
        {"message": [{"type": "text", "data": {"text": "对局开始"}}]},
        {"message": [{"type": "image", "data": {}}]},
        {"message": []},
    )
    with patch("pupulinbot.qq_delivery.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(RuntimeError, match="未找到图片"):
            await send_verified_image(bot, "42", Message())


@pytest.mark.asyncio
async def test_missing_message_id_is_failure():
    bot = bot_with_history()
    bot.send_group_msg.return_value = None
    with pytest.raises(RuntimeError, match="消息 ID"):
        await send_verified_image(bot, "42", Message())
    bot.get_msg.assert_not_awaited()
