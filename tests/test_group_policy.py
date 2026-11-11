import pytest
from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent
from nonebot.exception import IgnoredException

from pupulinbot.group_policy import check_group_command
from pupulinbot.help_menu import help_sections

RESTRICTED = {"706530575"}
ADMIN = 285843647
OWNER = 345678


@pytest.mark.parametrize(
    ("group", "user", "vip2", "mortal", "monitor", "manage"),
    [
        (706530575, 123456, False, False, False, False),
        (706530575, 234567, True, True, False, False),
        (706530575, OWNER, False, False, True, False),
        (706530575, ADMIN, False, True, True, True),
        (546573626, 123456, False, True, True, False),
        (546573626, 234567, True, True, False, False),
    ],
)
def test_group_command_access(group, user, vip2, mortal, monitor, manage):
    event = GroupMessageEvent.model_construct(group_id=group, user_id=user)
    for expected, commands in [
        (
            True,
            (
                "help",
                "mshelp",
                "msbind",
                "msunbind",
                "ms4",
                "msrecent",
                "mswith",
                "thbind",
                "th2",
                "thlife",
                "thrank",
            ),
        ),
        (mortal, ("mortal", "msreview", "msmortal", "mstable", "mortalstatus")),
        (monitor, ("mswatch", "thwatch", "msstatus", "thstatus")),
        (manage, ("vip2",)),
    ]:
        for command in commands:
            state = {"_prefix": {"command": (command,)}}
            options = {"superusers": {str(ADMIN)}, "monitor_owner": str(OWNER), "vip2": vip2}
            if expected:
                check_group_command(event, state, RESTRICTED, **options)
            else:
                with pytest.raises(IgnoredException):
                    check_group_command(event, state, RESTRICTED, **options)


def test_private_help_remains_available():
    check_group_command(
        PrivateMessageEvent.model_construct(), {"_prefix": {"command": ("help",)}}, RESTRICTED
    )


def test_help_hides_commands_unavailable_to_restricted_members():
    ordinary = "\n".join(body for _, body in help_sections(show_monitor=False, show_mortal=False))
    vip2 = "\n".join(body for _, body in help_sections(show_monitor=False))
    assert "/msbind" in ordinary and "/th4" in ordinary
    assert "/mortal" not in ordinary and "/mortal" in vip2
    assert all(
        command not in body
        for body in (ordinary, vip2)
        for command in ("/mswatch", "/msstatus", "/thwatch", "/thstatus", "/vip2")
    )
