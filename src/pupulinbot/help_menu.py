"""Compact command guide delivered as one OneBot merged-forward message."""

from nonebot.adapters.onebot.v11 import ActionFailed

BATCH_ACTIONS = frozenset(
    (
        "batch",
        "table",
        "links",
        "export",
        "status",
        "report",
        "chart",
        "file",
        "resume",
        "pause",
        "import",
    )
)


def help_sections(*, live_enabled=False, mortal_enabled=False):
    mortal = (
        "已配置分析引擎，实际状态请查看 /mortalstatus。"
        if mortal_enabled
        else ("分析引擎未配置，任务可以保存，但不会自动跑完。")
    )
    return [
        (
            "普普林 Bot · 入门",
            "命令可带 /，也可直接发送；仅英文命令生效，说明和玩家昵称仍可使用中文。\n"
            "/msbind 普普林：绑定雀魂昵称或 account ID\n/msbind friend <好友码>：通过好友码绑定\n"
            "/msunbind：解除自己的绑定\n"
            "/ms4 -i：查看自己的四麻面板\n/ms3 -i：三麻面板\n"
            "/ms2 @群友：查询群友绑定\n"
            "/ms2 @群友 昵称：代绑账号（管理员）；已有绑定不覆盖\n"
            "/help：重新查看本说明\n"
            "省略玩家时使用自己的绑定；参数中的 <…> 不需要输入尖括号。",
        ),
        (
            "查询 · 个人与战绩",
            "/ms4 [玩家]：个人资料\n/ms4 [玩家] -o [-i]：查询当前公开观战\n"
            "/msstats [玩家] [sanma]：统计文字\n"
            "/mslast [玩家] -l 10 [-i]：最近对局\n"
            "/ms [玩家] -l 100：最近 X 场文字摘要（默认最近对局房间）\n"
            "/msrank [sanma]：本群监控名单按段位、PT 排行\n"
            "/msfri <好友码>：好友码转账号\n/mssfri [玩家]：查询好友码",
        ),
        (
            "图表 · 看走势",
            "/mslife [玩家] -l 100：段位积分走势\n"
            "/msdetail [玩家] -l 100：战绩汇总图（含样本牌风）\n"
            "/msstyle [玩家] -l 100：12指标牌风分析图\n"
            "/mscompare [玩家] -l 100：前后半段对比\n"
            "/msvs <对手> -l 200：自己与对手的同桌统计\n"
            "/msheat [玩家]：对局时间热力图\n/msstat [玩家]：对局数量统计\n"
            "/mssession [玩家]：连续对局统计",
        ),
        (
            "筛选 · 查询参数",
            "每次只查一个房间；ms 默认最近对局房间，其他图表和高级查询默认玉南。\n"
            "-l 100：最近 100 场\n"
            "ms 最多 500 场；-l 0 表示查询最多 500 场。\n"
            "mode=3 / mode=4：三麻 / 四麻\n-r 金 / 金东 / 玉 / 玉东 / 王座 / 王座东：房间筛选\n"
            "game=东 / 南：对局长度\nstart=2026-11-01 end=2026-11-08：日期范围\n"
            "-m / -y：本月 / 本年，二选一\n"
            "例：/mslife 普普林 -l 100 -r 玉南\n"
            "兼容 last=100、room=玉南；金/玉/王座默认半庄。\n"
            "这些筛选用于高级查询，msstats 不支持全部参数。",
        ),
        (
            "雀魂 · 本群监控",
            "/mswatch on <昵称或account ID>：添加监控（仅负责人）\n"
            "/mswatch off <昵称或account ID>：移除监控（仅负责人）\n"
            "/mswatch off all：清空本群四麻监控（仅负责人）\n"
            "/mswatch list：查看名单；三麻加 mode=3\n"
            "各群独立，仅播报结算。\n"
            "/msstatus：服务状态",
        ),
        (
            "天凤 · 绑定、查询与监控",
            "/thbind <天凤昵称> / thunbind：绑定 / 解绑\n"
            "/th2 @群友 [昵称]：查询 / 管理员代绑；已有绑定不覆盖\n"
            "/th4 [昵称] -i / th3 [昵称] -i：四麻 / 三麻资料卡\n"
            "/th [昵称] -l 100 -r 凤：最近 X 场摘要，-i 发送图片\n"
            "/thlife [昵称] -l 100：顺位与 R 值走势图\n"
            "/thdetail [昵称] -l 100：战绩汇总图\n"
            "/thlast [昵称] -l 5 [-i]：最近对局；默认一场\n"
            "/thrank [sanma]：本群监控名单按段位、积分排行\n"
            "绑定后省略昵称查本人；三麻可用 mode=3；-l 最多500场。\n"
            "房间 -r 般/上/特/凤（默认南），加东查东风；支持 start/end、-m/-y。\n"
            "详细和/铳统计单列凤凰桌数据，不等同于最近 N 场。\n"
            "/thwatch on <天凤昵称>：添加本群监控（仅负责人）\n"
            "/thwatch off <昵称>：移除监控（仅负责人）；off all 清空\n"
            "/thwatch list / thstatus：本群监控与数据源状态\n"
            "开局使用公开观战列表，约5分钟延迟；仅覆盖公开观战的桌。\n"
            "结算来自 nodocchi，等待收录；新开启不会补发旧战绩。\n"
            "使用昵称，不要提供天凤私密登录 ID；绑定不自动开启监控。",
        ),
        (
            "Mortal · 单场与批量",
            "/mortal <雀魂/天凤链接或一番街ID@座位> [座位0–3]：单场分析\n"
            "天凤默认使用链接tw座位；手动指定优先。支持四麻半庄。\n"
            "/mstable <雀魂/天凤链接或一番街ID@0>：全桌四人分析\n"
            "一番街例：/mstable dboqr0kj4ak3gig8n0tg@0\n"
            "/msmortal [玩家] [-l 1–100]：批跑，默认 10 场，最多 100 场\n"
            "/mortal batch [1–100]：本人批跑，默认 10 场\n"
            "/mortal status <任务编号>：进度\n/mortal report <任务编号>：统计图片\n"
            "/mortal pause <任务编号>：暂停\n/mortal resume <任务编号>：继续\n"
            "/mortal export <任务编号> csv：导出（也支持 xlsx）\n"
            "/mortal links <链接1> <链接2>：多链接任务\n"
            "/mortal import <任务编号> <场次编号> <报告链接>：导入报告\n"
            "/mortal file <任务编号>：HTML 群文件\n/mortalstatus：分析服务状态\n" + mortal,
        ),
        (
            "使用规则 · 数据说明",
            "① 查询来自牌谱屋，主要覆盖金之间及以上；无数据不代表没打牌。\n"
            "② 结算提醒要等牌谱屋收录，有延迟；同群同账号同场去重，发送失败会重试。\n"
            "③ 雀魂监控由负责人维护，各群独立；监控不自动跑 Mortal。\n"
            "④ 普通成员仅管理本人在原群的 Mortal 任务；超级管理员可管理全部任务。\n"
            "⑤ 批跑使用外部分析服务，耗时受排队、验证和限流影响；暂停后已提交的场次可能继续。\n"
            "⑥ 请勿连续刷命令；遇异常先发 /msstatus 或 /mortalstatus。\n",
        ),
    ]


def forward_nodes(bot_id, **options):
    return [
        {
            "type": "node",
            "data": {
                "name": "普普林 Bot · 使用指南",
                "uin": str(bot_id),
                "content": [{"type": "text", "data": {"text": title + "\n\n" + text}}],
            },
        }
        for title, text in help_sections(**options)
    ]


async def send_help(bot, event, **options):
    nodes = forward_nodes(bot.self_id, **options)
    try:
        if event.message_type == "group":
            return await bot.call_api(
                "send_group_forward_msg", group_id=event.group_id, messages=nodes
            )
        return await bot.call_api("send_private_forward_msg", user_id=event.user_id, messages=nodes)
    except ActionFailed:
        # Some OneBot implementations disallow merged messages. Keep help usable.
        text = "合并帮助发送失败，以下为文字版：\n\n" + "\n\n".join(
            title + "\n" + content for title, content in help_sections(**options)
        )
        return await bot.send(event, text)
