"""实体引用解析的回归测试。

根因背景：实体的 entityName 里可能再嵌一层方括号
（`entityName=UNKNOWN ENTITY [cardType=INVALID]`），
用非贪婪正则会在内层 `]` 处提前截断，导致：
  · 起手手牌只读到一部分
  · 发现（三选一）只解析出一个选项
所以这里锁住「括号配平」这个行为。
"""

from __future__ import annotations

import unittest

from hsai import powerlog


class BracketRefTest(unittest.TestCase):
    def test_three_options_with_nested_name(self) -> None:
        body = (
            "  Entities[0]=[entityName=小巧玩具 id=168 zone=SETASIDE zonePos=0 cardId=SW_034 player=1]"
            "  Entities[1]=[entityName=星空光环 id=169 zone=SETASIDE zonePos=0 cardId=GDB_140 player=1]"
            "  Entities[2]=[entityName=UNKNOWN ENTITY [cardType=INVALID] id=170 zone=SETASIDE"
            " zonePos=0 cardId=REV_240 player=1]"
        )
        refs = powerlog._all_refs(body)
        self.assertEqual(len(refs), 3)
        self.assertEqual([r["card_id"] for r in refs], ["SW_034", "GDB_140", "REV_240"])
        self.assertEqual(refs[0]["name"], "小巧玩具")
        self.assertEqual(refs[2]["zone"], powerlog.ZONE_IDS["SETASIDE"])

    def test_name_does_not_swallow_fields(self) -> None:
        body = "[entityName=幸运币 id=72 zone=HAND zonePos=5 cardId=TTN_COIN2 player=1]"
        ref = powerlog._all_refs(body)[0]
        self.assertEqual(ref["name"], "幸运币")
        self.assertEqual(ref["id"], 72)
        self.assertEqual(ref["controller"], 1)

    def test_plain_entity_is_ignored(self) -> None:
        self.assertEqual(powerlog._all_refs("Entities[0]=[id=3 zone=PLAY]")[0]["id"], 3)
        self.assertEqual(powerlog._all_refs("没有方括号的一行"), [])


if __name__ == "__main__":
    unittest.main()
