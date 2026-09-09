import random
import unittest

from app.domain import contains_points_word, is_jackpot, make_math_problem


class JackpotTests(unittest.TestCase):
    def test_only_maximum_result_wins_for_every_game(self):
        for emoji, maximum in [("🎰", 64), ("🎲", 6), ("🎯", 6), ("🎳", 6), ("🏀", 5), ("⚽", 5)]:
            for value in range(0, maximum + 2):
                with self.subTest(emoji=emoji, value=value):
                    self.assertEqual(is_jackpot(emoji, value), value == maximum)


class MathProblemTests(unittest.TestCase):
    def test_generated_problem_always_fits_buttons(self) -> None:
        rng = random.Random(42)
        for _ in range(1_000):
            problem = make_math_problem(rng)
            self.assertGreaterEqual(problem.left, 1)
            self.assertGreaterEqual(problem.right, 1)
            self.assertLessEqual(problem.answer, 10)


class PointsWordTests(unittest.TestCase):
    def test_recognizes_grammatical_forms(self) -> None:
        for text in (
            "балл",
            "БАЛЛЫ",
            "нет баллов",
            "одним баллом",
            "к баллам",
            "говорим о баллах",
        ):
            with self.subTest(text=text):
                self.assertTrue(contains_points_word(text))

    def test_does_not_match_words_with_same_prefix(self) -> None:
        self.assertFalse(contains_points_word("баллистика и баллада"))


if __name__ == "__main__":
    unittest.main()

