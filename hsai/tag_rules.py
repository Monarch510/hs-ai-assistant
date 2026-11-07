"""11 类卡牌功能标签的纯文本规则。

口径见 docs/card-tagging/CONTEXT.md 第 3 节。这里只做「读卡面 → 打标签」，
不碰 IO、不依赖上层模块；落盘/查询在 hsai/tags.py。

为什么优先看结构化字段
----------------------
文本里提到某件事，不等于这张牌会做这件事：
· 裂盾一击只是「每有 1 点护甲值，本牌费用减少」——不是治疗/护甲
· 英雄之魂只是让英雄技能多 1 点伤害——不是本牌造成伤害
· 加尔范上尉只是「获得护甲值 ≥15 时」才给冲锋——不能按文本白送斩杀
所以 type/mechanics 优先（随从/武器/地标=站场，冲锋=斩杀，吸血=治疗，
冻结=减益），文本只用来补「动作 + 对象」都齐全的效果句。

保守优先
--------
规则宁漏勿错：拿不准的由 pending_reason 送进待定清单，不硬猜。
"""

from __future__ import annotations

import re

from .cards import Card

TAG_ORDER = ("单解", "群解", "站场", "增益", "减益", "治疗", "护甲", "资源", "抽牌", "斩杀", "未知")

_MARKUP_RE = re.compile(r"<[^>]*>")
_DROP_CHARS = str.maketrans("", "", "$#@_{}")
_SPACE_RE = re.compile(r"\s+")
_CLAUSE_RE = re.compile(r"[。；！\n]")

# 任务条件句 / 升级解锁提示：是「怎么升级、还差什么」，不是本牌效果。
_QUEST_RE = re.compile(r"(任务：|任务线：|支线任务：)")
_UPGRADE_WORDS = ("升级",)


def plain_text(card: Card) -> str:
    """卡面文本去掉富文本标记：<b>…</b>、$、#、@、_、{占位符}，空白压掉。

    中文卡面里 $ 是伤害数值前缀、# 是治疗数值前缀、@ 分隔占位段落；
    不剥掉就没法写「造成 6 点伤害」这种稳定匹配。
    """
    t = _MARKUP_RE.sub("", card.text or "")
    t = t.translate(_DROP_CHARS)
    return _SPACE_RE.sub("", t)


def _clauses(text: str) -> list[str]:
    return [c for c in _CLAUSE_RE.split(text) if c]


def _is_quest_clause(clause: str) -> bool:
    return bool(_QUEST_RE.search(clause)) or clause.startswith("奖励")


def _is_upgrade_hint(clause: str) -> bool:
    """「（获得 3 点护甲值后升级。）」这类提示句。"""
    if not any(w in clause for w in _UPGRADE_WORDS):
        return False
    if "后升级" in clause or "都会升级" in clause or "升级！" in clause:
        return True
    return clause.endswith("升级") and any(w in clause for w in ("（", "后", "都", "时"))


def _effect_clauses(text: str) -> list[str]:
    return [c for c in _clauses(text) if not _is_quest_clause(c) and not _is_upgrade_hint(c)]


# ---------------------------------------------------------------- 目标判定
# 伤害/消灭的宾语短语 → 能命中「一只随从 / 多只随从 / 敌方英雄」。
# 只做关键词判断，不做语法分析：够用，且出错时是漏判而不是乱打标。

_MASS_WORDS = (
    "所有", "每个", "全场", "随机分配", "最左", "最右",
    "两个", "三个", "四个", "五个", "几个", "若干", "多个",
)
_MINION_WORDS = ("随从", "敌人", "角色", "目标")


def _is_own_target(phrase: str) -> bool:
    if "友方" in phrase:
        return True
    if any(w in phrase for w in ("敌方", "敌人", "对手")):
        return False
    return ("你的" in phrase or "你所" in phrase
            or any(w in phrase for w in ("本随从", "本武器", "自身", "自己")))


def _is_own_clause(clause: str) -> bool:
    """这一句是不是在说自己/自己这边的东西（减益判定用）。"""
    if "友方" in clause:
        return True
    if any(w in clause for w in ("敌方", "敌人", "对手")):
        return False
    return any(w in clause for w in ("你的", "你所", "本随从", "本武器", "自身", "自己", "手牌", "牌库"))


def _damage_target(phrase: str) -> tuple[bool, bool, bool]:
    """(能打一只随从, 能打多只随从, 能打敌方英雄)。phrase 为空=无指定目标的直伤。"""
    if not phrase:
        return True, False, True
    own = _is_own_target(phrase)
    many = any(w in phrase for w in _MASS_WORDS)
    if "相邻" in phrase:
        # 「一个相邻目标」只波及一只；「及其相邻 / 相邻的随从」才有多只
        single_adjacent = re.search(r"(一个|随机一个)相邻", phrase) and not any(
            k in phrase for k in ("及其", "其相邻", "攻击目标相邻", "和随机")
        )
        many = many or not single_adjacent
    hits_minion = any(w in phrase for w in _MINION_WORDS) or phrase in (
        "其", "它", "它们", "该随从", "这些",
    )
    # 「角色/敌人/英雄」都可能指向敌方英雄，所以能打脸；「敌方随从/一个随从」
    # 这类只打随从的不算。单体「一个角色」也是斩杀（CONTEXT §6：寒冰箭）。
    face = False
    if not own:
        if any(w in phrase for w in ("角色", "敌人", "英雄")):
            face = True
        elif "对手" in phrase and "随从" not in phrase:
            face = True
    if own and not face:
        # 混合目标（「对一个随从和你的英雄各造成 4 点」）：随从那部分仍算单体去除
        if any(w in phrase for w in ("随从", "敌人", "角色")) and "和" in phrase:
            return True, False, False
        return False, False, False
    return hits_minion and not many, many and hits_minion, face


def _kill_target(phrase: str) -> tuple[bool, bool, bool]:
    """(能杀一只随从, 能杀多只随从, 能杀敌方英雄)。"""
    if not phrase:
        return False, False, False
    if "敌方英雄" in phrase:
        return False, False, True
    own = _is_own_target(phrase)
    many = any(w in phrase for w in ("所有", "每个", "全场", "全部", "这些")) or bool(
        re.match(r"(两个|三个|四个|五个|几个|若干|多个|\d+个)", phrase)
    )
    hits_minion = any(w in phrase for w in ("随从", "敌人", "角色", "目标", "地标")) or phrase.startswith(
        ("该", "这个", "这些")
    ) or phrase in ("其", "它", "它们")
    if own and "敌方" not in phrase:
        return False, False, False
    # 「消灭具有0点攻击力的敌方随从」这类条件句是条件群杀：能杀一只也能杀多只
    conditional = (
        "一个" not in phrase
        and "所有" not in phrase
        and re.search(r"(具有|攻击力(?:为|小于|大于|等于))", phrase)
    )
    if hits_minion and conditional:
        return True, True, False
    return hits_minion and not many, many and hits_minion, False


# ---------------------------------------------------------------- 伤害
_DMG_TARGET_RE = re.compile(r"对([^。，；]{0,26}?)造成")
# 没有「对…」时，只有这些写法才算「本牌造成伤害」；
# 「造成伤害」「造成剩余伤害」之类是描述/条件，不算。
_BARE_DMG_RE = re.compile(
    r"造成(?:\d+|[一二三四五六七八九十]+)(?:到|至)(?:\d+|[一二三四五六七八九十]+)点伤害"
    r"|造成(?:\d+|[一二三四五六七八九十]+)点(?:或以上)?伤害"
    r"|造成等同于[^。，；]{0,24}伤害"
    r"|造成伤害，数值等同于"
)
_DMG_CONDITION_RE = re.compile(r"造成[^。，；]{0,6}伤害[后时]")
# 「发射三枚飞弹，每枚造成 2 点」——多段伤害，目标写在「对/向…发射」里
_PROJECTILE_RE = re.compile(
    r"(?:对|向)([^。；]{0,14}?)(?:发射|射出|投掷|开出|开|施放)[^。；]{0,22}?每(?:枚|发|颗|个|把|枪|支)"
)


def _is_damage_effect(clause: str) -> bool:
    """这句是不是本牌真的造成伤害（排除触发条件/描述/生成物）。"""
    if "英雄技能" in clause:  # 「强化技能/替换技能」
        return False
    if _DMG_CONDITION_RE.search(clause) or "造成过伤害" in clause or "用法术造成" in clause:
        return False
    if "可以造成" in clause and not any(w in clause for w in ("发射", "射出", "投掷")):
        return False
    if "每造成" in clause or re.search(r"造成伤害的", clause):
        return False
    return True


def _damage_hits(text: str) -> tuple[bool, bool, bool]:
    single = many = face = False
    for clause in _effect_clauses(text):
        if "造成" not in clause or "伤害" not in clause:
            continue
        if not _is_damage_effect(clause):
            continue
        projectile = _PROJECTILE_RE.search(clause)
        if projectile:
            target = projectile.group(1)
            if "手牌" in target or "牌库" in target:
                continue  # 打手里/牌库里的牌不是场上解
            s, mny, f = _damage_target(target)
            single, face = single or s, face or f
            many = many or s or mny  # 多枚飞弹本来就可能落到多只身上
            continue
        matched = False
        for m in _DMG_TARGET_RE.finditer(clause):
            s, mny, f = _damage_target(m.group(1))
            single, many, face = single or s, many or mny, face or f
            matched = True
        if not matched:
            if not _BARE_DMG_RE.search(clause):
                continue
            # 「随机分配到…」按分配目标判，不拿整句里的「友方」当伤害目标
            # （「沉默一个友方随从，然后…分配到所有敌方随从」）
            if "分配" in clause:
                am = re.search(r"分配(?:到|给|至)([^。，；]{0,22})", clause)
                target = am.group(1) if am else clause
                s, mny, f = _damage_target(target)
                single, many, face = single or s, many or mny, face or f
                continue
            single = face = True
        # 随机多次（「随机对一个敌人造成 1 点，重复 3 次」）能扫到多只
        if "随机" in clause and "重复" in clause:
            many = True
    return single, many, face


# ---------------------------------------------------------------- 消灭
_KILL_RE = re.compile(r"消灭(?=([^。，；：]{0,30}))")


def _kill_hits(text: str) -> tuple[bool, bool, bool]:
    single = many = face = False
    for clause in _effect_clauses(text):
        if "消灭" not in clause:
            continue
        if "每当" in clause:
            continue
        # 「消灭这些机械并组合成…」是合体自己的随从，不是解场
        if any(w in clause for w in ("组合成", "组合为")):
            continue
        for m in _KILL_RE.finditer(clause):
            phrase = m.group(1)
            # 「被消灭的随从」「消灭一个随从后/时」都是描述或触发条件
            if not phrase or phrase.startswith(("了", "的")):
                continue
            # 「荣誉消灭：」是关键词说明，不是效果动词
            if clause[max(0, m.start() - 1):m.start()] in ("被", "誉"):
                continue
            if "后" in phrase[:6] or "时" in phrase[:6]:
                continue
            # 「消灭两侧相邻的随从」通常是吃自己的随从（虚空恐魔）
            if re.search(r"(两侧|本随从)[^。，；]{0,6}相邻", phrase):
                continue
            s, mny, f = _kill_target(phrase)
            single, many, face = single or s, many or mny, face or f
    return single, many, face


# ---------------------------------------------------------------- 变形 / 回手 / 夺控
def _transform_hits(text: str) -> tuple[bool, bool]:
    single = many = False
    for clause in _effect_clauses(text):
        if "变形" not in clause:
            continue
        # 变形自己（本随从/手牌/牌库/刚发现的东西）不是解场
        if any(w in clause for w in ("本随从", "手牌", "牌库", "发现")):
            continue
        if ("使所有" in clause or "将所有" in clause) and "你的所有" not in clause:
            many = True
            continue
        if "相邻" in clause:
            many = True
            continue
        if "将其变形" in clause:
            single = True
            continue
        if re.search(r"(使|将)一个[^。，；]{0,10}变形", clause) and "友方" not in clause:
            single = True
    return single, many


def _bounce_hits(text: str) -> tuple[bool, bool]:
    single = many = False
    for clause in _effect_clauses(text):
        if "手牌" not in clause or not re.search(r"(移回|返回)", clause):
            continue
        # 「把一个敌方随从移回你的手牌」是抢场+抢牌，既清对面场面又变成我方资源
        # （暗言术：窃）；「友方随从移回你的手牌」只是保护/回收，别算解场。
        if re.search(r"敌方[^。，；]{0,8}移回(?:你|自己)的手牌", clause):
            single = True
            continue
        if "移回你的手牌" in clause or "返回你的手牌" in clause:
            continue  # 回自己的手牌是保护/回收，不是解场
        if re.search(r"移回[^。，；]{0,6}手牌|返回[^。，；]{0,6}手牌", clause):
            if "所有" in clause or ("敌方随从" in clause and "一个" not in clause):
                many = True
            else:
                single = True
    return single, many


def _steal_to_hand(card: Card) -> bool:
    """把敌方随从变成自己手牌里的牌：单解之外还多了资源（CONTEXT 审计 #3）。"""
    for clause in _effect_clauses(plain_text(card)):
        if re.search(r"敌方[^。，；]{0,8}移回(?:你|自己)的手牌", clause):
            return True
    return False


def _steal_hits(text: str) -> tuple[bool, bool]:
    single = many = False
    for clause in _effect_clauses(text):
        if "控制权" not in clause or not any(w in clause for w in ("获得", "夺取")):
            continue
        if any(w in clause for w in ("所有", "三个", "两个", "每个")):
            many = True
        elif any(w in clause for w in ("一个", "其中", "该随从", "其")):
            single = True
    return single, many


# 「将你的英雄技能替换为"造成5点伤害"」这类技能改写：真正效果写在引号里，
# 剥掉这层壳就是一张普通伤害牌，按普通伤害牌判单解 + 斩杀（CONTEXT 审计 #9）。
# _is_damage_effect() 会拦下含「英雄技能」的从句，所以必须单独提取引号内容，
# 不能靠放宽 _damage_hits() 的既有判据。
_HP_REPLACE_RE = re.compile(
    r"英雄技能[^。；]{0,16}(?:替换为|变为|改为)[“\"]?([^”\"。；]{0,40})"
)
_HP_INNER_DMG_RE = re.compile(r"造成\d+点伤害")


def _hp_replacement_damage(card: Card) -> tuple[bool, bool]:
    """英雄技能被替换成造成伤害时，返回 (能单解, 能斩杀)。

    「造成5点伤害」无指定目标 → 可打随从也可打脸，两者都算；
    「随机对一个敌人造成8点伤害」按既有 _damage_target 同样两者都算。
    改写为召唤/发现/引用另一张牌的，引号内没有伤害数值，一律不收。
    """
    for m in _HP_REPLACE_RE.finditer(plain_text(card)):
        inner = m.group(1)
        if not _HP_INNER_DMG_RE.search(inner):
            continue
        single, _many, face = _damage_hits(inner)
        if single or face:
            return single, face
    return False, False


# 「对一个随从投掷你的武器」：伤害量取决于当前装备的武器，卡面没数值、
# 也没写「造成等同于…伤害」，_damage_hits 认不到（CONTEXT 审计 #16，用户拍板 B1）。
# 但动作确定 —— 对一个随从造成一次武器伤害 —— 与「造成等同于…攻击力的伤害」
# 先例同类（BT_354 刃舞=群解、CAP_001 静默打击=单解），按单解打。
# 全库只此 2 张（CORE_ICC_233 / ICC_233 末日回旋镖）；不收 WW_006 飞镖投掷
# （它走 _PROJECTILE_RE，已有单解+资源+斩杀），也不碰「移回你的手牌」族
# —— _bounce_hits 明确把回自己手牌当保护/回收跳过。
_THROW_WEAPON_RE = re.compile(r"投掷[^。；]{0,10}武器")


def _throws_weapon(card: Card) -> bool:
    """投掷武器对一个随从造成一次伤害：目标单体、动作确定，按单解打。"""
    return bool(_THROW_WEAPON_RE.search(plain_text(card)))


# ---------------------------------------------------------------- 10 条规则
def rule_single_removal(card: Card) -> bool:
    """能处理对面一只随从：单体伤害 / 消灭 / 变形 / 回手 / 夺控 / 投掷武器。"""
    if _hp_replacement_damage(card)[0] or _throws_weapon(card):
        return True
    text = plain_text(card)
    return any((
        _damage_hits(text)[0],
        _kill_hits(text)[0],
        _transform_hits(text)[0],
        _bounce_hits(text)[0],
        _steal_hits(text)[0],
    ))


def rule_aoe_removal(card: Card) -> bool:
    """能同时处理对面多只随从（冻结/沉默只算减益，不算群解）。"""
    text = plain_text(card)
    return any((
        _damage_hits(text)[1],
        _kill_hits(text)[1],
        _transform_hits(text)[1],
        _bounce_hits(text)[1],
        _steal_hits(text)[1],
    ))


def rule_board(card: Card) -> bool:
    """打出后场上多出或留下东西：随从牌、武器、地标、召唤、复活、装备、置入战场。"""
    if card.type in ("MINION", "WEAPON", "LOCATION"):
        return True
    for clause in _effect_clauses(plain_text(card)):
        if "召唤" in clause and not any(w in clause for w in ("每当", "召唤后", "召唤时")):
            return True
        if "复活" in clause or "置入战场" in clause:
            return True
        # 「装备一把武器」和「召唤」一样是落地效果（武器本来就属于站场）；
        # 「如果你装备着武器」只是条件，不产生场面。
        if "装备" in clause:
            if re.search(r"装备(?:一把|一块)", clause) and "每当" not in clause:
                return True
            if re.search(r"(并装备|装备你手牌|装备霜之哀伤)", clause):
                return True
    return False


_BUFF_GAIN_RE = re.compile(r"获得\+")
_BUFF_KEYWORD_RE = re.compile(r"获得[^。，；]{0,14}?(嘲讽|圣盾|复生|风怒|吸血|潜行|突袭|冲锋|剧毒)")
_BUFF_FIGURE_RE = re.compile(r"(攻击力|生命值|属性值)[^。，；]{0,6}(翻倍|等同于)")
_BUFF_WEAPON_RE = re.compile(r"(本武器|你的英雄)[^。，；]{0,8}攻击力变为")
# 「获得…属性值」（含「获得其属性值」「获得等同于…的属性值」）是加身材；
# 不并「属性值变为 X/X」——那是把身材改成固定值，方向可能是削（见耐普图隆/正义防御）。
_BUFF_ATTR_RE = re.compile(r"获得[^。，；]{0,20}属性值")

# 「偷取…N点身材」让一方变大、另一方变小，增益和减益都算（CONTEXT 审计 #3）。
# 只认带数值的表达：「夺取…控制权」「偷取一张牌」不是身材，别混进来。
_STEAL_FIGURE_RE = re.compile(r"偷取[^。，；]{0,12}\d+点(?:生命值|攻击力)")

# 光环式加值「你的…拥有+N」也是让单位变强（CONTEXT 审计 #4）。
# 「你最左边的随从拥有+N」这类同义写法同样收；但本随从自身条件不算：
# 「（在）你的回合拥有+N」「在你对手的回合拥有+N」没有受益对象，等于本随从自己变强，
# 按口径「本随从自身条件留着不碰」跳过；「你的英雄在你的回合拥有+N」有明确对象，保留。
_OWNS_PLUS_RE = re.compile(r"你[^。，；]{0,10}拥有\+\d+")


def _steals_figure(card: Card) -> bool:
    return any(_STEAL_FIGURE_RE.search(c) for c in _effect_clauses(plain_text(card)))


def _owns_plus(card: Card) -> bool:
    """「你的…拥有+N」是给别人加值；没有受益对象的「…回合拥有+N」不算。"""
    for clause in _effect_clauses(plain_text(card)):
        for match in _OWNS_PLUS_RE.finditer(clause):
            fragment = match.group(0)
            if "本随从" in fragment:
                continue
            if "回合" in fragment and "英雄" not in fragment:
                continue
            return True
    return False


# 「让后续伤害 +N」整族统一算增益（CONTEXT 审计 #5，用户拍板 D）。
# 连卡面单独一行「法术伤害+1」也收 —— 这是 §3「不许拿关键词打标」的显式例外：
# 不再区分有没有施动句、挂在谁身上（「双方玩家拥有法术伤害+2」也算）。
# 原本不收的「伤害翻倍」（倍数不是增量）已在审计 #14 按用户拍板收编为增益，
# 见 _doubles_output()；「能够以随从为目标」（改目标）已在审计 #7 收编，
# 见 _unlocks_target()。仍不收「变为/替换为『造成N点伤害』」—— 改写技能本身，
# #9 已改判单解+斩杀，仍不算增益。
_DAMAGE_BONUS_RES = tuple(re.compile(p) for p in (
    r"法术伤害\+\d+",
    r"英雄技能[^。，；]{0,10}伤害增加\d+点",
    r"英雄技能[^。，；]{0,8}额外造成\d+点伤害",
    r"伤害提高\d+点",
))


def _damage_bonus(card: Card) -> bool:
    """法术伤害 / 英雄技能 / 蔓足等「后续伤害 +N」算增益。"""
    clauses = _effect_clauses(plain_text(card))
    return any(rx.search(clause) for clause in clauses for rx in _DAMAGE_BONUS_RES)


# 「英雄技能能够以随从为目标」：解锁目标也是让后续效果变强（CONTEXT 审计 #7，用户拍板 B）。
# 用 plain_text 全串而非 _effect_clauses：任务线牌 SW_322 的「任务线：…奖励：…」
# 会被切句整句过滤成空列表，走从句口径会漏掉它。只认「英雄技能…能够以随从为目标」
# 这一句式，「尽可能以敌人为目标」（随机施放的目标偏好）、「还会以相邻的随从为目标」
# （额外目标）、「不能以英雄为目标」（限制）都不在此列。
_UNLOCK_TARGET_RE = re.compile(r"英雄技能[^。，；]{0,10}能够以随从为目标")


def _unlocks_target(card: Card) -> bool:
    """英雄技能解锁随从目标：改的是规则而非数值，但方向明确（技能变强），算增益。"""
    return bool(_UNLOCK_TARGET_RE.search(plain_text(card)))


# 「你的英雄免疫 / 无法受到伤害」：让己方英雄不被伤害也是变强（CONTEXT 审计 #10，
# 用户拍板 B2）。这是 §3「不许拿关键词打标」的显式例外 —— 关键词增益表里
# 故意没有「免疫」，这里只给**受益对象明确是「你的英雄」**的字面句开一道口子。
# 不收：「使一个友方野兽/随从获得免疫」（受益对象是随从）、「本随从获得免疫」、
# 「你的英雄在攻击时免疫」（「你的英雄」与「免疫」被条件隔开）、
# 奥秘的「使其…免疫」「在受到伤害后…免疫」（触发句，且按奥秘分支仍待定）。
# 用 _effect_clauses 而非全串：任务/升级提示句不算本牌效果。
_HERO_IMMUNE_RE = re.compile(r"你的英雄(?:免疫|无法受到伤害)")


def _hero_immune(card: Card) -> bool:
    """己方英雄免伤：方向明确（英雄不会被伤害），按增益打。"""
    return any(_HERO_IMMUNE_RE.search(clause)
               for clause in _effect_clauses(plain_text(card)))


# 「你的英雄拥有+关键词」也是给英雄贴光环，方向明确（英雄变强），按增益打
# （CONTEXT 审计 #12，用户拍板 B2）。三处旧判据都绕开了这个句式：
# #4 的 _OWNS_PLUS_RE 只认「拥有+N」数值、_BUFF_KEYWORD_RE 只认「获得…关键词」、
# rule_heal 明确排除含「拥有」的吸血句，于是 CATA_530 邪能灌魔成了 0 标签。
# 关键词表与 _BUFF_KEYWORD_RE 一致（免疫已由 #10 单独收编，不在这里重复开口子）。
# 只认受益对象明确是「你的英雄」：随从/机械/法术上的「拥有+关键词」不在此列，
# 那 64 张里还有 3 张 HERO，受 #8「英雄牌先不加标签」约束，按 B3 放开要另开一轮。
_HERO_OWNS_KEYWORD_RE = re.compile(
    r"你的英雄[^。，；]{0,6}拥有(?:吸血|嘲讽|圣盾|复生|风怒|潜行|突袭|冲锋|剧毒)"
)


def _hero_owns_keyword(card: Card) -> bool:
    """英雄光环关键词：受益对象明确，按增益打。"""
    return any(_HERO_OWNS_KEYWORD_RE.search(clause)
               for clause in _effect_clauses(plain_text(card)))


# 「法术施放两次」与「输出型伤害/治疗翻倍」都让后续效果变强，方向明确，按增益打
# （CONTEXT 审计 #14，用户拍板 C）。这推翻了 #5「倍数不是增量、不算增益」的边界：
# _BUFF_FIGURE_RE 早就收「攻击力/生命值/属性值翻倍」，唯独伤害/治疗这一支被 #5
# 点名排除（EX1_350 先知维伦 / GIL_646 发条自动机）；#14 一并收编。
# 不收：「受到…伤害翻倍」—— 可能让己方挨打更多，方向不明（诅咒之刃、量子反稳定机、
# 焦油飞溅等 12 张）；「圣物/本牌/发现并施放两次」等非「法术…施放两次」句式另开一轮。
_SPELL_TWICE_RE = re.compile(r"[^。，；]{0,14}法术[^。，；]{0,10}(?:将|会)?施放两次")
_OUTPUT_DOUBLE_RE = re.compile(r"(?:伤害|治疗效果?)[^。，；]{0,8}翻倍")
_TAKE_DAMAGE_RE = re.compile(r"受到[^。，；]{0,8}(?:的)?(?:所有)?伤害")


def _repeats_spell(card: Card) -> bool:
    """后续法术施放两次：效果次数增量，方向明确，按增益打。"""
    return any(_SPELL_TWICE_RE.search(clause)
               for clause in _effect_clauses(plain_text(card)))


def _doubles_output(card: Card) -> bool:
    """伤害/治疗效果翻倍：#5 曾按「倍数不是增量」排除，#14 收编为增益。

    「受到…伤害翻倍」方向不明（也可能让己方挨打更多），仍不收。
    """
    return any(_OUTPUT_DOUBLE_RE.search(clause) and not _TAKE_DAMAGE_RE.search(clause)
               for clause in _effect_clauses(plain_text(card)))


# 「摧毁/移除…牌库」让一方或双方损失资源，按减益处理（CONTEXT 审计 #6）。
# 原有判据只认「摧毁…(敌方|对手)」，漏了「双方」「你的」；按 #3 归属不决定标签，
# 削弱己方牌库也算减益。只认带「的」的归属写法：「摧毁你牌库中的一张牌」
# （BAR_911 灵魂撕裂）是附带代价，C 的边界故意不收，留给更宽的一轮。
_DECK_DESTROY_RE = re.compile(
    r"(?:摧毁|移除)[^。，；]{0,10}(?:双方|对手|敌方|你的)[^。，；]{0,6}牌库"
)


def _destroys_deck(card: Card) -> bool:
    """摧毁/移除牌库（含双方、自己的牌库）算减益。"""
    return any(_DECK_DESTROY_RE.search(clause) for clause in _effect_clauses(plain_text(card)))


# 「让对手减少/摧毁/移除法力水晶」统一算减益（CONTEXT 审计 #11，用户拍板 C）。
# 两种语序都收：「摧毁对手的两个法力水晶」「你的对手减少两个法力水晶」——
# 旧判据只认「摧毁…(敌方|对手)」，动词换成「减少」就整族漏掉。
# 只认对手/敌方：己方水晶的代价写法（恶魔卫士、疯入膏肓、法力裂解）不在此列。
# 4 张先例本来靠循环里的「摧毁…(敌方|对手)」通用判据命中，这里显式收编、
# 结果不变；通用判据还要管摧毁对手武器/手牌等，不能删。
_MANA_DRAIN_RE = re.compile(
    r"(?:减少|摧毁|移除)[^。，；]{0,8}(?:对手|敌方)[^。，；]{0,6}法力水晶"
    r"|(?:对手|敌方)[^。，；]{0,6}(?:减少|摧毁|移除)[^。，；]{0,6}法力水晶"
)


def _drains_opponent_crystals(card: Card) -> bool:
    """削对手法力水晶 = 限制对手后续能做的事，按减益打。"""
    return any(_MANA_DRAIN_RE.search(clause)
               for clause in _effect_clauses(plain_text(card)))


# 「选择一个友方随从，交给你的对手」按减益处理（CONTEXT 审计 #15，用户拍板 B1）。
# 宾语和动作被逗号拆到两个从句，_effect_clauses 按单句判不到，必须用 plain_text
# 全串匹配；正则刻意容忍逗号、但不跨句号/分号，避免把前后两句拼起来误命中。
# 只收「友方/一个…随从…交给(你的)对手」：把「牌」交给对手的
# JAIL_206 / JAM_036 / TRL_096 是送资源还是塞废牌，方向模糊，B2 的宽口径不收；
# 「获得/夺取控制权」（拥抱黑暗）方向相反，本就归单解，不在此列。
_HANDOVER_MINION_RE = re.compile(
    r"(?:友方|一个)[^。；]{0,12}随从[^。；]{0,12}交给(?:你的)?对手"
)


def _hands_over_friendly_minion(card: Card) -> bool:
    """把友方随从移交给对手：己方场面缩水、对手白得一个随从，按减益打。"""
    return bool(_HANDOVER_MINION_RE.search(plain_text(card)))


def rule_buff(card: Card) -> bool:
    """让身材/关键词变强：加身材、贴关键词、给手牌/牌库/武器/英雄加值（护甲除外）。"""
    if (_steals_figure(card) or _owns_plus(card) or _damage_bonus(card)
            or _unlocks_target(card) or _hero_immune(card)
            or _hero_owns_keyword(card) or _repeats_spell(card)
            or _doubles_output(card)):
        return True
    for clause in _effect_clauses(plain_text(card)):
        if (_BUFF_GAIN_RE.search(clause) or _BUFF_KEYWORD_RE.search(clause)
                or _BUFF_FIGURE_RE.search(clause) or _BUFF_WEAPON_RE.search(clause)
                or _BUFF_ATTR_RE.search(clause)):
            return True
    return False


def rule_debuff(card: Card) -> bool:
    """削弱或限制：减攻/换攻/沉默/冻结/加费/弃牌/摧毁牌库。

    冻结、沉默不论单体群体都归这里；但「被冻结」「无法被冻结」是状态/免疫，
    「冻结敌人后」是触发条件，都不是本牌施加的效果。
    """
    text = plain_text(card)
    # 偷取身材、摧毁牌库、削对方法力水晶、移交友方随从都有一方变弱，
    # 归属不影响方向（CONTEXT 审计 #3/#6/#11/#15）；放在循环前，免得被下面
    # 「友方从句跳过」的保守分支拦掉 —— 实测 21 张牌库摧毁里有 14 张
    # 会被 _is_own_clause 误拦，变节整句也带「友方」。
    if (_steals_figure(card) or _destroys_deck(card)
            or _drains_opponent_crystals(card)
            or _hands_over_friendly_minion(card)):
        return True
    # 「检视对手手牌并秘密选择一张 / 亡语：弃掉所选的牌」把宾语拆到两句，
    # 单句判不到；整牌同时命中这两段才算减益（弃自己的手牌不算）。
    if (re.search(r"检视[^。，；]{0,20}(?:对手|敌方)[^。，；]{0,10}手牌", text)
            and "弃掉所选" in text):
        return True
    for clause in _effect_clauses(text):
        frozen = clause
        for word in ("无法被冻结", "已被冻结", "被冻结", "冻结的"):
            frozen = frozen.replace(word, "")
        if "冻结" in frozen:
            # 冻结敌人才算减益；「冻结本随从/你的随从」是代价
            if not re.search(r"冻结(?:本随从|你的|友方)", clause) and not re.search(
                r"冻结[^。，；]{0,6}后", clause
            ):
                return True
        silenced = clause
        for word in ("无法被沉默", "被沉默", "沉默的"):
            silenced = silenced.replace(word, "")
        if "沉默" in silenced and not re.search(r"沉默(?:本随从|你的|友方)", clause):
            return True
        if _is_own_clause(clause):
            continue
        if "获得-" in clause:  # 获得-2攻击力 / -2/-2
            return True
        # 英雄生命值「变为 15」可加可减，拿不准的不硬判
        if "英雄" not in clause and re.search(r"(攻击力|生命值)[^。，；]{0,4}(变为|降低|减半)", clause):
            return True
        if "法力值消耗增加" in clause and any(w in clause for w in ("敌方", "对手")):
            return True
        if ("攻击力" in clause and "生命值" in clause
                and re.search(r"(互换|交换)", clause)):
            return True
        # 只针对敌方的摧毁/弃牌才算减益（「摧毁礼物收据」这种不是）
        if re.search(r"摧毁[^。，；]{0,8}(敌方|对手)", clause):
            return True
        if re.search(r"(对手|敌方)[^。，；]{0,6}弃", clause):
            return True
    return False


def rule_heal(card: Card) -> bool:
    """恢复生命值；吸血也算（但「使随从获得吸血」是增益，不是本牌治疗）。"""
    if card.has("LIFESTEAL"):
        return True
    for clause in _effect_clauses(plain_text(card)):
        if "吸血" in clause and not any(w in clause for w in ("获得", "具有", "拥有")):
            return True
        if not re.search(r"恢复[^。，；]{0,16}生命值", clause):
            # 「恢复你的英雄在本回合中受到的所有伤害」也是一种治疗
            if re.search(r"恢复[^。，；]{0,20}受到[^。，；]{0,10}伤害", clause):
                return True
            continue
        # 「你的恢复生命值的牌和技能改为造成伤害」是在说治疗牌，不是本牌治疗
        if re.search(r"恢复生命值的(牌|技能|效果|法术|随从)", clause):
            continue
        # 「当你恢复生命值后」「任务：恢复 15 点」是触发/任务条件
        if "恢复过" in clause or re.search(r"(当你|如果你|每当你)[^。，；]{0,8}恢复", clause):
            continue
        return True
    return False


def rule_armor(card: Card) -> bool:
    """获得护甲值。只「提到护甲」的条件牌不算（裂盾一击/加尔范上尉）。"""
    for clause in _effect_clauses(plain_text(card)):
        armor = (
            re.search(r"获得(?:\d+点?护甲|等同于[^。，；]{0,16}护甲)", clause)
            or re.search(r"获得[^。，；]{0,12}?(?:等量|双倍|两倍|三倍)的?护甲", clause)
            or "转化为护甲" in clause
            or re.search(r"获得(?!的)[^。，；]{0,10}?攻击力和护甲", clause)
        )
        if not armor:
            continue
        if re.search(r"护甲[^。，；]{0,6}后", clause):  # 「获得3点护甲值后升级」
            continue
        return True
    return False


def rule_resource(card: Card) -> bool:
    """非抽牌式补牌 / 费用优势：发现、随机获得、复制、找牌、跳费、减费。"""
    if card.has("DISCOVER"):
        return True
    if _steal_to_hand(card):  # 把敌方随从抢进手牌，也是补牌
        return True
    for clause in _effect_clauses(plain_text(card)):
        if "发现" in clause:
            return True
        if re.search(r"(获得|获取)[^。，；]{0,10}?(?:一|两|三|四|五|六|几|\d+)张", clause):
            return True
        if re.search(r"置入[^。，；]{0,6}你的手牌", clause):
            return True
        if "复制" in clause and "召唤" not in clause and not any(
            w in clause for w in ("消灭", "摧毁")
        ):
            return True
        if re.search(r"(获得|复原|增加)[^。，；]{0,10}法力水晶", clause):
            return True
        if "法力值上限" in clause:
            return True
        if "法力值消耗减少" in clause and "本牌" not in clause:
            return True
        if re.search(r"法力值消耗变为（[01]）", clause):
            return True
        if re.search(r"从[^。，；]{0,6}牌库中(?:选择|找|施放)", clause):
            return True
        if "洗入" in clause and "你的牌库" in clause and "手牌" not in clause:
            return True
    return False


def rule_draw(card: Card) -> bool:
    """从牌库抽牌。触发「每当你抽到…」不是本牌抽牌；只给对手抽也不算。"""
    for clause in _effect_clauses(plain_text(card)):
        if "抽" not in clause or "牌" not in clause:
            continue
        if "抽到" in clause or "每当" in clause:
            continue
        if re.search(r"抽[^。，；]{0,8}牌后", clause):
            continue
        if re.search(r"对手[^。，；]{0,8}抽", clause):
            continue
        return True
    return False


def _explicit_face_damage(text: str) -> bool:
    """文本里明确写了打敌方英雄/每个英雄/所有敌人（不是「可能选中」）。"""
    for clause in _effect_clauses(text):
        if "造成" not in clause or "伤害" not in clause:
            continue
        if not _is_damage_effect(clause):
            continue
        for m in _DMG_TARGET_RE.finditer(clause):
            phrase = m.group(1)
            if _is_own_target(phrase):
                continue
            if re.search(r"(英雄|你的对手|所有敌人|所有角色|所有其他角色|所有其他敌人)", phrase):
                return True
        if "分配" in clause:
            am = re.search(r"分配(?:到|给|至)([^。，；]{0,22})", clause)
            target = am.group(1) if am else clause
            if not _is_own_target(target) and _damage_target(target)[2]:
                return True
    return False


def rule_lethal(card: Card) -> bool:
    """能直接对敌方英雄造成伤害：直伤法术、冲锋、武器、英雄加攻。

    突袭不算；随从的普通战吼直伤只算单解（CONTEXT §6：军情七处特工），
    但明确写「对敌方英雄/每个英雄造成伤害」的随从效果算斩杀。
    英雄技能被替换成造成伤害时，引号内效果按普通伤害牌算斩杀（审计 #9）。
    """
    if _hp_replacement_damage(card)[1]:
        return True
    text = plain_text(card)
    if card.type == "WEAPON":
        return card.atk > 0 and "无法攻击英雄" not in text
    if card.type == "MINION" and card.has("CHARGE"):
        return card.atk > 0 and "无法攻击英雄" not in text
    for clause in _effect_clauses(text):
        if "消灭敌方英雄" in clause:
            return True
        if "每当" in clause:
            continue
        if re.search(r"获得[^。，；]{0,8}攻击力[后时]", clause):
            continue  # 「在英雄获得攻击力后」是触发条件
        if re.search(r"英雄[^。，；]{0,10}获得(?!的)[^。，；]{0,8}攻击力", clause):
            return True
    if _explicit_face_damage(text):
        return True
    if card.type != "SPELL":
        return False
    return _damage_hits(text)[2]


# ---------------------------------------------------------------- 未知（局面依赖）
# 效果「动作本身」依赖本局历史或当前场面，静态看卡面判不出来：触发亡语、
# 重放本局用过的牌、延迟触发、与对手交换、按选中随从复制身材。
# 普通「若…则…」条件牌不归这里——它们的效果动作是确定的，只是发生与否看场面。
_UNKNOWN_RES = (
    re.compile(r"触发[^。，；]{0,30}亡语"),
    re.compile(r"(再次施放|再次使用|重新使用|重放)[^。，；]{0,30}(本局对战|使用过|施放过)"),
    re.compile(r"\d+回合后"),
    re.compile(r"(?:与[^。，；]{0,6}对手[^。，；]{0,8}交换|交换[^。，；]{0,10}(?:手牌|随从|牌库|英雄技能))"),
    re.compile(r"(?:属性值|攻击力|生命值)[^。，；]{0,8}变为与[^。，；]{0,10}相同"),
    # 「唤起两种元素之力」「选择并释放…项灾变」是引用句，动作写在关联牌里
    re.compile(r"(?:唤起|选择并释放)[^。，；]{0,20}(?:之力|项)"),
    # 「对敌人施放…法术」由系统按预算选牌；写宽了会吃进「每当你施放一个法术」的触发句
    re.compile(r"对(?:敌人|敌方|所有敌人)[^。，；]{0,10}施放[^。，；]{0,24}法术"),
)


def rule_unknown(card: Card) -> bool:
    """效果动作依赖本局历史/当前场面，或卡面只是引用句，单看卡面定不了它会做什么。"""
    for clause in _effect_clauses(plain_text(card)):
        if any(p.search(clause) for p in _UNKNOWN_RES):
            return True
    return False


RULES = {
    "单解": rule_single_removal,
    "群解": rule_aoe_removal,
    "站场": rule_board,
    "增益": rule_buff,
    "减益": rule_debuff,
    "治疗": rule_heal,
    "护甲": rule_armor,
    "资源": rule_resource,
    "抽牌": rule_draw,
    "斩杀": rule_lethal,
    "未知": rule_unknown,
}


def _tags(card: Card) -> tuple[str, ...]:
    return tuple(tag for tag in TAG_ORDER if RULES[tag](card))


_PENDING_MARKERS = ("抉择", "随机", "复制", "触发")


def pending_reason(card: Card) -> str | None:
    """规则拿不准时的原因（人话）；None 表示规则已给出可信结果。

    必进待定的：奥秘牌本体（SECRET 机制，效果取决于对手动作，规则不硬猜）；
    规则一个标签都打不出来、但卡面确实有字的牌（含「抉择/随机/复制/触发」时说明可能漏了分支）。
    只在文本里「提到奥秘」的牌（秘法学家、驯羊师）不算，它们已有确定标签。
    """
    text = plain_text(card)
    if card.has("SECRET"):
        return "奥秘：效果取决于对手动作，规则不硬猜"
    if not text or _tags(card):
        return None
    for marker in _PENDING_MARKERS:
        if marker in text:
            return f"文本含「{marker}」，规则未命中任何标签，可能有分支漏标"
    return "规则未命中任何标签，卡面文本需要人工判断"


__all__ = [
    "TAG_ORDER",
    "plain_text",
    "rule_single_removal",
    "rule_aoe_removal",
    "rule_board",
    "rule_buff",
    "rule_debuff",
    "rule_heal",
    "rule_armor",
    "rule_resource",
    "rule_draw",
    "rule_lethal",
    "rule_unknown",
    "RULES",
    "pending_reason",
]
