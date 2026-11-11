import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment

from pupulinbot.repeat_stop import GROUP_ID, RepeatStop


def group_event(message_id, content, *, group=GROUP_ID, user=42):
    return GroupMessageEvent.model_construct(
        group_id=group,
        message_id=message_id,
        user_id=user,
        self_id=100,
        original_message=Message(content),
        message=Message("adapter-processed text"),
    )


def fake_bot():
    return SimpleNamespace(
        send_group_msg=AsyncMock(return_value={"message_id": 111}),
        get_msg=AsyncMock(
            return_value={
                "group_id": GROUP_ID,
                "message": [{"type": "image", "data": {"file": "stop.png"}}],
            }
        ),
    )


@pytest.mark.asyncio
async def test_four_consecutive_messages_only_in_target_group():
    detector = RepeatStop()
    bot = fake_bot()

    for i in range(4):
        await detector.handle(bot, group_event(i, "复读", group=1))
    for i in range(4, 7):
        await detector.handle(bot, group_event(i, "复读", user=i))
    await detector.handle(bot, group_event(6, "复读"))
    await detector.handle(bot, group_event(7, "本机器人", user=100))
    bot.send_group_msg.assert_not_awaited()
    await detector.handle(bot, group_event(8, "复读"))
    sent = bot.send_group_msg.await_args.kwargs
    assert sent["group_id"] == GROUP_ID
    assert len(sent["message"]) == 1 and sent["message"][0].type == "image"
    payload = sent["message"][0].data["file"]
    assert base64.b64decode(payload.removeprefix("base64://")) == detector.image.read_bytes()

    await detector.handle(bot, group_event(9, "复读"))
    bot.send_group_msg.assert_awaited_once()
    await detector.handle(bot, group_event(10, "复读 "))
    for i in range(11, 15):
        await detector.handle(bot, group_event(i, "复读"))
    assert bot.send_group_msg.await_count == 2


@pytest.mark.asyncio
async def test_same_image_with_per_message_napcat_urls(tmp_path):
    image = tmp_path / "stop.png"
    image.write_bytes(b"image bytes")
    detector = RepeatStop(image)
    bot = fake_bot()
    for i in range(4):
        source = MessageSegment(
            "image",
            {
                "file": "eb667b120ee3d79cc847ad9500235284.jpg",
                "url": f"https://example.invalid/{i}",
            },
        )
        await detector.handle(bot, group_event(i, Message(source)))
    bot.send_group_msg.assert_awaited_once()
