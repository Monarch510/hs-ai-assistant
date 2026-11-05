"""Effect 三处一致性：字段 / parse_effect / apply_effect。

改卡牌效果时最怕「加了一个字段，解析或结算漏一处」—— 不会报错，
只会静默少结算。这里用表驱动把每条文本规则的解析与结算一起钉住。

不读 data/：卡牌对象直接构造，效果表在用例里换成空表，
只用内置文本规则，规则表的变化不会让这批用例忽绿忽红。
"""

from __future__ import annotations

import unittest
from unittest import mock

from hsai.cards import Card
from hsai import model
from hsai.model import Board, Effect, SideState, Unit


def _card(text: str, **kw) -> Card:
    name = kw.pop("name", "测试牌")
    return Card(id="T_EFFECT", name=name, type=kw.pop("type", "SPELL"), text=text, **kw)


def _unit(name: str, atk: int = 2, hp: int = 5, **kw) -> Unit:
    return Unit(card_id="T_UNIT", name=name, atk=atk, hp=hp, **kw)


def _board(me: SideState | None = None, foe: SideState | None = None) -> tuple[Board, SideState, SideState]:
    a = me or SideState()
    b = foe or SideState()
    return Board([a, b], 5, 0, cards=None), a, b


# text -> {字段: 期望值}。每行都对应一类结算，见下面的独立用例。
PARSE_CASES = [
    ("造成3点伤害", {"damage": 3, "scope": "target"}),
    ("对所有敌方随从造成2点伤害", {"damage": 2, "scope": "enemy_minions"}),
    ("对所有随从造成2点伤害", {"damage": 2, "scope": "all_minions", "aoe": True}),
    ("对敌方英雄造成3点伤害", {"damage": 3, "scope": "enemy_hero"}),
    ("随机对一个敌人造成3点伤害", {"damage": 3, "scope": "random_enemy"}),
    ("消灭一个随从", {"destroy": True, "scope": "target"}),
    ("消灭所有攻击力小于3的随从", {"destroy": True, "scope": "all_minions", "min_atk": 3}),
    ("恢复5点生命值", {"heal": 5}),
    ("获得5点护甲", {"armor": 5}),
    ("冻结一个随从", {"freeze": True}),
    ("召唤两个1/1的僵尸", {"summon_atk": 1, "summon_hp": 1, "summon_count": 2}),
    ("使一个随从获得+2/+3", {"buff_atk": 2, "buff_hp": 3}),
    ("抽两张牌", {"draw": 2}),
    ("将所有随从的生命值变为1", {"set_hp": 1}),
]


class EffectConsistencyTest(unittest.TestCase):
    def setUp(self) -> None:
        # 效果表来自 data/effects.json；这里只用内置规则，避免测试依赖本机数据
        patcher = mock.patch.object(model, "_EFFECT_TABLE", {"keywords": {}, "patterns": []})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_parse_effect_table(self) -> None:
        """14 类效果的卡面文本都必须解出对应字段。"""
        for text, expected in PARSE_CASES:
            with self.subTest(text=text):
                eff = model.parse_effect(_card(text))
                for key, value in expected.items():
                    self.assertEqual(getattr(eff, key), value, f"{text} 的 {key}")
                self.assertTrue(eff.known, f"{text} 不该被判成未建模")

    # ------------------------------------------------------------ 伤害
    def test_single_target_damage(self) -> None:
        eff = model.parse_effect(_card("造成3点伤害"))
        board, me, foe = _board()
        victim = _unit("敌方随从")
        mine = _unit("我方随从")
        foe.board.append(victim)
        me.board.append(mine)
        board.apply_effect(eff, me, victim)
        self.assertEqual(victim.hp, 2)
        self.assertEqual(mine.hp, 5)

    def test_single_target_damage_on_hero_includes_spell_damage(self) -> None:
        eff = model.parse_effect(_card("造成3点伤害"))
        board, me, foe = _board()
        me.board.append(_unit("法伤随从", spell_damage=2))
        hero = Unit(card_id="HERO", name="敌方英雄", atk=0, hp=30)
        foe.board.append(hero)
        board.apply_effect(eff, me, hero)
        self.assertEqual(foe.hero_hp, 25)

    def test_enemy_minions_damage(self) -> None:
        eff = model.parse_effect(_card("对所有敌方随从造成2点伤害"))
        board, me, foe = _board()
        a, b, mine = _unit("敌1"), _unit("敌2"), _unit("我方")
        foe.board.extend([a, b])
        me.board.append(mine)
        board.apply_effect(eff, me, None)
        self.assertEqual((a.hp, b.hp, mine.hp), (3, 3, 5))

    def test_all_minions_damage_is_aoe(self) -> None:
        eff = model.parse_effect(_card("对所有随从造成2点伤害"))
        self.assertTrue(eff.aoe)
        board, me, foe = _board()
        mine, theirs = _unit("我方"), _unit("敌方")
        me.board.append(mine)
        foe.board.append(theirs)
        board.apply_effect(eff, me, None)
        self.assertEqual((mine.hp, theirs.hp), (3, 3))

    def test_enemy_hero_scope_only_hits_hero(self) -> None:
        eff = model.parse_effect(_card("对敌方英雄造成3点伤害"))
        board, me, foe = _board()
        theirs = _unit("敌方随从")
        foe.board.append(theirs)
        board.apply_effect(eff, me, None)
        self.assertEqual(foe.hero_hp, 27)
        self.assertEqual(theirs.hp, 5)

    def test_random_enemy_hits_minion_then_face(self) -> None:
        eff = model.parse_effect(_card("随机对一个敌人造成3点伤害"))
        board, me, foe = _board()
        theirs = _unit("敌方随从")
        foe.board.append(theirs)
        board.apply_effect(eff, me, None)
        self.assertEqual(theirs.hp, 2)
        foe.board.clear()
        board.apply_effect(eff, me, None)
        self.assertEqual(foe.hero_hp, 27)

    # ------------------------------------------------------------ 消灭
    def test_destroy_target(self) -> None:
        eff = model.parse_effect(_card("消灭一个随从"))
        board, me, foe = _board()
        victim = _unit("敌方随从")
        foe.board.append(victim)
        board.apply_effect(eff, me, victim)
        self.assertFalse(victim.alive)
        self.assertNotIn(victim, foe.board)

    def test_destroy_all_minions_keeps_low_attack(self) -> None:
        eff = model.parse_effect(_card("消灭所有攻击力小于3的随从"))
        self.assertEqual(eff.min_atk, 3)
        board, me, foe = _board()
        low_mine, high_mine = _unit("我方1攻", 1), _unit("我方4攻", 4)
        low_foe, high_foe = _unit("敌方2攻", 2), _unit("敌方5攻", 5)
        me.board.extend([low_mine, high_mine])
        foe.board.extend([low_foe, high_foe])
        board.apply_effect(eff, me, None)
        self.assertEqual([u.alive for u in (low_mine, high_mine, low_foe, high_foe)],
                         [True, False, True, False])

    # ------------------------------------------------------------ 恢复 / 护甲
    def test_heal_friendly_minion_and_hero_cap(self) -> None:
        eff = model.parse_effect(_card("恢复5点生命值"))
        board, me, foe = _board(me=SideState(hero_hp=28))
        wounded = _unit("受伤随从", hp=3)
        wounded.max_hp = 8
        me.board.append(wounded)
        board.apply_effect(eff, me, wounded)
        self.assertEqual(wounded.hp, 8)          # 不超过 max_hp
        hero = Unit(card_id="HERO", name="我方英雄", atk=0, hp=28)
        board.apply_effect(eff, me, hero)
        self.assertEqual(me.hero_hp, 30)         # 英雄回血封顶 30

    def test_armor_is_applied(self) -> None:
        """armor 只在亡语里结算过；普通法术/技能拿到护甲会被静默丢掉。"""
        eff = model.parse_effect(_card("获得5点护甲"))
        self.assertEqual(eff.armor, 5)
        board, me, foe = _board()
        board.apply_effect(eff, me, None)
        self.assertEqual(me.armor, 5)

    # ------------------------------------------------------------ 其它效果
    def test_freeze_target(self) -> None:
        eff = model.parse_effect(_card("冻结一个随从"))
        board, me, foe = _board()
        victim = _unit("敌方随从")
        foe.board.append(victim)
        board.apply_effect(eff, me, victim)
        self.assertTrue(victim.frozen)
        self.assertFalse(victim.can_attack_now)

    def test_summon_count_and_stats(self) -> None:
        eff = model.parse_effect(_card("召唤两个1/1的僵尸"))
        board, me, foe = _board()
        board.apply_effect(eff, me, None)
        self.assertEqual(len(me.board), 2)
        self.assertEqual([(u.atk, u.hp) for u in me.board], [(1, 1), (1, 1)])
        self.assertEqual(len(foe.board), 0)

    def test_buff_target(self) -> None:
        eff = model.parse_effect(_card("使一个随从获得+2/+3"))
        board, me, foe = _board()
        target = _unit("我方随从")
        me.board.append(target)
        board.apply_effect(eff, me, target)
        self.assertEqual((target.atk, target.hp, target.max_hp), (4, 8, 8))

    def test_draw_and_empty_deck_fatigue(self) -> None:
        eff = model.parse_effect(_card("抽两张牌"))
        board, me, foe = _board()
        me.deck = [_card("抽牌", name=f"第{i}张") for i in range(2)]
        board.apply_effect(eff, me, None)
        self.assertEqual((len(me.hand), len(me.deck)), (2, 0))
        board.apply_effect(eff, me, None)
        self.assertEqual(me.fatigue, 2)
        self.assertEqual(me.hero_hp, 27)         # 疲劳 1 + 2

    def test_set_hp_both_sides(self) -> None:
        eff = model.parse_effect(_card("将所有随从的生命值变为1"))
        board, me, foe = _board()
        mine, theirs = _unit("我方", hp=9), _unit("敌方", hp=7)
        me.board.append(mine)
        foe.board.append(theirs)
        board.apply_effect(eff, me, None)
        self.assertEqual((mine.hp, theirs.hp), (1, 1))

    def test_unknown_effect_does_not_crash(self) -> None:
        """认不出的牌会被建议引擎降权，结算时不能崩。"""
        board, me, foe = _board()
        board.apply_effect(Effect(known=False), me, None)
        self.assertEqual((me.hero_hp, me.armor, len(me.board), len(me.hand)), (30, 0, 0, 0))

    # ------------------------------------------------------------ 僵尸权重
    def test_lethal_bonus_weight_is_ignored(self) -> None:
        """DEFAULT_WEIGHTS 里没有 lethal_bonus，读它只会让分数随外部权重漂移。"""
        board, me, foe = _board(me=SideState(hero_hp=10))
        foe.board.append(_unit("打手", atk=12))
        self.assertGreaterEqual(foe.board_attack, me.hero_hp + me.armor)
        self.assertEqual(model.evaluate(board, {"lethal_bonus": 0}, perspective=0),
                         model.evaluate(board, {"lethal_bonus": 100}, perspective=0))

    def test_defensive_step_remains_and_quiet_board_is_unchanged(self) -> None:
        quiet, me, foe = _board()
        self.assertLess(foe.board_attack, me.hero_hp + me.armor)
        self.assertEqual(model.evaluate(quiet, {"lethal_bonus": 0}),
                         model.evaluate(quiet, {"lethal_bonus": 100}))
        self.assertEqual(model.evaluate(quiet), model.evaluate(quiet, {"lethal_bonus": 100}))
        # 防守阶跃还在：把对手攻击力抬到斩杀线，分数要掉一大截
        hit, me2, foe2 = _board(me=SideState(hero_hp=10))
        foe2.board.append(_unit("打手", atk=10))
        safe, _, foe3 = _board(me=SideState(hero_hp=10))
        foe3.board.append(_unit("打手", atk=9))
        self.assertLess(model.evaluate(hit) - model.evaluate(safe), -400)

    def test_default_weights_are_unchanged(self) -> None:
        self.assertEqual(sorted(model.DEFAULT_WEIGHTS),
                         ["aoe_risk", "face", "fatigue", "hand", "health", "taunt", "tempo"])
        self.assertEqual(len(model.DEFAULT_WEIGHTS), 7)


if __name__ == "__main__":
    unittest.main()
