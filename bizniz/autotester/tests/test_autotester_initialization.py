from bizniz.autotester.prompts.system_prompt import AUTOTESTER_SYSTEM_PROMPT


def test_system_prompt_seeded_in_history(autotester):
    """System prompt is the first message in history."""
    first = autotester.message_history[0]
    assert first["role"] == "system"
    assert first["content"] == AUTOTESTER_SYSTEM_PROMPT


def test_system_prompt_contains_pytest_context(autotester):
    """System prompt mentions pytest so the AI knows the test framework."""
    assert "pytest" in AUTOTESTER_SYSTEM_PROMPT.lower()
