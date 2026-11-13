"""Send a reaction image on the fourth consecutive copy in the configured group."""

import asyncio
import logging
import re
from collections import deque
from pathlib import Path

from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment

from .qq_delivery import send_verified_image

GROUP_ID = 546573626
STOP_IMAGE = Path(__file__).with_name("assets") / "stop_cat.jpg"
IMAGE_MD5 = re.compile(r"([0-9a-f]{32})(?:\.[a-z0-9]+)?", re.IGNORECASE)
logger = logging.getLogger(__name__)


def comparable_message(message: Message) -> Message:
    result = message.copy()
    result.reduce()
    for segment in result:
        if segment.type != "image":
            continue
        # NapCat may attach a different download URL to each copy of one image.
        name = segment.data.get("md5") or segment.data.get("file") or ""
        if isinstance(name, str) and (match := IMAGE_MD5.fullmatch(name)):
            segment.data = {"md5": match[1].lower()}
    return result


class RepeatStop:
    def __init__(self, image: Path = STOP_IMAGE, *, group_id: int = GROUP_ID):
        self.image = image
        self.group_id = group_id
        self.recent_ids: deque[int] = deque(maxlen=64)
        self.previous: Message | None = None
        self.count = 0

    async def handle(self, bot: Bot, event: GroupMessageEvent) -> None:
        if (
            event.group_id != self.group_id
            or event.user_id == event.self_id
            or event.message_id in self.recent_ids
        ):
            return
        self.recent_ids.append(event.message_id)
        message = comparable_message(event.original_message)
        if message == self.previous:
            self.count += 1
        else:
            self.previous = message
            self.count = 1
        if self.count == 4 and message:
            try:
                picture = await asyncio.to_thread(self.image.read_bytes)
                message = Message(MessageSegment.image(picture))
                await send_verified_image(bot, self.group_id, message)
            except Exception:
                logger.exception("发送四连复读停止图失败")
