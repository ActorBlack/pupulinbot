"""Restrict selected groups to monitoring commands before handlers can run."""

from nonebot.adapters.onebot.v11 import GroupMessageEvent
from nonebot.consts import PREFIX_KEY
from nonebot.exception import IgnoredException

MONITOR_COMMANDS = frozenset({("mswatch",), ("thwatch",), ("msstatus",), ("thstatus",)})


def check_group_command(event, state, monitor_only_groups, *, superusers=()):
    if not isinstance(event, GroupMessageEvent) or str(event.group_id) not in monitor_only_groups:
        return
    if str(event.user_id) in superusers:
        return
    command = state.get(PREFIX_KEY, {}).get("command")
    if command not in MONITOR_COMMANDS:
        raise IgnoredException("仅监控群只接受监控相关命令")
