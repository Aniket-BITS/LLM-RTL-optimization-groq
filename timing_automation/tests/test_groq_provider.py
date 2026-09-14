import sys
from types import SimpleNamespace
from unittest.mock import patch
import pytest
import run_optimizer


def test_empty_provider_choices_report_actionable_error(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-only-not-a-real-key")
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kwargs: SimpleNamespace(choices=[]))))
    with patch.dict(sys.modules, {"groq": SimpleNamespace(Groq=lambda: client)}):
        with pytest.raises(run_optimizer.OptimizerError, match="no completion choices"):
            run_optimizer.call_groq(system_prompt="test", user_prompt="test",
                model="openai/gpt-oss-120b", max_tokens=100,
                reasoning_effort="low")
