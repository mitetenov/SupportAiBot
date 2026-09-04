"""Recognises a user turning down the answer they were just given."""

import re
from collections.abc import Sequence


class RejectionDetector:
    """Recognises a user turning down the answer they were just given ("это не то", "не подходит", ...).

    Two call sites depend on agreeing about this: the retriever re-searches
    against the previous question on a rejection, and the history service
    keeps the already-shown FAQ entries excluded instead of resetting them.

    Matching happens on ё-normalised lowercase text, so a phrase only has to be
    listed once — "по-прежнему" and "по-прежнему" written with е are the same
    entry as far as this is concerned.
    """

    REJECTION_PHRASES: Sequence[str] = (
        # Explicit "wrong answer"
        "не то",
        "не та",
        "не это",
        "не подходит",
        "не помог",
        "не помогло",
        "другой вариант",
        "другая инструкция",
        "нет, я про другое",
        # "I did what you said and nothing changed". Without these the exclusion
        # set was reset on every such turn, so the retriever kept handing back
        # the same entries and the user was told to press the same two buttons
        # over and over instead of being moved on to the next instruction.
        "все равно не",
        "все еще не",
        "по-прежнему",
        "по прежнему",
        "ничего не изменилось",
        "ничего не поменялось",
        "ничего не помогает",
        "то же самое",
        "тоже самое",
        "опять не работает",
        "снова не работает",
        "также не работает",
        "так же не работает",
        "не заработало",
        "без изменений",
    )

    #: A rejection has to be a complete phrase.  A substring search made
    #: "Как подключить другое устройство?" reject a previous answer because it
    #: contains "другое", and read "не только" as "не то".  Those messages
    #: introduce a new constraint; excluding the previous FAQ for them loses
    #: useful context.
    _PHRASE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
        re.compile(rf"(?<!\w){re.escape(phrase.lower().replace('ё', 'е'))}(?!\w)", re.UNICODE)
        for phrase in REJECTION_PHRASES
    )
    _TERSE_REJECTION: re.Pattern[str] = re.compile(r"^другое[.!?]*$", re.UNICODE)

    @staticmethod
    def _normalise(text: str) -> str:
        """Lowercase and fold ё to е so a phrase needs only one spelling."""
        return text.lower().replace("ё", "е")

    @classmethod
    def is_rejection(cls, message: str | None) -> bool:
        """Returns True if the message indicates rejection of a previous answer."""
        if not message or not message.strip():
            return False
        normalised = cls._normalise(message)
        return cls._TERSE_REJECTION.fullmatch(normalised) is not None or any(
            pattern.search(normalised) is not None for pattern in cls._PHRASE_PATTERNS
        )


def is_rejection(message: str | None) -> bool:
    """Convenience function to check if a message is a rejection."""
    return RejectionDetector.is_rejection(message)
