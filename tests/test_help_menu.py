from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from nonebot.adapters.onebot.v11 import ActionFailed

from pupulinbot.help_menu import forward_nodes, help_sections, send_help


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["group", "private"])
async def test_help_is_one_forward_with_multiple_readable_nodes(kind):
    bot = SimpleNamespace(self_id="123", call_api=AsyncMock(return_value={"message_id": 9}))
    event = SimpleNamespace(message_type=kind, group_id=456, user_id=789)
    assert await send_help(bot, event) == {"message_id": 9}
    bot.call_api.assert_awaited_once()
    args, kwargs = bot.call_api.call_args
    assert args == (f"send_{kind}_forward_msg",)
    assert kwargs["group_id" if kind == "group" else "user_id"] == (456 if kind == "group" else 789)
    assert len(kwargs["messages"]) == 8
    text = "\n".join(n["data"]["content"][0]["data"]["text"] for n in kwargs["messages"])
    assert "/mslife" in text and "/msbind" in text and "/mswatch on" in text
    assert "自动播报对局结算" in text and "不会自动跑完" in text
    assert "/mslive" in text and "/thlive" in text
    assert all(n["data"]["uin"] == "123" for n in kwargs["messages"])


@pytest.mark.asyncio
async def test_rejected_forward_falls_back_once_to_usable_text():
    bot = SimpleNamespace(
        self_id="123", call_api=AsyncMock(side_effect=ActionFailed(info={})), send=AsyncMock()
    )
    event = SimpleNamespace(message_type="group", group_id=456)
    await send_help(bot, event, live_enabled=True, mortal_enabled=True)
    bot.send.assert_awaited_once()
    text = bot.send.call_args.args[1]
    assert "合并帮助发送失败" in text and "/help" in text and "自动播报对局结算" in text


def test_forward_does_not_embed_external_content_or_fake_history():
    assert len(forward_nodes("123")) == len(help_sections())
    assert all(n["type"] == "node" and "id" not in n["data"] for n in forward_nodes("123"))
