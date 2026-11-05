"""克苏恩四技能结算：数值以卡牌库 BE_013t* 的文本为准，集齐四个后四倍。

旧口径是 7 点 / +2/+3 / 两个 2/3 / ×3；卡牌库调整后是
6(24) / +2/+2(+8/+8) / 5(20) / 两个 2/2(8/8) / 四倍。
特别地「仆从」强化的是身材、不是个数：文本仍然只召唤两个。
"""

import pathlib
import unittest

from hsai import model, policy
from hsai.cards import Card


def _board(hero_hp: int = 30):
    me = model.SideState(hero_hp=hero_hp)
    foe = model.SideState(hero_hp=30)
    return model.Board([me, foe], 5, 0), me, foe


def _unit(name: str = "随从", atk: int = 3, hp: int = 8) -> model.Unit:
    return model.Unit(card_id="", name=name, atk=atk, hp=hp)


class CthunSkillValuesTest(unittest.TestCase):
    def test_boost_multiplier_is_four(self) -> None:
        self.assertEqual(policy.CTHUN_MULTIPLIER, 4)

    def test_rage_six_or_twenty_four(self) -> None:
        for boosted, dmg in ((False, 6), (True, 24)):
            with self.subTest(boosted=boosted):
                b, _, foe = _board()
                why = policy.apply_cthun_skill(b, 0, "克苏恩的愤怒", tripled=boosted)
                self.assertEqual(foe.hero_hp, 30 - dmg)
                self.assertIn(f"{dmg} 点", why)

    def test_gaze_five_or_twenty_with_lifesteal(self) -> None:
        for boosted, dmg in ((False, 5), (True, 20)):
            with self.subTest(boosted=boosted):
                b, me, foe = _board(hero_hp=20)
                target = _unit("大随从", atk=8, hp=30)
                foe.board.append(target)
                why = policy.apply_cthun_skill(b, 0, "克苏恩的凝视", tripled=boosted)
                self.assertEqual(target.hp, 30 - dmg)
                self.assertEqual(me.hero_hp, 20 + dmg)
                self.assertIn(f"{dmg} 点", why)

    def test_domain_two_two_or_eight_eight(self) -> None:
        for boosted, (da, dh) in ((False, (2, 2)), (True, (8, 8))):
            with self.subTest(boosted=boosted):
                b, me, _ = _board()
                units = [_unit("A", 1, 4), _unit("B", 2, 5)]
                me.board.extend(units)
                why = policy.apply_cthun_skill(b, 0, "克苏恩的领域", tripled=boosted)
                self.assertEqual([(u.atk, u.hp) for u in units],
                                 [(1 + da, 4 + dh), (2 + da, 5 + dh)])
                self.assertIn(f"+{da}/+{dh}", why)

    def test_servant_scales_stats_not_count(self) -> None:
        for boosted, stat in ((False, (2, 2)), (True, (8, 8))):
            with self.subTest(boosted=boosted):
                b, me, _ = _board()
                why = policy.apply_cthun_skill(b, 0, "克苏恩的仆从", tripled=boosted)
                self.assertEqual(len(me.board), 2, "强化只放大身材，数量始终是两个")
                for u in me.board:
                    self.assertEqual((u.atk, u.hp), stat)
                    self.assertTrue(u.taunt)
                self.assertIn("2 个", why)

    def test_choice_score_uses_same_multiplier(self) -> None:
        # 打分是另一处读倍数的代码，漏改就会出现「模拟里 8/8、理由里 6/6」
        b, _, _ = _board()
        card = Card(id="BE_013t4", name="克苏恩的仆从", cost=2, type="SPELL",
                    text="召唤两个2/2并具有嘲讽的狂热者。")
        eff = model.Effect(known=True, summon_atk=2, summon_hp=2, summon_count=2)
        _, why = policy.score_choice(card, eff, b, 0, 2, tripled=True)
        self.assertIn("8/8", why)

    def test_no_stale_times_three_wording(self) -> None:
        stale = "×" + "3"
        for name in ("README.md", "hsai/ui.py", "hsai/state.py",
                     "hsai/policy.py", "tests/test_cthun_progress.py"):
            with self.subTest(name=name):
                src = pathlib.Path(name).read_text(encoding="utf-8")
                self.assertNotIn(stale, src, f"{name} 还写着旧的倍数")
                self.assertNotIn("mult = 3", src, f"{name} 还有硬编码的 3 倍")


if __name__ == "__main__":
    unittest.main()
