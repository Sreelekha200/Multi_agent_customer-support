import re
from typing import Iterable

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4})")
SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def redact_pii(text: str) -> tuple[str, list[str]]:
    """Mask common PII values and return the redacted content plus detected categories."""
    redacted = text
    flags: list[str] = []

    patterns: Iterable[tuple[re.Pattern[str], str, str]] = (
        (EMAIL_RE, "email", "[REDACTED_EMAIL]"),
        (PHONE_RE, "phone", "[REDACTED_PHONE]"),
        (SSN_RE, "ssn", "[REDACTED_SSN]"),
        (CARD_RE, "card", "[REDACTED_CARD]"),
    )

    for pattern, flag_name, replacement in patterns:
        if pattern.search(redacted):
            redacted = pattern.sub(replacement, redacted)
            flags.append(flag_name)

    return redacted, flags


def validate_response_policy(response: str) -> list[str]:
    """Return policy violation names for unsafe or unapproved response wording."""
    lowered = response.lower()
    violations: list[str] = []

    if any(token in lowered for token in ("guarantee", "promised", "100%", "always fixed")):
        violations.append("unsupported_promise")
    if "legal advice" in lowered or "legal counsel" in lowered:
        violations.append("legal_advice")
    if any(token in lowered for token in ("bypass", "hack", "secret backdoor", "delete everything", "force access")):
        violations.append("unsafe_wording")

    return violations
