from app.guardrails import redact_pii
from app.tools import ToolPermissionError, ToolRegistry


def test_pii_redaction_invariant_covers_common_identifiers() -> None:
    text, flags = redact_pii("jane@example.com, 555-123-4567, 123-45-6789, 4111 1111 1111 1111")

    assert "jane@example.com" not in text
    assert "555-123-4567" not in text
    assert "123-45-6789" not in text
    assert "4111 1111 1111 1111" not in text
    assert {"email", "phone", "ssn", "card"}.issubset(flags)


def test_hard_denies_cannot_be_bypassed_by_specialist_tools() -> None:
    registry = ToolRegistry.__new__(ToolRegistry)
    registry.ALLOWLISTS = ToolRegistry.ALLOWLISTS

    for tool_name in ("disable_2fa", "change_email", "unlock_account", "delete_account"):
        try:
            registry.call("account-security", tool_name, customer_id="customer-123")
        except (ToolPermissionError, AttributeError):
            pass
        else:
            raise AssertionError(f"{tool_name} must remain human-only")
