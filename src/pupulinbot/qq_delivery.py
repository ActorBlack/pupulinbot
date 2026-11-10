"""Verify image notifications before the monitor records them as delivered."""

import asyncio

from nonebot.adapters.onebot.v11 import Message


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
