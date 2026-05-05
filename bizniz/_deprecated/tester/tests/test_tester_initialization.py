from bizniz.tester.prompts.system_prompt import TESTER_SYSTEM_PROMPT


def test_system_prompt_seeded_in_history(tester):
    """System prompt is the first message in history."""
    first = tester.message_history[0]
    assert first["role"] == "system"
    assert first["content"] == TESTER_SYSTEM_PROMPT


def test_system_prompt_contains_pytest_context(tester):
    """System prompt mentions pytest so the AI knows the test framework."""
    assert "pytest" in TESTER_SYSTEM_PROMPT.lower()
