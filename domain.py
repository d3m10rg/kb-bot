from __future__ import annotations

import random
import re
from dataclasses import dataclass
from random import Random

JACKPOT_VALUES = {"🎰": 64, "🎲": 6, "🎯": 6, "🎳": 6, "🏀": 5, "⚽": 5}


def is_jackpot(emoji: str, value: int) -> bool:
    return JACKPOT_VALUES.get(emoji) == value


# Все падежные формы существительного «балл» во множественном и единственном
# числе. Границы слова не дают совпасть, например, с «баллистика».
POINTS_WORD_RE = re.compile(
    r"(?<![\wа-яё])балл(?:ы|а|ов|у|ам|ом|ами|е|ах)?(?![\wа-яё])",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class MathProblem:
    left: int
    right: int

    @property
    def answer(self) -> int:
        return self.left + self.right


def make_math_problem(rng: Random | None = None) -> MathProblem:
    """Create a positive-integer sum whose answer is in the range 2..10."""
    source = rng or random
    left = source.randint(1, 9)
    right = source.randint(1, 10 - left)
    return MathProblem(left=left, right=right)


def contains_points_word(text: str | None) -> bool:
    return bool(text and POINTS_WORD_RE.search(text))

