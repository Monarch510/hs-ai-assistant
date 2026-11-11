"""12 类卡牌功能标签的规则钉行为（CONTEXT §6 必测例子）。

只用真实卡牌库按中文名取牌；不联网、不依赖 data/card_tags.json。
"""

from __future__ import annotations

import unittest

from hsai import cards as cards_mod
from hsai.cards import Card
from hsai import tag_rules


def _tags(card: Card) -> tuple[str, ...]:
    return tuple(tag for tag in tag_rules.TAG_ORDER if tag_rules.RULES[tag](card))


# CONTEXT §6 的必测例子：期望值以该节为准
REQUIRED = {
    "火球术": ("单解", "斩杀"),
    "寒冰箭": ("单解", "减益", "斩杀"),   # 「对一个角色」能打到敌方英雄脸
    "冰霜新星": ("减益",),
    "神圣新星": ("群解", "治疗"),
    "暴风雪": ("群解", "减益"),
    "绝命乱斗": ("群解",),
    "妖术": ("单解",),
    "闷棍": ("单解",),
    "治疗波": ("治疗",),
    "盾牌格挡": ("护甲", "抽牌"),
    "真言术：盾": ("增益", "抽牌"),
    "奥术智慧": ("抽牌",),
    "不稳定的传送门": ("资源",),
    "淡水鳄": ("站场",),                  # 2/3 白板随从
    "狼骑兵": ("站场", "斩杀"),
    "军情七处特工": ("单解", "站场"),
    "裂盾一击": ("群解",),                # 不是治疗/护甲
    "加尔范上尉": ("站场", "增益"),        # 只是提到护甲，不是治疗
}

# 反例：这些牌不该被打上括号里的标签（比整组期望值更细，防止规则写太宽）
NEGATIVE = [
    ("寒冰箭", "治疗"),
    ("冰霜新星", "群解"),          # 只冻结，不处理随从
    ("暴风雪", "斩杀"),            # 只打敌方随从，不能打脸
    ("神圣新星", "增益"),
    ("妖术", "减益"),
    ("闷棍", "减益"),
    ("真言术：盾", "治疗"),        # 「获得+2生命值」是增益不是治疗
    ("裂盾一击", "治疗"),
    ("裂盾一击", "护甲"),          # 只是按护甲减费
    ("加尔范上尉", "治疗"),
    ("加尔范上尉", "护甲"),
    ("加尔范上尉", "斩杀"),        # 冲锋是条件给的，不能白送
    ("军情七处特工", "斩杀"),      # 随从战吼直伤不算终结手段
    ("淡水鳄", "增益"),
    ("灵魂炸弹", "斩杀"),          # 混合目标：还有一半伤害打自己英雄
    ("奥金尼灵魂祭司", "治疗"),    # 治疗牌改成伤害，本身不治疗
    ("耐普图隆，潮汐之王", "增益"),  # 「属性值变为5/5」不是「获得…属性值」
    ("棱彩透镜", "未知"),            # 交换的是法力值消耗，动作确定
    ("加尔范上尉", "未知"),          # 条件牌：动作确定，只是看历史
    ("子弹上膛", "未知"),            # 「每当你施放一个法术」是触发条件，不是引用句
    ("冰雪亡魂", "未知"),            # 同上：条件是施放，本牌只加自己身材
    ("EX1_091", "增益"),             # 夺控不是偷身材
    ("EX1_091", "减益"),
    ("EDR_810", "增益"),             # 「偷取的生命值增加1点」是描述水蛭，不是本牌偷
    ("EDR_810", "减益"),
]

# 审计回归：多段飞弹 / 每个英雄 / 区间伤害 / 混合目标 / 治疗转换
REGRESSION = {
    "强能奥术飞弹": ("单解", "群解", "斩杀"),      # 每枚飞弹造成伤害
    "哥利亚，斯尼德的杰作": ("单解", "群解", "站场"),
    "暗影投弹手": ("站场", "斩杀"),                # 对每个英雄造成伤害 → 能打脸
    "连环爆裂": ("单解", "斩杀"),                  # 造成3到6点伤害
    "灵魂炸弹": ("单解",),
    "奥金尼灵魂祭司": ("站场",),
}

# 已落规则族的回归：这些牌曾留在待定清单，规则认下后必须退出待定。
# 只收「纯同构」的族；带命中面计数 / 精确文案 / 同族反例的族单独留方法，
# 别塞进表里，否则失败时看不出是哪条判据松了。
RESOLVED_FAMILIES = (
    ("光环·拥有", ('AV_661', 'WW_341'), ('增益',)),
    ("后续伤害增量", ('BAR_546', 'KARA_00_06', 'TOY_801b'), ('增益',)),
    ("牌库摧毁", ('BAR_913', 'TRL_246'), ('减益',)),
    ("解锁目标", ('BCON_001', 'SW_322'), ('增益',)),
    ("技能改写", ('BT_429', 'CORE_EX1_625', 'TLC_632'), ('单解', '斩杀')),
    ("英雄免伤", ('BT_752', 'TRL_302'), ('保护',)),
    ("削对方法力", ('BT_753', 'PROLOGUE_MANABURN'), ('减益',)),
    ("法术重复", ('CORE_DMF_058', 'DMF_058', 'TIME_211B'), ('增益',)),
    ("移交友方随从", ('CORE_ICC_206', 'ICC_206'), ('减益',)),
    ("投掷武器", ('CORE_ICC_233', 'ICC_233'), ('单解',)),
)


# 二轮审计 S1-S4：装备武器=站场 / 护甲变体 / 分配伤害 / 单卡边界
AUDIT_S1_S4 = {
    "BT_011": ("站场", "减益"),                    # S1 正义圣契：装备一把武器
    "天灾领主加尔鲁什": ("群解", "站场"),
    "光铸凯瑞尔": ("群解", "站场", "斩杀"),
    "阳炎冶锻": ("站场", "增益"),
    "EX1_409": ("站场", "增益"),                   # S1 升级（按 id，另有重名附魔）
    "武器计划": ("站场", "护甲"),
    "勇气战将洛卡拉": ("站场",),
    "加拉克苏斯大王": ("站场",),
    "黑锋骑士乌瑟尔": ("站场",),
    "疯狂之灾祸": ("站场",),
    "兽人铸甲师": ("站场", "护甲"),                # S2 获得等量的护甲值
    "凶恶的废钢猎犬": ("站场", "护甲"),
    "米诺陶牛头人": ("站场", "护甲"),
    "高阶祭司塞卡尔": ("站场", "护甲"),            # S2 转化为护甲值
    "石头护手": ("站场", "护甲", "斩杀"),
    "禁忌之果": ("护甲",),                        # S2 获得双倍的护甲值
    "拒绝诱惑": ("护甲",),
    "古夫的旅行": ("增益", "护甲"),                # S2 获得+5攻击力和护甲值
    "深海低语": ("群解", "减益"),                  # S3 分配目标在「敌方随从」
    "艾森娜": ("群解", "站场", "斩杀"),            # S3 分配目标在「所有敌人」
    "治疗石": ("治疗",),                          # S4 恢复受到的所有伤害
    "禁忌咒文": ("单解",),                        # S4 消灭宾语超长
    "平心静气": ("单解", "群解", "减益"),          # S4 条件群杀
    "灯光表演": ("单解", "斩杀"),                  # S4 「发射…造成伤害的灯光」
    "米米尔隆的头部": ("站场",),                   # S4 消灭的是自己的机械
    "虚空恐魔": ("站场",),                        # S4 消灭两侧相邻（多为己方）
}


# 交互审计（2026-11-07）：逐条确认「没有规则」的牌。规则改动要有正反例。
AUDIT_PENDING = {
    "漂浮教程": ("增益",),                          # 「获得等同于…属性值」= 加身材
    "憎恶军官": ("站场", "增益"),                  # 「吞食并获得其属性值」
    "不死徒刑": ("未知",),                          # 触发亡语：动作看局面
    "破碎之力": ("未知",),                          # 2 回合后延迟触发
    "审判": ("未知",),                              # 按选中随从复制身材
    "克苏恩仪式": ("未知",),                        # 2 回合后选择并触发
    "魔导师晨拥": ("未知",),                        # 重放本局用过的法术
    "暗言术：窃": ("单解", "资源"),                 # 敌方随从移回你的手牌：抢场+抢牌
    "战术撤离": (),                                 # 奥秘：友方随从回手，不算解场
    "AV_258": ("未知",),                            # 引用句：唤起两种元素之力
    "CATA_190h": ("未知",),                         # 引用句：选择并释放…项灾变
    "CORE_CATA_011": (),                            # 「尚未导入」预览说明，不是引用句
    "AV_283": ("未知",),                            # 引用句：对敌人施放…法术
    "FIR_959": ("站场", "未知"),                    # 引用句可与站场共存
    "TTN_842": ("站场", "资源", "未知"),            # 引用句可与资源共存
    "AV_324": ("增益", "减益"),                     # 偷取身材：两端都算，不看归属
    "RLK_655": ("增益", "减益"),                    # 友方亡灵各偷取：含己方从句也算
    "VAC_931": ("增益", "减益"),                    # 英雄偷取攻击力
    "SCH_354": ("站场", "增益", "减益"),            # 从所有敌方随从处偷取
    "CATA_699": ("站场", "增益", "减益"),           # 偷取敌方随从 3 点生命值
    "TLC_831": ("站场", "增益", "减益"),            # 召唤物偷身材，按现有规则归本牌
    # #4 光环式「拥有+N」= 增益；本随从自身条件不算
    "AV_661": ("增益",),                            # 场地光环：你的随从拥有+1攻击力
    "WW_341": ("增益",),                            # 「你最左边的随从」同义写法
    "CS2_122": ("站场", "增益"),                    # 团队领袖：光环挂在随从牌上
    "CORE_REV_921": ("站场", "增益"),               # 「剩余时间内，你的图腾拥有+2」
    "SCH_519": ("站场", "增益"),                    # 你的武器拥有+2攻击力
    "ULDA_208": ("增益",),                          # 被动：你的所有随从拥有+1生命值
    "TTN_713": ("站场",),                           # 反例：「在你的回合拥有+4」是本随从
    "UNG_928": ("站场",),                           # 反例：「在你对手的回合拥有+2」同上
    "JAIL_202": ("站场", "增益"),                    # 「你的英雄在你的回合拥有+1」有受益对象
    # #5 D：带数值的「后续伤害 +N」整族统一算增益
    "BAR_546": ("增益",),                            # 英雄技能伤害增加1点
    "AT_003": ("站场", "增益"),                      # 英雄技能额外造成1点伤害
    "TRL_390": ("站场", "增益"),                     # 「下一个英雄技能」同义写法
    "SW_321": ("单解", "增益", "斩杀"),              # 直伤牌附带的技能增强
    "AT_093": ("站场", "增益"),                      # 卡面纯属性行「法术伤害+1」也算（显式例外）
    "KARA_00_06": ("增益",),                         # 本回合拥有法术伤害+5
    "BAR_079t14b": ("增益",),                        # 衍生牌「法术伤害+2」
    "TOY_801b": ("增益",),                           # 获得法术伤害+1
    "TSC_923": ("增益",),                            # 使你的随从获得法术伤害+1
    "BRMA14_3": ("站场", "增益"),                    # 「双方玩家」也按动作方向算
    "YOG_082": ("单解", "增益", "斩杀"),             # 下一个法术拥有法术伤害+2
    "JAIL_EVENT_101": ("增益",),                     # 伤害提高1点
    "TSC_219": ("站场", "增益"),                     # 蔓足造成的伤害提高1点
    # #14 收编：输出型伤害/治疗翻倍 = 增益（推翻 #5「倍数不是增量」）
    "EX1_350": ("站场", "增益"),                     # 先知维伦：伤害和治疗效果翻倍
    "GIL_646": ("站场", "增益"),                     # 发条自动机：英雄技能伤害/治疗翻倍
    # 替换技能在 #9 改判单解+斩杀（仍不算增益）
    # #9：英雄技能改写为伤害 = 单解 + 斩杀（不算增益）
    "CORE_EX1_625": ("单解", "斩杀"),                # 暗影形态：变为「造成2点伤害」
    "BT_429": ("单解", "斩杀"),                      # 恶魔变形：替换为「造成5点伤害」
    "CORE_BT_429": ("单解", "斩杀"),                 # 恶魔变形（经典）
    "EX1_625": ("单解", "斩杀"),                     # 暗影形态（专家1）
    "VAN_EX1_625": ("单解", "斩杀"),                 # 暗影形态：两档伤害，首个匹配即判
    "TB_HunterPrince_03": ("单解", "斩杀"),          # 恶魔变形（乱斗版）
    "TLC_632": ("单解", "斩杀"),                     # 萨弗拉斯的故事：随机对一个敌人
    "AT_050": ("单解", "站场", "斩杀"),              # 灌魔之锤：WEAPON 补单解
    "WON_321": ("单解", "站场", "斩杀"),             # 灌魔之锤（另一版本）
    "BRMC_94": ("单解", "站场", "斩杀"),             # 萨弗拉斯：随机伤害 + 武器
    # 反例：非伤害改写 / 触发句不被收编
    "OG_006": ("站场",),                            # 恶鳍审判者：改写为召唤鱼人
    "UNG_917": ("增益",),                           # 恐龙学：改写为使野兽获得+3/+3
    "LOE_021": (),                                  # 毒镖陷阱：奥秘触发句，仍待定
    # #6 C：摧毁/移除牌库（含「双方」「你的」）算减益
    "BAR_913": ("减益",),                            # 摧毁双方牌库顶的三张牌
    "TRL_246": ("减益",),                            # 摧毁双方牌库中一半的牌
    "CATA_720": ("站场", "减益"),                    # 摧毁双方玩家牌库中低费牌
    "CORE_ICC_407": ("站场", "减益"),                # 移除你对手的牌库顶一张
    "CORE_ICC_911": ("站场", "减益"),                # 移除「你的」牌库顶三张：削弱己方也算
    "CORE_MAW_034": ("站场", "减益"),                # 典狱长：摧毁你的牌库
    "DMF_118": ("站场", "减益"),                     # 提克特斯：腐蚀改对手牌库
    "GVG_016": ("站场", "减益"),                     # 魔能机甲：对手出牌便削自己牌库
    "TOY_529": ("减益", "斩杀", "未知"),             # 死亡轮盘：减益可与斩杀/未知共存
    # 反例：缺「的」的附带代价句不收；非牌库目标不受影响
    "BAR_911": ("单解", "群解"),                     # 「摧毁你牌库中的一张牌」C 故意留着
    "ULDA_030": (),                                  # 「移除你牌库顶的两张牌」同上
    "CORE_EX1_066": ("站场", "减益"),                # 摧毁武器：由旧判据命中，标签不变
    "END_035": ("站场", "减益"),                     # 已有减益，不受新判据影响
    "BOM_05_MemoryBurn_08s": ("单解", "减益"),       # 同上
    # #7 B：英雄技能解锁随从目标 = 增益（推翻 #5 把「改目标」列为不收的边界）
    "BCON_001": ("增益",),                           # 致命狙击：剩余时间内解锁目标
    "SW_322": ("增益",),                            # 保卫矮人区：任务线奖励句（clauses 会整句过滤）
    "DRG_253": ("站场", "增益"),                    # 矮人神射手：光环挂在随从牌上
    "GVG_087": ("站场", "增益"),                    # 热砂港狙击手
    # 反例：相邻表达不被本判据收编
    "YOG_522": (),                                  # 「还会以相邻随从为目标」不是解锁
    "RLK_218": ("站场", "增益"),                    # 「不能以英雄为目标」是限制（增益来自法术伤害+2）
    "TRL_319": ("站场",),                           # 「会以选中随从及相邻随从作为目标」字面不同
    "CORE_ICC_068": ("站场", "减益"),               # 有明确动作词「冻结」，标签不变
    # #10 B2 原判「增益」，#21 B1 起整族迁入第 12 类「保护」（迁移、互斥）
    "BT_752": ("保护",),                             # 疾影：全库唯一「无法受到伤害」
    "TRL_302": ("保护",),                            # 暂避锋芒：直到下个回合你的英雄免疫
    "CORE_ICC_850": ("站场", "斩杀", "保护"),         # 暗影之刃：战吼免伤 + 武器能打脸
    "KAR_712": ("站场", "保护"),                     # 紫罗兰幻术师：在你的回合中免伤
    "ULDA_114": ("站场", "保护"),                    # 死亡护盾：武器光环
    "TIME_021": ("站场", "保护"),                    # 末日准备狂：流放触发的免伤
    "GDB_843": ("站场", "资源", "斩杀", "保护"),      # 视差光枪：法术迸发免伤
    "CORE_GVG_021": ("站场", "增益", "保护"),         # 玛尔加尼斯：+2/+2 仍给增益，免伤转保护
    # 反例：受益对象不是「你的英雄」/ 被条件隔开 / 奥秘触发句，都不收
    "AT_057": ("站场",),                             # 「使一个友方野兽获得免疫」
    "CORE_ICC_467": ("站场",),                       # 「使一个友方随从获得免疫」
    "CORE_MAW_034": ("站场", "减益"),                 # 「本随从获得免疫」
    "LOOT_222": ("站场", "斩杀"),                    # 「你的英雄在攻击时免疫」被条件隔开
    "BAR_321": ("增益",),                            # 同上，增益来自 +1 攻击力
    "EX1_295": (),                                   # 寒冰屏障：奥秘「使其…免疫」
    "VAN_EX1_295": (),
    "LOOT_214": (),                                  # 闪避：「在受到伤害后…免疫」
    # #11 C：减少/摧毁/移除对手法力水晶 = 减益（两种语序统一显式收编）
    "BT_753": ("减益",),                             # 法力燃烧：对手减少两个
    "PROLOGUE_MANABURN": ("减益",),                  # 序章版同文案
    "LOOTA_811": ("减益",),                          # 先例：原本靠「摧毁…对手」通用判据，标签不变
    "LOOTA_BOSS_26P7": ("减益",),
    "STORY_10_LORDOFOUTLAND": ("增益", "减益", "抽牌"),
    "TB_DIABLO4_PROMO_CARD5": ("站场", "减益", "斩杀"),
    # 反例：己方水晶是代价，C 只认对手/敌方
    "EX1_301": ("站场",),                            # 恶魔卫士：摧毁你的一个法力水晶
    "TRL_500": ("增益",),                            # 疯入膏肓：代价换牌库随从 +2/+2
    "DEEP_027": ("站场",),                           # 暗石守卫：抉择代价（本体不受影响）
    "DEEP_027A": ("减益",),                          # #25 B1：碎裂形态「弃两张牌」整句 → 减益
    "DEEP_027B": ("减益",),                          # #25 B1：法力裂解「摧毁你的一个法力水晶」整句 → 减益
    "EX1_308": ("单解", "斩杀"),                     # 灵魂之火：伤害+随机弃一张，不补减益
    "LOOT_417": ("群解",),                           # 大灾变：消灭随从+弃两张牌，不补减益
    "DMF_115": ("站场",),                            # 怨灵捣蛋鬼：每个玩家各一个
    "DMF_534": ("未知",),                            # #26 B2：混乱套牌，费用/攻击力互换双向方向不明 → 未知
    "AT_038": ("站场", "资源"),                      # 豹骑士：亡语失去一个，不按减益
    "EDR_483": ("未知",),                            # 破碎之力：延迟跳费按 #3 判未知，不受影响
    # #12 B2：「你的英雄拥有+关键词」= 增益（受益对象明确，不碰 HERO）
    "CATA_530": ("增益",),                           # 邪能灌魔：恶魔猎手法术，0 标签转增益
    "ETC_398": ("站场", "增益"),                     # 暗影之眼：英雄拥有吸血
    "CATA_151": ("站场", "增益"),                    # 艾萨拉，海洋之主：英雄拥有风怒
    # 反例：非英雄对象的「拥有+关键词」不收（B3 的 64 张），尤其 HERO 按 #8 跳过
    "BOT_238": (),                                   # #8 明确跳过的 HERO：你的所有机械拥有突袭
    "CORE_ICC_833": ("站场",),                       # HERO：你的所有元素拥有吸血
    "BOT_543": ("站场",),                            # 你的所有法术拥有吸血
    "CATA_613": ("站场",),                           # 「则拥有免疫」是本随从
    "BE_230": ("站场",),                             # 你的最左边的随从拥有突袭
    "CATA_898": ("站场",),                           # 所有敌方随从拥有嘲讽
    "TRL_302": ("保护",),                            # #10 已收的免疫，#21 起归保护
    "BT_752": ("保护",),                             # #10 已收的免伤，#21 起归保护
    # #14 C：后续法术施放两次 / 输出型伤害治疗翻倍 = 增益
    "CORE_DMF_058": ("增益",),                       # 日蚀：0 标签转增益
    "DMF_058": ("增益",),                            # 日蚀（原版）
    "TIME_211B": ("增益",),                          # 强化永恒之井：时间线牌，0 标签转增益
    "BOT_411": ("站场", "增益"),                    # 伊莱克特拉·风潮
    "TSC_071": ("站场", "增益"),                    # 双弓积骇纳迦
    "BE_337": ("站场", "增益"),                     # 地母塞拉赞恩
    "CATA_154": ("站场", "增益"),                   # 希奈丝特拉
    "EDR_464": ("站场", "增益"),                    # 泰兰德
    "RLK_541": ("站场", "增益"),                    # 维萨鲁斯
    "VAC_507": ("站场", "增益"),                    # 阳光汲取者莱妮莎
    "BOT_236": ("站场", "增益", "治疗"),            # 水晶工匠坎格尔：治疗翻倍
    "EDR_480": ("站场", "增益"),                    # 戈德林：友方野兽造成的伤害翻倍
    "VAN_EX1_350": ("站场", "增益"),                # 先知维伦（经典）
    "GIL_646_PUZZLE": ("站场", "增益"),             # 发条自动机（谜题版）
    "TLC_836": ("站场", "增益"),                    # 环形山的尼利：本就有增益，不重复
    # 反例：受到伤害翻倍方向不明（也可能让己方挨打更多），不收
    "LOE_118": ("站场", "斩杀"),                    # 诅咒之刃：英雄受到伤害翻倍（代价）
    "TIME_060": ("站场",),                          # 量子反稳定机：本随从受到伤害翻倍
    "BT_733": ("站场",),                            # 莫尔葛工匠：所有随从受到法术伤害翻倍
    "AT_004": ("单解",),                            # 奥术冲击：「受到的法术伤害增益效果翻倍」
    "TTN_726": ("单解", "斩杀"),                    # 焦油飞溅：随从受到伤害翻倍
    "SC_757": ("站场", "资源"),                     # 幻像：复制受到伤害翻倍
    "GDB_472": ("站场", "资源"),                    # 塔尔加斯：敌方随从受到伤害翻倍
    "RLK_952": ("站场",),                           # 附魔师：敌方随从受到伤害翻倍
    "ULDA_714": (),                                 # 畸变：英雄受到伤害翻倍，仍 0 标签
    "STORY_06_KINGSLAYERS": ("站场", "斩杀"),       # 弑君者：敌方英雄受到伤害翻倍
    "TIME_211A": ("站场",),                         # 强化辛艾萨莉：属性值翻倍不属本判据
    # 反例：非「法术…施放两次」句式 / 技能改写 / #8 HERO 跳过
    "REV_942": ("站场",),                           # 圣物仓库：宾语是圣物
    "CORE_REV_942": ("站场",),
    "TTN_477": ("单解", "资源", "斩杀"),            # 熔火符文：本牌施放两次
    "WW_425": ("抽牌",),                            # 观星：从句被逗号隔断
    "BE_801": ("抽牌",),                            # 百花齐放：同上
    "JAIL_123": ("站场", "资源"),                   # 越狱构造师：「该牌被使用时」
    "TTN_302": ("资源",),                           # 泰坦锻造陷阱：发现并施放两次
    "CORE_DMF_057": ("单解", "资源"),               # 月蚀：减费，不是施放两次
    "DMF_057": ("单解", "资源"),
    "CORE_EX1_625": ("单解", "斩杀"),               # #9 技能改写，仍不算增益
    "BT_429": ("单解", "斩杀"),
    "BOT_238": (),                                  # #8 HERO 跳过，仍 0 标签
    # #15 B1：把友方随从交给对手 = 减益
    "CORE_ICC_206": ("减益",),                       # 变节：0 标签转减益
    "ICC_206": ("减益",),                            # 变节（原版）
    "BCON_015": ("减益", "抽牌"),                    # 慷慨大方：补减益，抽牌保留
    # 反例：交给对手的是「牌」不是随从，方向模糊（B2 宽口径不收）
    "JAIL_206": ("抽牌",),
    "JAM_036": ("站场", "抽牌"),
    "TRL_096": ("站场", "资源"),
    "STORY_05_HORDEADVENTURER": ("站场",),
    # 反例：获得控制权方向相反，本就归单解
    "CORE_ICC_849": ("单解",),
    "ICC_849": ("单解",),
    "BOM_03_POWEROFLOVE_08S": ("单解",),
    "STORY_10_ICECROWNOBELISK": ("站场",),
    # #16 B1：投掷武器 = 单解（伤害量依赖武器，动作确定）
    "CORE_ICC_233": ("单解",),                       # 末日回旋镖：0 标签转单解
    "ICC_233": ("单解",),                            # 末日回旋镖（原版）
    # 反例：飞镖投掷走 _PROJECTILE_RE、回手族按既有边界，全不受影响
    "WW_006": ("单解", "资源", "斩杀"),
    "CORE_EX1_144": ("资源",),
    "EX1_144": ("资源",),
    "CATA_585": ("单解",),
    "BT_711": ("单解", "站场"),
    "CATA_215": ("单解",),
    "AV_226": ("单解", "减益"),
    "CORE_AV_226": ("单解", "减益"),
    "BOM_10_WINGBUFFET_006S": ("群解",),
    # 先例：「造成等同于…攻击力」既有判据不受影响
    "BT_354": ("群解",),
    "CAP_001": ("单解", "增益"),
    "BE_732": ("单解", "站场"),
    "AV_209": ("站场", "斩杀"),
    # #17 B1：亡语「回到战场」= 站场（同「再次召唤本随从」先例）
    "CORE_ICC_244": ("站场",),                       # 殊死一搏：0 标签转站场
    "ICC_244": ("站场",),                            # 殊死一搏（原版）
    "ICC_244E": ("站场",),                           # 救赎附魔：规则命中但 scope 外、不落盘
    # 反例：奥秘救赎是「使其回到战场」，无亡语前缀，仍 0 标签待定
    "EX1_136": (),
    "VAN_EX1_136": (),
    # 反例：既有「再次召唤」先例全是站场、无增益，本轮不碰
    "CS2_038": ("站场",),
    "BT_101": ("站场",),
    "CORE_ICC_257": ("站场",),
    # #18 B1：指控牌的延迟消灭 = 单解（动词在句末，_KILL_RE 认不到）
    "CORE_MAW_001": ("单解",),                       # 纵火指控：0 标签转单解
    "MAW_001": ("单解",),                            # 纵火指控（原版）
    "CORE_MAW_019": ("单解",),                       # 谋杀指控：0 标签转单解
    "MAW_019": ("单解",),                            # 谋杀指控（原版）
    # 反例：句号隔开 + why 含「复制」的盗窃指控不收
    "CORE_MAW_023": (),
    "MAW_023": (),
    # 反例：奥秘是 SECRET 强制待定，「下个回合开始时」「每当…时」都不收
    "CORE_CS3_016": (),
    "CS3_016": (),
    "EX1_594": (),
    "UNG_831": (),
    "AT_063": ("站场",),
    "WON_024": ("站场",),
    # 反例：已有标签的延迟消灭牌，补单解即误伤
    "RLK_536": ("未知",),
    "ETC_534": ("站场",),
    "NX2_037": ("站场",),
    "SCH_126": ("站场",),
    "VAC_503": ("站场",),
    # #19 B1：单体「移出对战」= 单解（永久移除、不触发亡语的硬解）
    "CORE_MAW_013": ("单解",),                       # 终身刑罚：0 标签转单解
    "MAW_013": ("单解",),                            # 终身刑罚（原版）
    # 反例：群体写法不收；WW_0700 是 HERO，按 #8 不加标签
    "WW_0700": (),
    "TLC_452T21": ("站场",),                         # scope 外，仅随从牌本身
    "TTN_429T2": (),                                 # scope 外
    # 反例：既有软解先例不受 #19 影响
    "FP1_025": ("单解", "站场"),
    "ULD_269": ("站场",),
    # #20 贴休眠：窄口径 ^锚定恰好 3 张，0 标签转减益
    "CORE_MAW_026": ("减益",),                       # 监禁
    "MAW_026": ("减益",),                            # 监禁（原版）
    "GDB_851B": ("减益",),                           # 晕击星光
    # 反例：中口径「使其休眠N回合」的另外 5 张，带战吼/自己召唤的前缀，不收
    "BT_737": ("站场",),                             # 玛维·影歌
    "TIME_442": ("站场",),                           # 时间流守望者
    "ETC_838": ("站场",),                            # 宏大梦想：给自己召唤的野兽贴
    "MIS_305": ("站场", "资源"),                     # 产品延期
    "JAIL_850": ("站场", "增益"),                    # 典狱官玛维
    # 反例：把动作写在未来的第 11 类，仍归未知
    "RLK_919": ("站场", "未知"),
    "CORE_RLK_919": ("站场", "未知"),
    "MAW_026E": ("未知",),                           # 附魔「服刑」，scope 外
    "BT_737E": ("未知",),                            # 附魔「被禁锢」，scope 外
    # 反例：冻结仍是减益（§3 软处理归属，本轮未动）
    "PART_005": ("减益",),
    # #21 B1：新增第 12 类「保护」，免伤族整族从增益迁入（互斥）
    "CORE_REV_504": ("保护",),                       # 脱罪力证：0 标签转保护
    "REV_504": ("保护",),                            # 脱罪力证（原版）
    "LOE_119": ("站场", "保护"),                     # 活化铠甲：补保护，保留站场
    "AV_146": ("站场", "斩杀", "保护"),              # 无法撼动之物：伤害减半
    "GVG_021": ("站场", "增益", "保护"),             # 玛尔加尼斯：+2/+2 仍给增益
    # 反例：受益对象不是己方英雄、或是伤害效果递减，都不收
    "LOEA16_27": ("站场",),                          # 本随从每次只会受到1点伤害
    "LOEA16_27H": ("站场",),
    "KARA_08_06": ("站场",),                         # 蓝色光线照射下的角色
    "TRLA_142": ("站场",),                           # 嘲讽（随从）每次只会受到1点
    "CATA_491": ("群解",),                           # 每次伤害减少1点＝伤害效果递减
    "JAM_002": ("单解",),
    # #22 变形费用三分支（用户拍板）：增加=增益 / 减少=减益 / 相同=未知
    "CORE_REV_920": ("增益",),                       # 可信的伪装：0 标签转增益
    "REV_920": ("增益",),
    "DMF_108": ("增益",),                            # 愚人套牌：牌库法术升级
    "TTN_485": ("增益",),                            # 星辰排列：手牌随从升级
    "ULDA_116": ("增益",),                           # 无恒药剂：冒险 passive
    "CORE_REV_923": ("站场", "增益"),                # 淤泥之池：地标牌型 + 升级
    "REV_923": ("站场", "增益"),
    "CORE_REV_924": ("增益", "减益", "资源"),        # 始源之潮：敌方降级 + 友方升级
    "REV_924": ("增益", "减益", "资源"),
    "CFM_696": ("群解", "减益", "资源"),             # 衰变：敌方降级
    "SCH_235": ("减益", "资源"),                     # 衰变飞弹
    "BE_055": ("站场", "资源", "未知"),              # 阿扎达斯：同费变形
    "DMF_700": ("群解", "未知"),                     # 异变轮转
    "GVG_108": ("站场", "未知"),                     # 侏儒变形师
    "THD_042P": ("单解", "未知"),                    # 布莱恩的鞍座
    # 反例：#8 HERO 不加标签；奥秘标签层给增益、仍按 SECRET 分支待定
    "CORE_ICC_481": (),                              # 死亡先知萨尔（HERO）
    "ICC_481": (),
    "BT_042": ("单解", "增益"),
    # #23 B1：解锁过载水晶并造成等量伤害 → 单解+斩杀
    "CORE_SW_114": ("单解", "斩杀"),             # 强行透支：0 标签转单解+斩杀
    "SW_114": ("单解", "斩杀"),
    # 反例：解锁 / 等量伤害族其余写法都不收
    "BRM_011": ("单解", "斩杀"),                  # 熔岩震击：先伤害后解锁，旧判据已给
    "CATA_724": ("站场",),                        # 缚风者：亡语解锁
    "SW_031": (),                                 # 号令元素：任务线奖励解锁，仍待定
    "TTN_835": ("站场", "抽牌"),                  # 托里姆：战吼解锁+抽牌
    "OG_104": (),                                 # 暗影之握：治疗转伤害，仍待定
    "AT_026": ("站场",),                          # 愤怒卫士：每当受到伤害的等量反伤
    # #24 B2：全体友方随从回手 → 资源（B1 的「保护」未采用）
    "DAL_728": ("资源",),                         # 战略转移：0 标签转资源
    "ETC_079": ("资源",),                         # 舞动全场：减费句本轮不夹带
    "PART_002": (),                               # 单个友方随从回手，仍待定
    "SCH_514": (),                                # 两个已死友方随从回手，仍待定
    "UNG_956": (),                                # 亡语贴回手，仍待定
    "SW_460": (),                                 # 攻击后回手，仍待定
    "CFM_800": (),                                # 奥秘战术撤离，仍按 SECRET 待定
}


class TagRulesCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = cards_mod.load()

    def card(self, name: str) -> Card:
        card = self.db.by_name_or_id(name)
        assert card is not None, f"卡牌库缺少 {name}"
        return card

    def test_tag_order(self) -> None:
        self.assertEqual(
            tag_rules.TAG_ORDER,
            ("单解", "群解", "站场", "增益", "减益", "治疗", "护甲", "资源", "抽牌", "斩杀",
             "未知", "保护"),
        )

    def test_rules_cover_all_tags(self) -> None:
        self.assertEqual(tuple(tag_rules.RULES), tag_rules.TAG_ORDER)
        for tag, rule in tag_rules.RULES.items():
            with self.subTest(tag=tag):
                self.assertTrue(callable(rule))

    def test_required_examples(self) -> None:
        for table_name, table in (
            ("REQUIRED", REQUIRED), ("REGRESSION", REGRESSION),
            ("AUDIT_S1_S4", AUDIT_S1_S4), ("AUDIT_PENDING", AUDIT_PENDING),
        ):
            for name, expected in table.items():
                with self.subTest(table=table_name, name=name):
                    self.assertEqual(_tags(self.card(name)), expected)

    def test_negative_examples(self) -> None:
        for name, tag in NEGATIVE:
            with self.subTest(name=name, tag=tag):
                self.assertNotIn(tag, _tags(self.card(name)))


    def test_resolved_families_leave_pending(self) -> None:
        for family, cids, expected in RESOLVED_FAMILIES:
            for cid in cids:
                with self.subTest(family=family, cid=cid):
                    self.assertEqual(_tags(self.card(cid)), expected)
                    self.assertIsNone(tag_rules.pending_reason(self.card(cid)))

    def test_unknown_ref_leaves_pending(self) -> None:
        # 引用句被第 11 类认下后，就不该再留在待定清单里；
        # 有确定标签的牌（REQUIRED）同理，不该被拖进待定
        for cid in ("AV_258", "AV_283", *REQUIRED):
            with self.subTest(cid=cid):
                self.assertIsNone(tag_rules.pending_reason(self.card(cid)))

    def test_protect_damage_limit_leaves_pending(self) -> None:
        # #21 伤害上限族：两张脱罪力证由 0 标签转保护并退出待定
        for cid in ("CORE_REV_504", "REV_504"):
            with self.subTest(cid=cid):
                self.assertEqual(_tags(self.card(cid)), ("保护",))
                self.assertIsNone(tag_rules.pending_reason(self.card(cid)))
        # 钉死第 12 类的全库命中面：免伤 + 伤害上限 + 伤害减半共 43 张
        # （scope 内 24 张，其余是冒险/附魔/乱斗牌，规则命中但不落盘）
        hits = [cid for cid, card in self.db.by_id.items()
                if tag_rules.rule_protect(card)]
        self.assertEqual(len(hits), 43)
        # 迁移是互斥的：只靠免伤拿标签的牌不该再带「增益」
        for cid in ("BT_752", "TRL_302", "CORE_ICC_850", "KAR_712"):
            with self.subTest(cid=cid):
                self.assertIn("保护", _tags(self.card(cid)))
                self.assertNotIn("增益", _tags(self.card(cid)))

    def test_hero_owns_keyword_leaves_pending(self) -> None:
        # 英雄关键词光环被增益认下后，就不该再留在待定清单里
        for cid in ("CATA_530", "ETC_398", "CATA_151"):
            with self.subTest(cid=cid):
                self.assertIn("增益", _tags(self.card(cid)))
                self.assertIsNone(tag_rules.pending_reason(self.card(cid)))

    def test_event_essence_tags(self) -> None:
        # 审计 #13：6 份精华各按自己的效果文本打标，本体（系统牌）不打标
        expected = {
            "CATA_EVENT_110T2": ("单解", "斩杀"),   # 对一个敌人造成8点伤害
            "CATA_EVENT_110T3": ("抽牌",),          # 抽三张法术牌
            "CATA_EVENT_110T4": ("资源",),          # 复原八个法力水晶
            "CATA_EVENT_110T5": ("护甲",),          # 获得12点护甲值
            "CATA_EVENT_110T6": ("站场",),          # 召唤一条8/8的龙
            "CATA_EVENT_110T7": ("群解",),          # 对所有敌方随从造成4点伤害
        }
        for cid, want in expected.items():
            with self.subTest(cid=cid):
                self.assertEqual(_tags(self.card(cid)), want)
                self.assertIsNone(tag_rules.pending_reason(self.card(cid)))

    def test_return_to_battlefield_leaves_pending(self) -> None:
        # 「亡语：回到战场」被站场认下后，两张殊死一搏就不该再留在待定清单里
        for cid in ("CORE_ICC_244", "ICC_244"):
            with self.subTest(cid=cid):
                self.assertEqual(_tags(self.card(cid)), ("站场",))
                self.assertIsNone(tag_rules.pending_reason(self.card(cid)))
        # 两张奥秘救赎仍是奥秘待定（why 不变、标签仍为空）
        for cid in ("EX1_136", "VAN_EX1_136"):
            with self.subTest(cid=cid):
                self.assertEqual(_tags(self.card(cid)), ())
                self.assertEqual(
                    tag_rules.pending_reason(self.card(cid)),
                    "奥秘：效果取决于对手动作，规则不硬猜",
                )

    def test_delayed_kill_leaves_pending(self) -> None:
        # 指控牌的延迟消灭被单解认下后，四张牌都不该再留在待定清单里
        for cid in ("CORE_MAW_001", "MAW_001", "CORE_MAW_019", "MAW_019"):
            with self.subTest(cid=cid):
                self.assertEqual(_tags(self.card(cid)), ("单解",))
                self.assertIsNone(tag_rules.pending_reason(self.card(cid)))
        # 两张盗窃指控仍是「文本含复制」待定（why 不变、标签仍为空）
        for cid in ("CORE_MAW_023", "MAW_023"):
            with self.subTest(cid=cid):
                self.assertEqual(_tags(self.card(cid)), ())
                self.assertEqual(
                    tag_rules.pending_reason(self.card(cid)),
                    "文本含「复制」，规则未命中任何标签，可能有分支漏标",
                )

    def test_remove_from_game_leaves_pending(self) -> None:
        # 单体「移出对战」被单解认下后，两张终身刑罚不该再留在待定清单里
        for cid in ("CORE_MAW_013", "MAW_013"):
            with self.subTest(cid=cid):
                self.assertEqual(_tags(self.card(cid)), ("单解",))
                self.assertIsNone(tag_rules.pending_reason(self.card(cid)))
        # HERO 雷诺按 #8 仍不加标签、why 不变（不会被群体写法顺带打标）
        reno = self.card("WW_0700")
        self.assertEqual(reno.type, "HERO")
        self.assertEqual(_tags(reno), ())
        self.assertEqual(
            tag_rules.pending_reason(reno),
            "规则未命中任何标签，卡面文本需要人工判断",
        )

    def test_puts_to_sleep_leaves_pending(self) -> None:
        # 贴休眠被减益认下后，3 张纯法术不该再留在待定清单里
        for cid in ("CORE_MAW_026", "MAW_026", "GDB_851B"):
            with self.subTest(cid=cid):
                self.assertEqual(_tags(self.card(cid)), ("减益",))
                self.assertIsNone(tag_rules.pending_reason(self.card(cid)))
        # 钉死命中面：^ 锚定的窄判据全库只认这 3 张
        hits = sorted(cid for cid, card in self.db.by_id.items()
                      if tag_rules._puts_to_sleep(card))
        self.assertEqual(hits, ["CORE_MAW_026", "GDB_851B", "MAW_026"])
        # 战吼贴休眠的两张先例仍只有站场，不被顺带补减益
        for cid in ("BT_737", "TIME_442"):
            with self.subTest(cid=cid):
                self.assertNotIn("减益", _tags(self.card(cid)))

    def test_transform_cost_leaves_pending(self) -> None:
        # #22：5 张 0 标签队列牌拿到增益后，就不该再留在待定清单里
        for cid in ("CORE_REV_920", "REV_920", "DMF_108", "TTN_485", "ULDA_116"):
            with self.subTest(cid=cid):
                self.assertEqual(_tags(self.card(cid)), ("增益",))
                self.assertIsNone(tag_rules.pending_reason(self.card(cid)))
        # 全库钉死命中面：59 张牌的三方向与标签严格一一对应，互不串味
        wanted = {"增加": "增益", "减少": "减益", "相同": "未知"}
        checked = 0
        for cid, card in self.db.by_id.items():
            dirs = tag_rules._transform_cost_directions(card)
            if not dirs:
                continue
            checked += 1
            with self.subTest(cid=cid):
                for direction, tag in wanted.items():
                    self.assertEqual(direction in dirs, tag in _tags(card))
        self.assertEqual(checked, 59)
        # #8：HERO 死亡先知萨尔不参与，仍是 0 标签待定
        for cid in ("CORE_ICC_481", "ICC_481"):
            with self.subTest(cid=cid):
                self.assertEqual(self.card(cid).type, "HERO")
                self.assertEqual(_tags(self.card(cid)), ())
                self.assertIsNotNone(tag_rules.pending_reason(self.card(cid)))
        # 奥秘：标签层给增益，但 pending_reason 仍按 SECRET 分支留在待定
        secret = self.card("BT_042")
        self.assertIn("增益", _tags(secret))
        self.assertEqual(
            tag_rules.pending_reason(secret),
            "奥秘：效果取决于对手动作，规则不硬猜",
        )

    def test_unlock_overload_damage_leaves_pending(self) -> None:
        # #23：两张强行透支拿到单解+斩杀后，就不该再留在待定清单里
        for cid in ("CORE_SW_114", "SW_114"):
            with self.subTest(cid=cid):
                self.assertEqual(_tags(self.card(cid)), ("单解", "斩杀"))
                self.assertIsNone(tag_rules.pending_reason(self.card(cid)))
        # 全库钉死命中面：窄正则恰好 2 张，不多不少
        hits = [cid for cid, card in self.db.by_id.items()
                if tag_rules._unlock_overload_damage(card)]
        self.assertEqual(sorted(hits), ["CORE_SW_114", "SW_114"])
        # 同族其余写法一律不动
        for cid, expected in (("BRM_011", ("单解", "斩杀")),
                              ("CATA_724", ("站场",)),
                              ("SW_031", ()),
                              ("TTN_835", ("站场", "抽牌")),
                              ("OG_104", ()),
                              ("AT_026", ("站场",))):
            with self.subTest(cid=cid):
                self.assertEqual(_tags(self.card(cid)), expected)

    def test_return_all_friendly_to_hand_leaves_pending(self) -> None:
        # #24 B2：两张「将所有友方随从移回你的手牌」拿到资源后退出待定；
        # 不是 B1，第 12 类「保护」范围没扩，仍只收英雄免伤族
        for cid in ("DAL_728", "ETC_079"):
            with self.subTest(cid=cid):
                self.assertEqual(_tags(self.card(cid)), ("资源",))
                self.assertIsNone(tag_rules.pending_reason(self.card(cid)))
        # 全库钉死命中面：窄正则恰好 2 张，不多不少
        hits = [cid for cid, card in self.db.by_id.items()
                if tag_rules._returns_all_friendly_to_hand(card)]
        self.assertEqual(sorted(hits), ["DAL_728", "ETC_079"])
        # `ETC_079` 的减费句本身仍不算资源：本轮没夹带放宽 rule_resource 的减费分支
        fake = Card(id="FAKE_COST_1", name="仅减费", type="SPELL",
                    text="在本回合中，其法力值消耗为（1）点。")
        self.assertNotIn("资源", _tags(fake))
        # 同族其余写法一律不动：暗影步靠减费句已有资源，其余仍 0 标签待定
        for cid, expected in (("EX1_144", ("资源",)),
                              ("CORE_EX1_144", ("资源",)),
                              ("PART_002", ()),
                              ("SCH_514", ()),
                              ("UNG_956", ()),
                              ("SW_460", ()),
                              ("CFM_800", ())):
            with self.subTest(cid=cid):
                self.assertEqual(_tags(self.card(cid)), expected)
        self.assertEqual(tag_rules.pending_reason(self.card("CFM_800")),
                         "奥秘：效果取决于对手动作，规则不硬猜")

    def test_own_resource_cost_leaves_pending(self) -> None:
        # #25 B1：两张纯己方代价整句牌拿到减益后退出待定；
        # 它们是 DEEP_027 暗石守卫的抉择选项牌，按 B1 打标、不按 #13 移出 scope
        for cid in ("DEEP_027A", "DEEP_027B"):
            with self.subTest(cid=cid):
                self.assertEqual(_tags(self.card(cid)), ("减益",))
                self.assertIsNone(tag_rules.pending_reason(self.card(cid)))
        # 全库钉死命中面：整句全等恰好 2 张，不多不少
        hits = [cid for cid, card in self.db.by_id.items()
                if tag_rules._own_resource_cost(card)]
        self.assertEqual(sorted(hits), ["DEEP_027A", "DEEP_027B"])
        # 同句但带其他动作的写法一律不补减益：本体已有站场、其余各有原标签
        for cid, expected in (("DEEP_027", ("站场",)),
                              ("EX1_301", ("站场",)),
                              ("VAN_EX1_301", ("站场",)),
                              ("EDR_483", ("未知",)),
                              ("EX1_310", ("站场", "斩杀")),
                              ("EX1_308", ("单解", "斩杀")),
                              ("LOOT_417", ("群解",)),
                              ("AT_025", ("群解",))):
            with self.subTest(cid=cid):
                self.assertEqual(_tags(self.card(cid)), expected)

    def test_cost_attack_swap_leaves_pending(self) -> None:
        # #26 B2：费用/攻击力互换双向都发生、方向不明 → 未知，退出待定
        card = self.card("DMF_534")
        self.assertEqual(_tags(card), ("未知",))
        self.assertIsNone(tag_rules.pending_reason(card))
        # 全库钉死命中面：恰好 DMF_534 一张
        hits = [cid for cid, c in self.db.by_id.items()
                if tag_rules._cost_attack_swap(c)]
        self.assertEqual(hits, ["DMF_534"])
        # 攻血互换族不受新判据影响，仍按减益打
        for cid in ("AT_016", "BOT_517", "CFM_063"):
            with self.subTest(cid=cid):
                self.assertIn("减益", _tags(self.card(cid)))
                self.assertFalse(tag_rules._cost_attack_swap(self.card(cid)))

    def test_plain_text_strips_markup(self) -> None:
        # $ / # 是卡面里的数值前缀，<b> 是富文本标记
        self.assertEqual(tag_rules.plain_text(self.card("火球术")), "造成6点伤害。")
        self.assertTrue(tag_rules.plain_text(self.card("治疗波")).startswith("恢复8点生命值"))
        warrior = tag_rules.plain_text(self.card("加尔范上尉"))
        self.assertNotRegex(warrior, r"[<>$#@_]")
        self.assertIn("护甲", warrior)
        self.assertIn("15", warrior)

    def test_plain_text_keeps_chinese_and_digits(self) -> None:
        text = tag_rules.plain_text(self.card("不稳定的传送门"))
        self.assertIn("随从", text)
        self.assertIn("3", text)

    def test_aoe_removal_can_hit_face(self) -> None:
        # 「对一个敌人造成…」能打到敌方英雄，按口径算斩杀（Lead 补充）
        swipe = _tags(self.card("横扫"))
        self.assertEqual(swipe, ("单解", "群解", "斩杀"))

    def test_weapon_cannot_hit_hero(self) -> None:
        weapon = Card(id="T_CANT_FACE", name="测试武器", type="WEAPON", atk=3,
                      text="<b>吸血</b>无法攻击英雄。")
        self.assertFalse(tag_rules.rule_lethal(weapon))
        self.assertTrue(tag_rules.rule_heal(weapon))

    def test_secret_goes_pending(self) -> None:
        counter = self.card("法术反制")
        self.assertEqual(_tags(counter), ())
        reason = tag_rules.pending_reason(counter)
        self.assertIsInstance(reason, str)
        self.assertIn("奥秘", reason)

    def test_merely_mentioning_secret_not_pending(self) -> None:
        # 只是「抽一张奥秘牌」「如果你控制一个奥秘」：动作确定，别拖进待定
        for name in ("秘法学家", "驯羊师", "神秘挑战者"):
            with self.subTest(name=name):
                card = self.card(name)
                self.assertIsNotNone(_tags(card), f"{name} 应有确定标签")
                self.assertIsNone(tag_rules.pending_reason(card))


    def test_pending_empty_text_is_none(self) -> None:
        self.assertIsNone(tag_rules.pending_reason(Card(id="T_EMPTY", name="空", type="SPELL", text="")))

    def test_pending_zero_tag_with_text(self) -> None:
        # 无效果文本、规则也认不出 → 进待定，而不是硬猜
        reason = tag_rules.pending_reason(
            Card(id="T_WEIRD", name="怪牌", type="SPELL", text="将双方玩家的牌库洗混。")
        )
        self.assertIsInstance(reason, str)

    def test_rules_are_pure(self) -> None:
        """规则只读 Card 字段：同一张牌连判两次结果一致，且不改动卡牌。"""
        card = self.card("寒冰箭")
        before = Card(**{**card.__dict__})
        self.assertEqual(_tags(card), _tags(card))
        self.assertEqual(card, before)


if __name__ == "__main__":
    unittest.main()
