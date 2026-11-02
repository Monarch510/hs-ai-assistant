"""卡牌文本分类：这张牌「算什么」——清场？单解？嘲讽？治疗？

为什么单独一个模块
------------------
这套判断是**卡牌知识**，不是哪个算法的内部实现。预判（infer）要用它算
「对面手上有没有清场」，留牌（mulligan）要用它算「起手该不该留解牌」。
放在 infer 里、让 mulligan 去 import 私有函数，等于让留牌层依赖预判层的
内部细节：infer 一改判定方式，留牌行为会跟着悄悄变，而没人会想到去测留牌。

所以这里只放「读卡牌文本 → 打标签」的纯函数，不依赖任何上层模块
（只依赖 cards.Card），谁都可以安全引用。

判定口径（保守优先，宁可漏判也不要重复计数）
--------------------------------------------
· 清场：法术 + 「所有」且（伤害 或 消灭），或「全场」
  —— 「消灭所有随从」也算清场，不只是伤害型
· 单解：法术 + 「消灭」或「造成X点伤害」，但**排除清场**
  —— 否则同一张牌会同时算进 aoe_risk 和 removal_risk，界面上两行互相矛盾
"""

from __future__ import annotations

from .cards import Card


def is_aoe(card: Card) -> bool:
    """群体清场：能一次性处理对面多个随从。"""
    if card.type != "SPELL" or not card.text:
        return False
    t = card.text
    # 清场不一定造成伤害：「消灭所有随从」（如绝命乱斗）同样是清场
    return ("所有" in t and ("伤害" in t or "消灭" in t)) or "全场" in t


def is_single_removal(card: Card) -> bool:
    """单体去除：只解决一个威胁。清场不算单解。"""
    if card.type != "SPELL" or not card.text:
        return False
    t = card.text
    # 「消灭所有随从」是清场，不是单解 —— 否则同一张牌会同时被算进
    # aoe_risk 和 removal_risk，界面上两行互相矛盾
    if "所有" in t:
        return False
    return "消灭" in t or ("造成" in t and "点伤害" in t)


def is_taunt(card: Card) -> bool:
    """嘲讽：影响对面能不能直接打脸。"""
    return card.has("TAUNT")


def is_heal(card: Card) -> bool:
    """回血或叠甲：影响对面的斩杀线估算。"""
    t = card.text or ""
    return ("恢复" in t and "生命值" in t) or "护甲" in t
