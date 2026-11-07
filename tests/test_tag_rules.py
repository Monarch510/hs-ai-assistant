"""11 类卡牌功能标签的规则钉行为（CONTEXT §6 必测例子）。

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
    # 反例：翻倍不是「+N 增量」；替换技能在 #9 改判单解+斩杀（仍不算增益）
    "EX1_350": ("站场",),                            # 先知维伦：伤害翻倍
    "GIL_646": ("站场",),                            # 发条自动机：伤害翻倍
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
    # #10 B2：受益对象明确是「你的英雄」的免伤 = 增益（关键词增益表免疫的显式例外）
    "BT_752": ("增益",),                             # 疾影：全库唯一「无法受到伤害」
    "TRL_302": ("增益",),                            # 暂避锋芒：直到下个回合你的英雄免疫
    "CORE_ICC_850": ("站场", "增益", "斩杀"),         # 暗影之刃：战吼免伤 + 武器能打脸
    "KAR_712": ("站场", "增益"),                     # 紫罗兰幻术师：在你的回合中免伤
    "ULDA_114": ("站场", "增益"),                    # 死亡护盾：武器光环
    "TIME_021": ("站场", "增益"),                    # 末日准备狂：流放触发的免伤
    "GDB_843": ("站场", "增益", "资源", "斩杀"),      # 视差光枪：法术迸发免伤
    "CORE_GVG_021": ("站场", "增益"),                # 玛尔加尼斯：本就含增益，标签不变
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
    "DEEP_027": ("站场",),                           # 暗石守卫：抉择代价
    "DEEP_027B": (),                                 # 法力裂解：纯代价牌，仍 0 标签
    "DMF_115": ("站场",),                            # 怨灵捣蛋鬼：每个玩家各一个
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
    "TRL_302": ("增益",),                            # #10 已收的免疫，不重复
    "BT_752": ("增益",),                             # #10 已收的免伤，不重复
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
            ("单解", "群解", "站场", "增益", "减益", "治疗", "护甲", "资源", "抽牌", "斩杀", "未知"),
        )

    def test_rules_cover_all_tags(self) -> None:
        self.assertEqual(tuple(tag_rules.RULES), tag_rules.TAG_ORDER)
        for tag, rule in tag_rules.RULES.items():
            with self.subTest(tag=tag):
                self.assertTrue(callable(rule))

    def test_required_examples(self) -> None:
        for name, expected in REQUIRED.items():
            with self.subTest(name=name):
                self.assertEqual(_tags(self.card(name)), expected)

    def test_negative_examples(self) -> None:
        for name, tag in NEGATIVE:
            with self.subTest(name=name, tag=tag):
                self.assertNotIn(tag, _tags(self.card(name)))

    def test_audit_regressions(self) -> None:
        for name, expected in REGRESSION.items():
            with self.subTest(name=name):
                self.assertEqual(_tags(self.card(name)), expected)

    def test_audit_s1_s4(self) -> None:
        for key, expected in AUDIT_S1_S4.items():
            with self.subTest(key=key):
                self.assertEqual(_tags(self.card(key)), expected)

    def test_audit_pending(self) -> None:
        for name, expected in AUDIT_PENDING.items():
            with self.subTest(name=name):
                self.assertEqual(_tags(self.card(name)), expected)

    def test_unknown_ref_leaves_pending(self) -> None:
        # 引用句被第 11 类认下后，就不该再留在待定清单里
        for cid in ("AV_258", "AV_283"):
            with self.subTest(cid=cid):
                self.assertIsNone(tag_rules.pending_reason(self.card(cid)))

    def test_owns_plus_leaves_pending(self) -> None:
        # 光环被增益规则认下后，就不该再留在待定清单里
        for cid in ("AV_661", "WW_341"):
            with self.subTest(cid=cid):
                self.assertEqual(_tags(self.card(cid)), ("增益",))
                self.assertIsNone(tag_rules.pending_reason(self.card(cid)))

    def test_damage_bonus_leaves_pending(self) -> None:
        # 后续伤害增量被认下后，就不该再留在待定清单里
        for cid in ("BAR_546", "KARA_00_06", "TOY_801b"):
            with self.subTest(cid=cid):
                self.assertEqual(_tags(self.card(cid)), ("增益",))
                self.assertIsNone(tag_rules.pending_reason(self.card(cid)))

    def test_deck_destroy_leaves_pending(self) -> None:
        # 牌库摧毁被减益认下后，就不该再留在待定清单里
        for cid in ("BAR_913", "TRL_246"):
            with self.subTest(cid=cid):
                self.assertEqual(_tags(self.card(cid)), ("减益",))
                self.assertIsNone(tag_rules.pending_reason(self.card(cid)))

    def test_unlock_target_leaves_pending(self) -> None:
        # 解锁目标被增益认下后，就不该再留在待定清单里（含任务线牌的「奖励：」句）
        for cid in ("BCON_001", "SW_322"):
            with self.subTest(cid=cid):
                self.assertEqual(_tags(self.card(cid)), ("增益",))
                self.assertIsNone(tag_rules.pending_reason(self.card(cid)))

    def test_hp_replacement_leaves_pending(self) -> None:
        # 技能改写被单解+斩杀认下后，就不该再留在待定清单里
        for cid in ("BT_429", "CORE_EX1_625", "TLC_632"):
            with self.subTest(cid=cid):
                self.assertEqual(_tags(self.card(cid)), ("单解", "斩杀"))
                self.assertIsNone(tag_rules.pending_reason(self.card(cid)))

    def test_hero_immune_leaves_pending(self) -> None:
        # 己方英雄免伤被增益认下后，就不该再留在待定清单里
        for cid in ("BT_752", "TRL_302"):
            with self.subTest(cid=cid):
                self.assertEqual(_tags(self.card(cid)), ("增益",))
                self.assertIsNone(tag_rules.pending_reason(self.card(cid)))

    def test_mana_drain_leaves_pending(self) -> None:
        # 削对方法力水晶被减益认下后，就不该再留在待定清单里
        for cid in ("BT_753", "PROLOGUE_MANABURN"):
            with self.subTest(cid=cid):
                self.assertEqual(_tags(self.card(cid)), ("减益",))
                self.assertIsNone(tag_rules.pending_reason(self.card(cid)))

    def test_hero_owns_keyword_leaves_pending(self) -> None:
        # 英雄关键词光环被增益认下后，就不该再留在待定清单里
        for cid in ("CATA_530", "ETC_398", "CATA_151"):
            with self.subTest(cid=cid):
                self.assertIn("增益", _tags(self.card(cid)))
                self.assertIsNone(tag_rules.pending_reason(self.card(cid)))

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

    def test_confident_cards_not_pending(self) -> None:
        for name in REQUIRED:
            with self.subTest(name=name):
                self.assertIsNone(tag_rules.pending_reason(self.card(name)))

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
