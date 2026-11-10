from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent
from nonebot.exception import IgnoredException

from pupulinbot.group_policy import check_group_command


def test_monitor_only_group_rejects_non_monitor_commands_for_regular_users():
    restricted = {"706530575"}
    for caller in (123456, 234567):
        event = GroupMessageEvent.model_construct(group_id=706530575, user_id=caller)
        for command in ("help", "mshelp", "ms", "msbind", "mslife", "mortal", "msmortal", "thlast"):
            try:
                check_group_command(
                    event,
                    {"_prefix": {"command": (command,)}},
                    restricted,
                    superusers={"285843647"},
                )
            except IgnoredException:
                pass
            else:
                raise AssertionError(f"Unexpectedly allowed {command} for {caller}")
        for command in ("mswatch", "thwatch", "msstatus", "thstatus"):
            check_group_command(
                event, {"_prefix": {"command": (command,)}}, restricted, superusers={"285843647"}
            )


def test_other_groups_and_private_messages_keep_existing_commands():
    for event in (
        GroupMessageEvent.model_construct(group_id=546573626),
        GroupMessageEvent.model_construct(group_id=914835457),
        GroupMessageEvent.model_construct(group_id=1131357545),
        PrivateMessageEvent.model_construct(),
    ):
        check_group_command(event, {"_prefix": {"command": ("mortal",)}}, {"706530575"})


def test_superuser_has_all_commands_in_every_monitor_only_group():
    for group in (706530575, 163355054, 996147537, 1076805406):
        event = GroupMessageEvent.model_construct(group_id=group, user_id=285843647)
        for command in (
            "help",
            "ms",
            "ms4",
            "mortal",
            "msbind",
            "thlast",
            "mswatch",
            "thwatch",
        ):
            check_group_command(
                event, {"_prefix": {"command": (command,)}}, {str(group)}, superusers={"285843647"}
            )
