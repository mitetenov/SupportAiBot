"""Recognises a user turning down the answer they were just given."""

from collections.abc import Sequence


class RejectionDetector:
    """Recognises a user turning down the answer they were just given ("это не то", "не подходит", ...).

    Two call sites depend on agreeing about this: the retriever re-searches
    against the previous question on a rejection, and the history service
    keeps the already-shown FAQ entries excluded instead of resetting them.
    """

    REJECTION_PHRASES: Sequence[str] = (
        "не то",
        "не та",
        "не это",
        "не подходит",
        "не помог",
        "другой вариант",
        "другая инструкция",
        "другое",
        "нет,",
    )

    @classmethod
    def is_rejection(cls, message: str | None) -> bool:
        """Returns True if the message indicates rejection of a previous answer."""
        if not message or not message.strip():
            return False
        lower = message.lower()
        return any(phrase in lower for phrase in cls.REJECTION_PHRASES)


def is_rejection(message: str | None) -> bool:
    """Convenience function to check if a message is a rejection."""
    return RejectionDetector.is_rejection(message)
