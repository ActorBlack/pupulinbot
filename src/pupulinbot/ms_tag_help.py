"""Player-facing explanations for the four-player msstyle achievement panel."""

from .ms_candidate_tags import RULES
from .ms_trait_tags import AVERAGE_TRAITS, RATE_TRAITS, TAG_COMBINATIONS, yakuman_tag_groups

TAG_DESCRIPTIONS = {
    "潜龙": "同时具备「门清流」「重炮手」「稳健防守」，门前蓄势、攻守兼备的彩色终极成就。",
    "一鸣惊人": "整场恰好和牌1次并拿到一位，至少2场且占查询总场次至少2%。",
    "得不偿失": "本次查询的平均铳点严格大于平均打点；两项须为有效正数，相等不触发。",
    "无功而返": "多次对局结束后，段位积分恰好没有变化。",
    "吃瓜群众": "完整对局中和牌、放铳、立直次数均为0的场次，至少2场且占查询场次至少2%。",
    "天意弄人": "立直好形率较高，但总体立直后和牌率偏低。",
    "出奇制胜": "立直好形率较低，但总体立直后和牌率偏高。",
    "高开低走": "起手向听较低，但和牌少、和牌巡数偏晚。",
    "逆流而上": "起手向听较高，但和牌多、和牌巡数偏早。",
    "进退失据": "副露和立直后的和牌率都偏低，整体放铳率偏高。",
    "电光石火": "同时具备「闪电战」和「神速立直」。",
    "先声夺人": (
        "同时具备「先发制人」和「神速立直」；立直至少100次，"
        "先制率高于均值至少8个百分点，平均立直巡目早于均值至少0.7巡。"
    ),
    "绝处逢生": "同时具备「零点逃生」和「悬崖勒马」。",
    "福祸相依": "同时具备「悬赏猎人」和「被炸仙人」。",
    "百折不挠": "同时具备「白手起家」和「越挫越勇」。",
    "执两用中": "同时具备「中庸之道」和「毫厘之间」。",
    "如履薄冰": "同时具备「咫尺天涯」和「悬崖勒马」。",
    "炎凉": "同时具备「如履薄冰」和「毫厘之间」，一至四位都曾多次以微小点差分出胜负。",
    # Core play style and recent form.
    "副露流": "副露频率明显高于均值。",
    "门清流": "副露频率明显低于均值，经常保留门前清。",
    "立直流": "立直频率明显高于均值。",
    "默听流": "默听和牌占比偏高，立直频率也没有高于均值。",
    "闪电战": "和牌偏早、和牌率较高，平均打点偏低。",
    "高打点": "和牌时的平均得点较高。",
    "狂战士": "和牌率和放铳率都明显高于均值。",
    "稳健防守": "放铳较少，同时保持一定的和牌率。",
    "局收支重视": "按小局统计的平均净得点较高。",
    "争一流": "一位占比明显高于均值。",
    "避四流": "四位占比偏低，同时保持较好的连对率。",
    "好调中": "近期顺位明显改善，连对多，吃四少。",
    "恶调中": "近期顺位明显下滑，吃四偏多。",
    # Hand-level outcomes and luck.
    "里宝超人": "立直和牌时，比均值更常翻中里宝牌。",
    "里宝绝缘体": "立直和牌时，比均值更少翻中里宝牌。",
    "一发超人": "立直和牌时，比均值更常拿到一发。",
    "一发绝缘体": "立直和牌时，比均值更少拿到一发。",
    "自摸超人": "和牌中自摸的占比偏高。",
    "先发制人": "立直时更常是桌上先宣告的一方。",
    "后发制人": "立直时更常是在别人立直后追立的一方。",
    "副露进攻": "副露后更容易和牌，放铳率也控制得住。",
    "副露防守": "副露后放铳较少，同时保持一定的和牌率。",
    "罚符小偷": "流局时比均值更常处于听牌状态。",
    "全自动放铳": "立直后的放铳率偏高。",
    "全自动防守": "立直后的放铳率偏低。",
    "立直收割机": "立直后较容易和牌。",
    "场供贡献者": "宣告立直后，最终成功和牌的比例偏低。",
    "立直流局王": "立直后较常以流局收场。",
    "好形爱好者": "立直时多面听的占比偏高。",
    "愚形爱好者": "立直时多面听的占比偏低。",
    "神速立直": "立直的平均巡目较早。",
    "白手起家": "平均起手向听数较高，起手离听牌较远。",
    "生于罗马": "平均起手向听数较低，起手离听牌较近。",
    "火力全开": "打点效率较高，计算为和牌率×平均打点。",
    "散财童子": "铳点损失较高，计算为放铳率×平均铳点。",
    "高效打点": "净打点效率较高，计算为和牌率×平均打点−放铳率×平均铳点。",
    "低效打点": "净打点效率较低，计算为和牌率×平均打点−放铳率×平均铳点。",
    # Other statistical achievements.
    "燕返": "立直宣言牌相对容易放铳，宣言之后的放铳率仍控制得住。",
    "立直提款机": "平均每次立直的净收益较高，立直后放铳率没有偏高。",
    "众矢之的": "常先制立直，也经常引来其他玩家追立。",
    "副露拉锯战": "副露多，副露后常打到流局，放铳率没有偏高。",
    "日常踩雷": "整体放铳率没有偏高，但放铳时更容易撞上默听。",
    "无声快刀": "默听和牌占比高，和牌早，平均打点也维持得住。",
    "过山车": "一位和四位都常见，成绩容易走向两端。",
    "中庸之道": "名次常落在中间，二位和三位都不少。",
    "略胜一筹": "至少50场，其中领先二位1～1000点拿下一位至少3场，且占总场次至少3%。",
    "棋差一着": "至少50场，其中落后一位1～1000点拿到二位至少3场，且占总场次至少3%。",
    "签到游戏": "段位积分起伏较大，最后却回到接近起点的位置。",
    "精选立直": "立直频率偏低，但立直时更常是好形。",
    "振听达人": "振听状态下宣告立直的情况较多。",
    "偶失荆州": "平时放铳偏少，但一放铳平均就要付出较多点数。",
    "深度门清": "较少副露，和牌时默听和立直都占有一定比例。",
    "悬赏猎人": "立直和牌时，一发和里宝牌都比均值更常见。",
    "一骑绝尘": (
        "至少50场，一位至少15场且占总场次至少15%；"
        "领先二位至少20000点的一位至少5场，占总场次至少5%，且占一位至少40%。"
    ),
    "越挫越勇": (
        "至少50场，吃四后60分钟内同房间续打至少10场且占总场次至少10%；"
        "其中下一场一位至少4场，占总场次至少4%，且占这类续打至少50%。"
    ),
    "悬崖勒马": "至少50场，其中领先四位1～1000点守住三位至少3场，且占总场次至少3%。",
    "咫尺天涯": "至少50场，其中落后三位1～1000点吃四至少3场，且占总场次至少3%；同点不计。",
    "鏖战": "样本内至少 2 场半庄确认西入，且占所查场数至少 2%。",
    "独裁者": "统计范围内最大连庄达到 6 次。",
    "一击必杀": "立直后和牌率没有偏高，但和牌时平均收入较高。",
    "以小搏大": "放铳较多，但平均每次放铳损失较小。",
    "被炸仙人": "被自摸炸庄的情况多，平均被炸点数也高。",
    "零点逃生": "至少50场，其中终局0～3000点拿到三位至少2场，且占总场次至少2%。",
    "连败之道": "样本中出现过较长的一串连续四位。",
    "连胜之道": "样本中出现过较长的一串连续一位。",
    "顺位集邮": "相邻几场里，常把所有顺位各拿一遍。",
    "马拉松": "至少50场，耗时至少55分钟的对局至少8场且占总场次至少8%，长局连对率至少70%。",
    "空中飞人": ("至少 100 场、被飞至少 12 次，且被飞率达到 12%。终局负分计作被飞，0 点不计。"),
    "体面结局": "至少50场，四位至少10场且占总场次至少10%；四位中终局至少15000点的比例至少35%。",
    # Combinations, special profiles and recorded events.
    "进退自如": "副露后既较容易和牌，又较少放铳。",
    "追立冒险家": "追立占比和整体立直后放铳率都偏高。",
    "重炮手": "同时具备「一击必杀」和「高打点」。",
    "精打细算": (
        "至少 50 场，同时具备「高效打点」和「局收支重视」，净打点效率高于房间基准至少 350 点/局。"
    ),
    "毫厘之间": "同时具备「略胜一筹」和「棋差一着」。",
    "静默猎手": "同时具备「深度门清」和「默听流」。",
    "上分糕手": "同时具备「争一流」和「避四流」。",
    "魔术师": "同时具备「进退自如」「精打细算」「副露流」，副露攻守兼备，整体收益出色。",
    "惊鸿": "同时具备「闪电战」「副露进攻」「副露流」，频繁副露且和牌快，副露后也更容易和牌。",
    "暗杀星": (
        "具备「无声快刀」和「默听流」，和牌至少60次，副露率比房间基准低至少3个百分点，"
        "默听和牌占比至少20%；不要求立直和牌占比达到40%。"
    ),
    "普普林": "和牌、放铳、副露和立直频率符合特定牌风，同时打点和净打点效率较高。",
    "流局满贯": "统计范围内达成过流局满贯。",
    "四暗刻受害者": "统计范围内有牌谱记录显示你坐庄时被对手自摸「四暗刻」。",
}


PENDING_RARITY = {
    "吃瓜群众": "需要完整逐场和牌、放铳及立直统计。",
    "一鸣惊人": "需要逐场和牌次数和最终名次。",
    "鏖战": "需要原谱确认西入。",
}

# Read numerical rules from the classifier so future threshold changes remain visible.
METRICS = {
    "games": "查询对局数",
    "hands": "小局数",
    "wins": "和牌次数",
    "deals": "放铳次数",
    "calls": "副露小局数",
    "riichis": "立直次数",
    "riichi_wins": "立直和牌次数",
    "draws": "流局次数",
    "classified_deals": "已分类放铳次数",
    "firsts": "一位场数",
    "fourths": "四位场数",
    "dealer_streak": "最大连庄次数",
    "first_rate": "一位率",
    "second_rate": "二位率",
    "third_rate": "三位率",
    "fourth_rate": "四位率",
    "middle_rate": "二三位合计比例",
    "riichi_win_share": "立直和牌占全部和牌比例",
    "close_first": "领先二位1～1000点的一位场数",
    "close_second": "落后一位1～1000点的二位场数",
    "close_third": "领先四位1～1000点的三位场数",
    "close_fourth": "落后三位1～1000点的四位场数",
    "zero_pt_games": "终局段位积分变动恰为0的场数",
    "zero_pt_rate": "零PT场次占比",
    "pt_range": "样本内累计PT最大波动",
    "pt_net": "净PT变动",
    "runaway": "领先二位至少20000点的一位场数",
    "runaway_share": "大幅领先场次占一位比例",
    "after_fourth": "吃四后60分钟内同房间续打场数",
    "rebound": "这类续打中一位场数",
    "rebound_rate": "这类续打中一位比例",
    "zero_escape": "终局0～3000点的三位场数",
    "first_streak": "连续一位场数",
    "fourth_streak": "连续四位场数",
    "rank_album": "连续四场集齐四种顺位的窗口占比",
    "long_games": "耗时至少55分钟的对局数",
    "long_top2": "长局连对率",
    "healthy_fourth": "四位中终局至少15000点的比例",
    "west_games": "原谱确认西入的半庄数",
    "west_rate": "西入场次占比",
    "默听率": "默听和牌占比",
    "和了巡数": "平均和牌巡数",
    "立直好型2": "立直好形率",
    "流听率": "流局听牌率",
    "先制率": "先制立直率",
    "追立率": "追立率",
    "被追率": "被追立率",
    "宣言铳率": "立直宣言牌放铳率",
    "默听放铳占比": "放铳中铳给默听的比例",
    "立直多面": "立直多面听占比",
}
RATE_METRICS = {
    "first_rate",
    "second_rate",
    "third_rate",
    "fourth_rate",
    "middle_rate",
    "riichi_win_share",
    "zero_pt_rate",
    "runaway_share",
    "rebound_rate",
    "rank_album",
    "long_top2",
    "healthy_fourth",
    "west_rate",
    "默听放铳占比",
    "立直好型2",
    "立直多面",
    "先制率",
    "追立率",
    "被追率",
    "宣言铳率",
}


def _is_rate(metric):
    return metric in RATE_METRICS or metric.endswith("率") or metric.endswith("_sample_rate")


def _label(metric):
    if metric.endswith("_sample_rate"):
        return METRICS[metric.removesuffix("_sample_rate")] + "占总对局比例"
    return METRICS.get(metric, metric)


def _condition(metric, lower, upper):
    if metric == "铳点高于打点":
        return "平均铳点严格大于平均打点，两项均为有效正数"
    relative = metric.endswith("差")
    base = metric[:-1] if relative else metric
    label = _label(base)
    ratio = _is_rate(base)

    def formatted(value):
        return f"{value * 100:g}%" if ratio else f"{value:g}"

    if relative:
        offset = lower if lower is not None else upper
        side = "高" if offset > 0 else "低"
        amount = f"{abs(offset) * 100:g}个百分点" if ratio else f"{abs(offset):g}"
        if offset == 0:
            return label + ("不低于房间均值" if lower is not None else "不高于房间均值")
        return f"{label}比房间均值{side}至少{amount}"
    if lower is not None and upper is not None:
        return f"{label}为{formatted(lower)}～{formatted(upper)}"
    operator = "≥" if lower is not None else "≤"
    bound = lower if lower is not None else upper
    return f"{label}{operator}{formatted(bound)}"


def _trait_conditions(tag):
    for key, (basis, minimum, high, low, _, high_name, low_name) in RATE_TRAITS.items():
        if tag not in (high_name, low_name):
            continue
        delta = high if tag == high_name else -low
        result = [
            "至少20场、100小局",
            _condition(basis, minimum, None),
            _condition(
                key + "差", delta if tag == high_name else None, None if tag == high_name else delta
            ),
            "通过95%比例区间的均值方向校验",
        ]
        if tag == "副露进攻":
            result.append("副露后放铳率不高于房间均值")
        if tag == "副露防守":
            result.append("副露后和牌率不低于房间均值减3个百分点")
        return result
    for key, (basis, minimum, high, low, _, high_name, low_name) in AVERAGE_TRAITS.items():
        if tag not in (high_name, low_name):
            continue
        delta = high if tag == high_name else -low
        result = [
            "至少20场、100小局",
            _condition(basis, minimum, None),
            _condition(
                key + "差", delta if tag == high_name else None, None if tag == high_name else delta
            ),
        ]
        if key in {"打点效率", "净打点效率"}:
            result.append("和牌至少30次")
        if key in {"铳点损失", "净打点效率"}:
            result.append("放铳至少30次")
        return result
    return []


SPECIAL_CONDITIONS = {
    "先声夺人": [
        "同时满足先发制人、神速立直",
        "至少400小局、立直100次",
        "先制率比均值高至少8个百分点",
        "平均立直巡目比均值早至少0.7巡",
    ],
    "精打细算": [
        "至少50场",
        "同时满足高效打点、局收支重视",
        "净打点效率比房间均值高至少350点/小局",
    ],
    "暗杀星": [
        "满足无声快刀、默听流",
        "和牌至少60次",
        "副露率比均值低至少3个百分点",
        "默听和牌占比至少20%",
        "不要求立直和牌占比达到40%",
    ],
    "普普林": [
        "至少50场",
        "副露率25%～27%",
        "立直率18.5%～20.5%",
        "放铳率9.5%～11.5%",
        "和牌率21.5%～24%",
        "净打点效率≥800",
        "平均打点≥6600",
    ],
    "空中飞人": ["至少100场", "终局素点小于0的对局至少12场，且占样本至少12%", "0点不计被飞"],
    "吃瓜群众": [
        "每场完整对局的和牌、放铳、立直次数均为0",
        "达标场次至少2场，且占查询对局至少2%",
        "由逐场统计核验，缺失记录不充作达标",
    ],
    "一鸣惊人": [
        "整场恰好和牌1次并拿到一位",
        "达标场次至少2场，且占查询对局至少2%",
        "由逐场和牌次数及最终名次核验",
    ],
    "进退自如": ["同时满足副露进攻、副露防守"],
    "追立冒险家": ["同时满足后发制人、全自动放铳", "描述两项总体特征，不是追立后的条件放铳率"],
    "好调中": [
        "至少50场，比较最近20场与此前所有场次",
        "均顺改善至少0.30且不少于1.96倍均值差标准误",
        "最近20场均顺≤房间均顺减0.20、连对至少13场、四位至多3场",
    ],
    "恶调中": [
        "至少50场，比较最近20场与此前所有场次",
        "均顺恶化至少0.30且不少于1.96倍均值差标准误",
        "最近20场均顺≥房间均顺加0.20、四位至少7场",
    ],
    "局收支重视": ["至少20场、300小局", "净素点变化÷小局数达到查询房间的局收支门槛"],
    "副露流": ["至少20场、100小局", "副露率达到查询房间高线，并通过95%区间的均值方向校验"],
    "门清流": ["至少20场、100小局", "副露率不高于查询房间低线，并通过95%区间的均值方向校验"],
    "立直流": ["至少20场、100小局", "立直率达到查询房间高线，并通过95%区间的均值方向校验"],
    "默听流": [
        "至少20场、100小局、和牌30次",
        "默听和牌占比达到查询房间高线，立直率不高于房间均值",
        "默听比例通过95%区间的均值方向校验",
    ],
    "高打点": ["至少20场、100小局、和牌30次", "平均打点达到查询房间高打点门槛"],
    "闪电战": [
        "至少20场、100小局、和牌30次",
        "平均打点不高于房间低打点线，平均和牌巡数不晚于房间速攻线",
        "和牌率达到房间高线并通过95%区间的均值方向校验",
    ],
    "狂战士": ["至少20场、100小局", "和牌率与放铳率均达到房间高线，且均通过95%区间的均值方向校验"],
    "稳健防守": [
        "至少20场、100小局",
        "放铳率不高于房间低线且通过95%区间的均值方向校验",
        "和牌率达到房间要求的下限",
    ],
    "争一流": ["至少50场", "一位率达到房间高线并通过95%区间的均值方向校验"],
    "避四流": [
        "至少50场",
        "四位率不高于房间低线且通过95%区间的均值方向校验",
        "连对率不低于房间均值",
    ],
    "流局满贯": ["本次统计范围内有至少1次有效流局满贯记录"],
}


def tag_conditions(tag):
    if tag in SPECIAL_CONDITIONS:
        return SPECIAL_CONDITIONS[tag]
    if tag in RULES:
        conditions = [_condition(*rule) for rule in RULES[tag]]
        if tag in {"略胜一筹", "棋差一着", "悬崖勒马", "咫尺天涯"}:
            conditions.append("同点不计")
        if tag in {"天意弄人", "出奇制胜"}:
            conditions.append("好形率与立直后和牌率为两项总体统计")
        return conditions
    for *children, combined in TAG_COMBINATIONS:
        if combined == tag:
            return ["同一账号、房间和查询范围内同时满足：" + "＋".join(children)]
    return _trait_conditions(tag)


def tag_help(query: str) -> str:
    tag = query.strip().lstrip("#＃").strip()
    if not tag:
        return "用法：taghelp <成就名称>，例如 taghelp 进退自如。"
    description = TAG_DESCRIPTIONS.get(tag)
    if description is None:
        wins, victims = yakuman_tag_groups()
        if tag in wins:
            description = f"统计范围内有牌谱记录显示你和过「{tag}」。"
        elif tag in victims:
            yaku = "国士无双" if tag == "国士受害者" else tag.removesuffix("受害者")
            description = f"统计范围内有记录显示你放铳给「{yaku}」，或坐庄时被对手自摸该役满。"
        else:
            return "未找到这个成就，请输入 msstyle 卡片上显示的标签名。"
    lines = [f"【{tag}】", description]
    conditions = tag_conditions(tag)
    if conditions:
        lines.append("条件：" + "；".join(conditions) + "。")
    if tag in PENDING_RARITY:
        lines.append("稀有度：待核验，暂以灰色显示，不代表常见。" + PENDING_RARITY[tag])
    return "\n".join(lines)
