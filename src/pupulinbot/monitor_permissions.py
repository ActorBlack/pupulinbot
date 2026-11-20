"""Group-scoped delegation of monitoring controls."""


def delegated_monitor(group_id, user_id, delegates):
    return str(user_id) in delegates.get(str(group_id), ())
