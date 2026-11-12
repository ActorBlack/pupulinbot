"""Deliver image messages through OneBot."""

import asyncio
import logging

from nonebot.adapters.onebot.v11 import Message, MessageSegment
from nonebot.exception import AdapterException

logger = logging.getLogger(__name__)


async def send_ranking_images(bot, group_id, pages):
    """Send existing ranking pages as ordinary QQ images in page order."""
    for index, page in enumerate(pages, 1):
        try:
            await send_verified_image(bot, group_id, Message(MessageSegment.image(page)))
        except (AdapterException, RuntimeError, TimeoutError) as exc:
            logger.warning(
                "排行图片未确认：group=%s page=%s/%s error=%s",
                group_id,
                index,
                len(pages),
                type(exc).__name__,
            )
            if index > 1:
                return f"已发送排行前 {index - 1} 页，第 {index} 页未确认送达。"
            return "排行图片未确认送达，请稍后查看。"


async def send_ranking_forward(bot, group_id, title, pages):
    nodes = [
        {
            "type": "node",
            "data": {
                "name": title,
                "uin": str(bot.self_id),
                "content": [
                    {"type": "image", "data": {"file": MessageSegment.image(page).data["file"]}}
                ],
            },
        }
        for page in pages
    ]
    try:
        result = await bot.call_api(
            "send_group_forward_msg", group_id=group_id, messages=nodes, source=title, _timeout=90
        )
    except AdapterException:
        result = None
    if not isinstance(result, dict) or not result.get("message_id"):
        return "排行的合并消息发送失败，请稍后重试。"


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
