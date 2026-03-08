import json
import pytest
from unittest.mock import MagicMock

from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.deep_debugger.deep_debugger import DeepDebugger
from bizniz.deep_debugger.types import DeepDiagnosis, DeepDebuggerBadAIResponseError


def _make_response(
    root_cause="Interface mismatch between modules",
    root_cause_category="interface_mismatch",
    fix_target="code",
    affected_files=None,
    fix_plan=None,
    suggested_approach="Align the function signatures across modules",
    missing_packages=None,
    confidence="high",
    repair_history_analysis="Previous repairs only addressed symptoms",
):
    if affected_files is None:
        affected_files = ["tracker.py", "api.py"]
    if fix_plan is None:
        fix_plan = ["Fix tracker.py signature", "Update api.py calls"]
    if missing_packages is None:
        missing_packages = []

    text = json.dumps({
        "root_cause": root_cause,
        "root_cause_category": root_cause_category,
        "fix_target": fix_target,
        "affected_files": affected_files,
        "fix_plan": fix_plan,
        "suggested_approach": suggested_approach,
        "missing_packages": missing_packages,
        "confidence": confidence,
        "repair_history_analysis": repair_history_analysis,
    })
    return text, "job-deep-123", [{"role": "assistant", "content": text}]


@pytest.fixture
def mock_client():
    return MagicMock(spec=BaseAIClient)


class TestDeepDebugger:

    def test_returns_deep_diagnosis(self, mock_client):
        mock_client.get_text.return_value = _make_response()
        debugger = DeepDebugger(client=mock_client)

        result = debugger.diagnose(
            error_output="AssertionError: expected 'hello' got None",
            source_files={"tracker.py": "class Tracker: pass"},
            test_files={"test_tracker.py": "def test_it(): ..."},
        )

        assert isinstance(result, DeepDiagnosis)
        assert result.root_cause == "Interface mismatch between modules"
        assert result.root_cause_category == "interface_mismatch"
        assert result.affected_files == ["tracker.py", "api.py"]
        assert len(result.fix_plan) == 2
        assert result.confidence == "high"

    def test_uses_fresh_messages_no_history(self, mock_client):
        """Verify the client is called with use_message_history=False."""
        mock_client.get_text.return_value = _make_response()
        debugger = DeepDebugger(client=mock_client)

        debugger.diagnose(
            error_output="error",
            source_files={"src.py": "pass"},
            test_files={"test_src.py": "pass"},
        )

        call_kwargs = mock_client.get_text.call_args.kwargs
        assert call_kwargs["use_message_history"] is False

    def test_formats_source_and_test_files(self, mock_client):
        mock_client.get_text.return_value = _make_response()
        debugger = DeepDebugger(client=mock_client)

        debugger.diagnose(
            error_output="NameError",
            source_files={
                "module_a.py": "def foo(): return 1",
                "module_b.py": "def bar(): return 2",
            },
            test_files={"test_a.py": "def test_foo(): assert foo() == 1"},
            architecture_context="Two-module architecture",
        )

        call_args = mock_client.get_text.call_args
        messages = call_args.kwargs.get("messages") or call_args[0][0]
        user_msg = [m for m in messages if m["role"] == "user"][0]["content"]

        assert "--- module_a.py ---" in user_msg
        assert "def foo(): return 1" in user_msg
        assert "--- module_b.py ---" in user_msg
        assert "--- test_a.py ---" in user_msg
        assert "Two-module architecture" in user_msg

    def test_formats_repair_history(self, mock_client):
        mock_client.get_text.return_value = _make_response()
        debugger = DeepDebugger(client=mock_client)

        debugger.diagnose(
            error_output="error",
            source_files={"src.py": "pass"},
            test_files={"test_src.py": "pass"},
            repair_history=["Changed return type", "Added missing import"],
        )

        call_args = mock_client.get_text.call_args
        messages = call_args.kwargs.get("messages") or call_args[0][0]
        user_msg = [m for m in messages if m["role"] == "user"][0]["content"]
        assert "1. Changed return type" in user_msg
        assert "2. Added missing import" in user_msg

    def test_empty_repair_history(self, mock_client):
        mock_client.get_text.return_value = _make_response()
        debugger = DeepDebugger(client=mock_client)

        debugger.diagnose(
            error_output="error",
            source_files={"src.py": "pass"},
            test_files={"test_src.py": "pass"},
            repair_history=[],
        )

        call_args = mock_client.get_text.call_args
        messages = call_args.kwargs.get("messages") or call_args[0][0]
        user_msg = [m for m in messages if m["role"] == "user"][0]["content"]
        assert "(no previous attempts)" in user_msg

    def test_retries_on_empty_response(self, mock_client):
        good_response = _make_response()
        mock_client.get_text.side_effect = [
            ("", "job-1", [{"role": "assistant", "content": ""}]),
            good_response,
        ]
        debugger = DeepDebugger(client=mock_client)

        result = debugger.diagnose(
            error_output="error",
            source_files={"src.py": "pass"},
            test_files={"test_src.py": "pass"},
        )

        assert isinstance(result, DeepDiagnosis)
        assert mock_client.get_text.call_count == 2

    def test_raises_after_max_retries(self, mock_client):
        mock_client.get_text.return_value = ("", "job-1", [{"role": "assistant", "content": ""}])
        debugger = DeepDebugger(client=mock_client)

        with pytest.raises(DeepDebuggerBadAIResponseError):
            debugger.diagnose(
                error_output="error",
                source_files={"src.py": "pass"},
                test_files={"test_src.py": "pass"},
            )

    def test_status_messages_emitted(self, mock_client):
        mock_client.get_text.return_value = _make_response()
        messages = []
        debugger = DeepDebugger(client=mock_client, on_status_message=messages.append)

        debugger.diagnose(
            error_output="error",
            source_files={"src.py": "pass"},
            test_files={"test_src.py": "pass"},
        )

        assert any("analyzing full project context" in m for m in messages)
        assert any("root cause identified" in m for m in messages)

    def test_dependency_issue_returns_missing_packages(self, mock_client):
        mock_client.get_text.return_value = _make_response(
            root_cause="Missing 'requests' package",
            root_cause_category="dependency_issue",
            missing_packages=["requests"],
        )
        debugger = DeepDebugger(client=mock_client)

        result = debugger.diagnose(
            error_output="ModuleNotFoundError: No module named 'requests'",
            source_files={"src.py": "import requests"},
            test_files={"test_src.py": "pass"},
        )

        assert result.root_cause_category == "dependency_issue"
        assert result.missing_packages == ["requests"]

    def test_non_dependency_issue_has_empty_packages(self, mock_client):
        mock_client.get_text.return_value = _make_response()
        debugger = DeepDebugger(client=mock_client)

        result = debugger.diagnose(
            error_output="error",
            source_files={"src.py": "pass"},
            test_files={"test_src.py": "pass"},
        )

        assert result.missing_packages == []
