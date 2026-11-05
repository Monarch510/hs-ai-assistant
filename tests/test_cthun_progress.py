"""克苏恩技能计数：任何触发的技能都算用掉一次，四个各一次才翻四倍。"""

from __future__ import annotations

import unittest

from hsai import cards, powerlog, state


class CthunProgressTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = cards.load()
        self.st = state.GameState(self.db)
        self.st.our_id = 1

    def _send(self, card_id: str, src_card: str = "BE_014") -> None:
        self.st._choice_source = {"id": 55, "card_id": src_card}
        self.st._on_choice_sent(
            powerlog.Event("choice_sent", {"refs": [{"card_id": card_id}]}, "")
        )

    def test_any_source_counts(self) -> None:
        # 子嗣触发的技能也算用掉：本体之后才可能凑满四个
        self._send("BE_013t1")
        self.assertEqual(self.st.cthun_used, {"克苏恩的愤怒"})

    def test_same_skill_twice_counts_once(self) -> None:
        self._send("BE_013t1")
        self._send("BE_013t1")
        self.assertEqual(len(self.st.cthun_used), 1)

    def test_tripled_only_after_four_distinct(self) -> None:
        for cid in ("BE_013t1", "BE_013t2", "BE_013t3"):
            self._send(cid)
        self.assertFalse(self.st.cthun_view()["tripled"])
        self._send("BE_013t4")
        view = self.st.cthun_view()
        self.assertEqual(view["count"], 4)
        self.assertTrue(view["tripled"])

    def test_reset_clears_progress(self) -> None:
        self._send("BE_013t1")
        self.st.reset()
        self.assertEqual(self.st.cthun_used, set())


class CloneKeepsStateTest(unittest.TestCase):
    """模拟会 clone 局面：以前手写字段列表漏掉的属性必须还在。"""

    def test_hero_attack_survives_clone(self) -> None:
        from hsai import model

        s = model.SideState(hero_hp=20, hero_atk=3, hero_attacks_left=1)
        c = s.clone()
        self.assertEqual(c.hero_atk, 3, "英雄攻击力不能被 clone 丢掉")
        self.assertEqual(c.hero_attacks_left, 1)

    def test_per_unit_skill_set_survives_clone(self) -> None:
        from hsai import model

        s = model.SideState(hero_hp=20)
        s._cthun_by_unit = {"cthun#0": {"克苏恩的愤怒"}}
        c = s.clone()
        self.assertEqual(c._cthun_by_unit, {"cthun#0": {"克苏恩的愤怒"}})
        c._cthun_by_unit["cthun#0"].add("克苏恩的领域")
        self.assertEqual(s._cthun_by_unit["cthun#0"], {"克苏恩的愤怒"},
                         "副本改了不能影响原局面")


if __name__ == "__main__":
    unittest.main()
