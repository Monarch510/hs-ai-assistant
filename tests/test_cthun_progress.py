"""克苏恩技能计数：任何触发的技能都算用掉一次，四个各一次才翻 3 倍。"""

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


if __name__ == "__main__":
    unittest.main()
