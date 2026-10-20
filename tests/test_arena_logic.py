import unittest

from bot.services.arena_service import (
    QUESTION_BANK, calculate_battle_rewards, choose_question, rank_for_xp, utc_day_key,
)


class ArenaLogicTests(unittest.TestCase):
    def test_rank_thresholds(self):
        self.assertEqual(rank_for_xp(0), "Rookie")
        self.assertEqual(rank_for_xp(99), "Rookie")
        self.assertEqual(rank_for_xp(100), "Warrior")
        self.assertEqual(rank_for_xp(300), "Elite")
        self.assertEqual(rank_for_xp(700), "Legend")

    def test_question_bank_has_valid_answers(self):
        self.assertGreaterEqual(len(QUESTION_BANK), 10)
        for question in QUESTION_BANK:
            self.assertGreaterEqual(question["answer"], 0)
            self.assertLess(question["answer"], len(question["options"]))
            self.assertGreaterEqual(len(question["options"]), 2)

    def test_question_kind_filter(self):
        self.assertEqual(choose_question("quiz")["kind"], "quiz")
        self.assertEqual(choose_question("puzzle")["kind"], "puzzle")

    def test_battle_rewards(self):
        self.assertEqual(calculate_battle_rewards(True, False, 1.0, 2.0), (20, 5, 1, 0))
        self.assertEqual(calculate_battle_rewards(False, True, 2.0, 1.0), (5, 20, 0, 1))
        self.assertEqual(calculate_battle_rewards(True, True, 1.0, 2.0), (20, 5, 1, 0))
        self.assertEqual(calculate_battle_rewards(True, True, 1.0, 1.0), (10, 10, 0, 0))
        self.assertEqual(calculate_battle_rewards(False, False, 1.0, 2.0), (2, 2, 0, 0))

    def test_day_key_is_utc(self):
        self.assertEqual(utc_day_key(), utc_day_key())


if __name__ == "__main__":
    unittest.main()
