"""Deliver group messages through OneBot."""

import asyncio

from nonebot.adapters.onebot.v11 import Message, MessageSegment
from nonebot.exception import AdapterException


async def send_ranking_forward(bot, group_id, title, pages):
    """Send the ranking pages in order as nodes of a single QQ forward."""
    return await send_group_forward(
        bot, group_id, title, pages, failure_message="排行的合并消息发送失败，请稍后重试。"
    )


async def send_group_forward(
    bot, group_id, title, items, *, failure_message="合并消息发送失败，请稍后重试。"
):
    """Send each text block or image as a node in one QQ forward."""
    nodes = [
        {
            "type": "node",
            "data": {
                "name": title,
                "uin": str(bot.self_id),
                "content": [
                    {"type": "image", "data": {"file": MessageSegment.image(item).data["file"]}}
                    if isinstance(item, bytes)
                    else {"type": "text", "data": {"text": item}}
                ],
            },
        }
        for item in items
    ]
    try:
        result = await bot.call_api(
            "send_group_forward_msg", group_id=group_id, messages=nodes, source=title, _timeout=90
        )
    except (AdapterException, TimeoutError):
        result = None
    if not isinstance(result, dict) or not result.get("message_id"):
        return failure_message


async def send_verified_image(bot, group, message: Message):
    result = await bot.send_group_msg(group_id=int(group), message=message, _timeout=90)
    message_id = result.get("message_id") if isinstance(result, dict) else None
    if not message_id:
        raise RuntimeError("QQ 未返回图片消息 ID，将重试。")
    # NapCat may acknowledge a send whose QQ message is empty. Allow its local
    # message cache to settle, but never equate an acknowledgement with an image.
    for attempt in range(3):
        if attempt:
            await asyncio.sleep(attempt)
        try:
            saved = await bot.get_msg(message_id=message_id, _timeout=4)
            if not isinstance(saved, dict):
                continue
            if str(saved.get("group_id", group)) != str(group):
                continue
            segments = saved.get("message")
            if not isinstance(segments, list):
                continue
            if any(
                isinstance(segment, dict)
                and segment.get("type") == "image"
                and isinstance(segment.get("data"), dict)
                and (segment["data"].get("file") or segment["data"].get("url"))
                for segment in segments
            ):
                return result
        except Exception:
            if attempt == 2:
                raise
    raise RuntimeError(f"QQ 消息 {message_id} 中未找到图片，将重试。")
