from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from nonebot.adapters.onebot.v11 import GroupMessageEvent
from nonebot.exception import IgnoredException

from pupulinbot.db import Database
from pupulinbot.group_policy import check_group_command
from pupulinbot.models import Player
from pupulinbot.ms_watch import WatchCommands
from pupulinbot.tenhou import TenhouError
from pupulinbot.tenhou_commands import manage_watch
from pupulinbot.tenhou_monitor import TenhouStore

GROUP = "493063325"
USER = "1015435450"
DELEGATES = {GROUP: {USER}}


@pytest.mark.parametrize("group,user", [(GROUP, USER), ("123", USER), (GROUP, "456")])
def test_delegate_commands_are_scoped(group, user):
    event = GroupMessageEvent.model_construct(group_id=int(group), user_id=int(user))
    for command in ("mswatch", "thwatch", "thlive", "mslive", "mortal", "extable", "vip2"):
        state = {"_prefix": {"command": (command,)}}
        if group == GROUP and user == USER and command in {"mswatch", "thwatch", "thlive"}:
            check_group_command(event, state, {GROUP, "123"}, monitor_delegates=DELEGATES)
        else:
            with pytest.raises(IgnoredException):
                check_group_command(event, state, {GROUP, "123"}, monitor_delegates=DELEGATES)


async def test_watch_delegate_adds_removes_only_in_assigned_group(tmp_path):
    db = Database(tmp_path / "bot.sqlite")
    await db.initialize()
    store = TenhouStore(db)
    await store.initialize()
    client = SimpleNamespace(
        resolve=AsyncMock(return_value=Player(42, "测试")),
        history=AsyncMock(return_value=[{}]),
    )
    watch = WatchCommands(db, None, manager="owner", clients={4: client}, delegates=DELEGATES)
    await watch.favorites("on 测试", USER, GROUP)
    assert len(await db.subscriptions()) == 1
    for group, user in (("123", USER), (GROUP, "456")):
        with pytest.raises(ValueError, match="负责人"):
            await watch.favorites("off all", user, group)
        with pytest.raises(TenhouError, match="负责人"):
            await manage_watch(store, client, "off all", user, group, delegates=DELEGATES)
    await watch.favorites("off 测试", USER, GROUP)
    assert not await db.subscriptions()
    await manage_watch(
        store, client, "on applePie", USER, GROUP, manager="owner", delegates=DELEGATES
    )
    assert len(await store.subscriptions(GROUP)) == 1
    await manage_watch(store, client, "off applePie", USER, GROUP, delegates=DELEGATES)
    assert not await store.subscriptions(GROUP)
