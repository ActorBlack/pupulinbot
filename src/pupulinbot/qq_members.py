"""Read the current group roster through OneBot."""

from nonebot.exception import AdapterException


class GroupMembersError(Exception):
    pass


async def group_member_ids(bot, group_id):
    message = "暂时无法获取本群成员名单，请稍后重试。"
    try:
        members = await bot.get_group_member_list(group_id=group_id, no_cache=True)
        ids = {str(member["user_id"]) for member in members}
    except (AdapterException, TimeoutError, TypeError, KeyError) as exc:
        raise GroupMembersError(message) from exc
    if not isinstance(members, list) or not ids or not all(qq.isdecimal() for qq in ids):
        raise GroupMembersError(message)
    return ids
