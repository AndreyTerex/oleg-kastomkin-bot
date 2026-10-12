import unittest
from copy import deepcopy

from lobby_state import RosterError, change_roster


class RosterTests(unittest.TestCase):
    def record(self):
        return {"stage": "signup", "target": 2, "in": [1, 2], "sub": [], "out": []}

    def test_repeat_join_does_not_reorder_players_or_queue(self):
        record = self.record()
        change_roster(record, [1], "in")
        self.assertEqual(record["in"], [1, 2])
        change_roster(record, [3, 4], "in")
        change_roster(record, [3], "in")
        self.assertEqual(record["waiting"], [3, 4])
        self.assertEqual(record["sub"], [3, 4])

    def test_voluntary_sub_leaves_waiting_queue(self):
        record = self.record()
        change_roster(record, [3], "in")
        change_roster(record, [3], "sub")
        self.assertEqual(record["waiting"], [])
        self.assertEqual(record["sub"], [3])

    def test_locked_roster_unchanged(self):
        for stage in ("captains", "draft", "launching", "done"):
            record = self.record()
            record["stage"] = stage
            before = deepcopy(record)
            with self.assertRaises(RosterError):
                change_roster(record, [1], "out")
            self.assertEqual(record, before)

    def test_batch_move_deduplicates_and_respects_capacity(self):
        record = self.record()
        changed, overflow = change_roster(record, [3, 3, 4], "in")
        self.assertEqual(changed, [])
        self.assertEqual(overflow, [3, 4])
        change_roster(record, [1], None)
        changed, overflow = change_roster(record, [3], "in")
        self.assertEqual(record["in"], [2, 3])
        self.assertEqual(record["waiting"], [4])
