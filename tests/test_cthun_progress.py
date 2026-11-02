"""克苏恩强化规则：只有本体触发才计数，且四个技能各用一次才算强化。"""

from __future__ import annotations

import unittest

from hsai import cards, powerlog, state


class CthunProgressTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = cards.load()
        self.st = state.GameState(self.db)
        self.st.our_id = 1

    def _send(self, src_id: int, src_card: str, card_id: str) -> None:
        self.st._choice_source = {"id": src_id, "card_id": src_card}
        ev = powerlog.Event("choice_sent", {"refs": [{"card_id": card_id}]}, "")
        self.st._on_choice_sent(ev)

    def test_non_cthun_trigger_never_counts(self) -> None:
        # 子嗣/法术触发：任何时候都能四选一，也不推进强化
        self._send(55, "BE_014", "BE_013t1")
        self.assertEqual(self.st.cthun_by_source.get(55), None)

    def test_cthun_trigger_counts_per_instance(self) -> None:
        self._send(77, "BE_013", "BE_013t1")
        self.assertEqual(self.st.cthun_by_source[77], {"克苏恩的愤怒"})
        # 另一只克苏恩独立计数
        self._send(88, "BE_013", "BE_013t2")
        self.assertEqual(self.st.cthun_by_source[88], {"克苏恩的领域"})
        self.assertEqual(len(self.st.cthun_by_source[77]), 1)

    def test_tripled_only_after_four_distinct(self) -> None:
        for cid in ("BE_013t1", "BE_013t2", "BE_013t3"):
            self._send(77, "BE_013", cid)
        self.st._choice_source = {"id": 77, "card_id": "BE_013"}
        self.assertFalse(self.st.cthun_view()["tripled"])
        self._send(77, "BE_013", "BE_013t4")
        self.st._choice_source = {"id": 77, "card_id": "BE_013"}
        view = self.st.cthun_view()
        self.assertEqual(view["count"], 4)
        self.assertTrue(view["tripled"])

    def test_reset_clears_progress(self) -> None:
        self._send(77, "BE_013", "BE_013t1")
        self.st.reset()
        self.assertEqual(self.st.cthun_by_source, {})


if __name__ == "__main__":
    unittest.main()
