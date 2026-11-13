from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from nonebot.adapters.onebot.v11 import ActionFailed, Message, MessageSegment

from pupulinbot.qq_delivery import send_ranking_forward, send_verified_image


@pytest.mark.parametrize("title", ["雀魂群排行", "天凤群排行"])
@pytest.mark.parametrize("pages", [[b"first"], [b"first", b"second"]])
async def test_ranking_sends_all_image_pages_in_one_forward(title, pages):
    bot = SimpleNamespace(self_id="123", call_api=AsyncMock(return_value={"message_id": 9}))
    assert await send_ranking_forward(bot, 456, title, pages) is None
    bot.call_api.assert_awaited_once_with(
        "send_group_forward_msg",
        group_id=456,
        source=title,
        _timeout=90,
        messages=[
            {
                "type": "node",
                "data": {
                    "name": title,
                    "uin": "123",
                    "content": [
                        {"type": "image", "data": {"file": MessageSegment.image(page).data["file"]}}
                    ],
                },
            }
            for page in pages
        ],
    )


@pytest.mark.parametrize("failure", [ActionFailed(retcode=100), TimeoutError()])
async def test_failed_rank_forward_returns_an_error(failure):
    bot = SimpleNamespace(self_id="123", call_api=AsyncMock(side_effect=failure))
    assert await send_ranking_forward(bot, 456, "雀魂群排行", [b"first"]) == (
        "排行的合并消息发送失败，请稍后重试。"
    )
    bot.call_api.assert_awaited_once()


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
