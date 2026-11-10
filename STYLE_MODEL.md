# 四麻牌风模型来源

参考页面：https://yurakuurame.github.io/4ma-majang-type-check/

作者仓库：https://github.com/yurakuurame/4ma-majang-type-check

核对日期：2026-11-08（Asia/Shanghai）。HTML SHA-256：`b28ecc72f8c7207a6ad9734f02182201d33f59443c603d5e755ea2636de5d950`。

本项目独立实现公开数学公式及参数，未打包网页 JavaScript、样式或图片。
`ms_style.py` 保存12项均值、标准差和X/Y线性系数；六类标签译为：后手反击型、
先手闪避型、门前打点型、副露速度型、铁壁地藏型、全局参与型。
强度半径阈值依次为1.47、3.2、8.89、12.71，均使用严格大于。

零轴和Y/X分区边界按原网页执行结果保留，标签不解释为能力、段位或建议动作。
三麻不适用；缺失、非有限、负指标及超出0–1的率拒绝分类；不臆测缺失值。

`tests/fixtures/style-reference-oracle.json` 是29组参数及原网页计算结果，
由隔离的JavaScript上下文生成，覆盖六种类型和四位真实玩家数据。
原HTML仅作本地核对材料，不作为产品代码或运行时依赖。

牌谱屋字段含义另外对照SAPikachu/amae-koromo的
`src/data/types/metadata.ts` 与 `src/components/playerDetails/playerDetails.tsx`。
最近N场的时间边界、模式及场数核验与原样本摘要共用，不回填累计统计。

## 自定义补充标签

`ms` 的最近样本汇总另行追加最多 4 个自定义标签。它们不修改上面的原模型，
也不冒充原网站规则。阈值、样本门槛、缺失字段处理及展示顺序见
[STYLE_TAG_RULES.md](STYLE_TAG_RULES.md)，实现为 `ms_tags.py`。
