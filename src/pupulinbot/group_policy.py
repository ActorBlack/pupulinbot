"""Command access for restricted groups, VIP2 members and maintainers."""

from nonebot.adapters.onebot.v11 import GroupMessageEvent
from nonebot.consts import PREFIX_KEY
from nonebot.exception import IgnoredException

from .ms_commands import QUERY_ALIASES, QUERY_COMMANDS
from .tenhou_commands import QUERY_COMMANDS as TENHOU_QUERIES

MONITOR_COMMANDS = frozenset({"mswatch", "thwatch", "msstatus", "thstatus", "thlive", "mslive"})
MORTAL_COMMANDS = frozenset({"mortal", "msreview", "msmortal", "mstable", "mortalstatus"})
PUBLIC_COMMANDS = frozenset(
    {
        "help",
        "mshelp",
        "msbind",
        "msunbind",
        "msstats",
        "msrank",
        *QUERY_COMMANDS,
        *TENHOU_QUERIES,
        *(alias for aliases in QUERY_ALIASES.values() for alias in aliases),
    }
)


def command_name(state):
    command = state.get(PREFIX_KEY, {}).get("command")
    return command[0] if command and len(command) == 1 else None


def monitor_allowed(restricted, vip2, *, maintainer=False):
    return maintainer or not (restricted or vip2)


def check_group_command(
    event,
    state,
    monitor_only_groups,
    *,
    superusers=(),
    monitor_owner=None,
    vip2=False,
    capsolver_users=(),
):
    if not isinstance(event, GroupMessageEvent):
        return
    user = str(event.user_id)
    if user in superusers:
        return
    command = command_name(state)
    if command in {"exmortal", "extable"}:
        if user not in capsolver_users:
            raise IgnoredException("未开通 CapSolver 跑谱权限")
        return
    restricted = str(event.group_id) in monitor_only_groups
    if command == "vip2":
        raise IgnoredException("仅 Bot 超级管理员可管理二级会员")
    if command in MONITOR_COMMANDS:
        if not monitor_allowed(restricted, vip2, maintainer=user == monitor_owner):
            raise IgnoredException("当前账号无监控命令权限")
        return
    if restricted and command not in PUBLIC_COMMANDS and not (vip2 and command in MORTAL_COMMANDS):
        raise IgnoredException("当前群或账号未开放此命令")
