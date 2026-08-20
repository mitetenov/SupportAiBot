"""Decides when a conversation needs a human tagged in the support topic."""

import re


class EscalationPolicy:
    """Decides when a conversation needs a human tagged in the support topic.

    The primary signal is the [ESCALATE] marker the model appends: it has the
    whole conversation in view and the system prompt tells it exactly when to use it.
    The keyword check is only a safety net for a user explicitly asking for a person.

    Matching is anchored on word boundaries. Substring checks without boundaries
    would fire on innocent phrases like "живу в Германии" or "болит живот".
    """

    ESCALATE_MARKER: str = "[ESCALATE]"

    # UNICODE word boundary matching for Russian morphology
    ASKS_FOR_HUMAN: re.Pattern[str] = re.compile(
        r"\b(оператор\w*|человек\w*|человеч\w*|жив(?:ой|ого|ому|ым|ом))\b",
        re.IGNORECASE | re.UNICODE,
    )

    @classmethod
    def model_requested_escalation(cls, raw_response: str | None) -> bool:
        """True when the model appended the escalation marker."""
        return bool(raw_response and cls.ESCALATE_MARKER in raw_response)

    @classmethod
    def user_requests_human(cls, user_message: str | None) -> bool:
        """True when the user explicitly asked to talk to a person."""
        if not user_message or not user_message.strip():
            return False
        return cls.ASKS_FOR_HUMAN.search(user_message) is not None

    @classmethod
    def strip_marker(cls, raw_response: str | None) -> str:
        """Removes the service marker before the text is shown to the user."""
        if raw_response is None:
            return ""
        return raw_response.replace(cls.ESCALATE_MARKER, "").strip()

    # CamelCase aliases for Java parity / convenience
    modelRequestedEscalation = model_requested_escalation
    userRequestsHuman = user_requests_human
    stripMarker = strip_marker
