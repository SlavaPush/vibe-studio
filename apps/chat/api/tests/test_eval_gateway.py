import asyncio
import importlib.util
from pathlib import Path

import pytest
from conftest import FakeProviders, connect_error, read_timeout, status_error

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "eval_gateway.py"
spec = importlib.util.spec_from_file_location("eval_gateway", SCRIPT)
eval_gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eval_gateway)

CASE = {
    "id": "greeting",
    "text": "Привет",
    "decision": "allow",
    "labels": {"intent": "chat", "complexity": "trivial", "risk": "none"},
}


def evaluate(providers: FakeProviders) -> dict:
    return asyncio.run(eval_gateway.evaluate(providers, CASE, asyncio.Semaphore(1)))


@pytest.mark.parametrize("error", [read_timeout(), status_error(500), TimeoutError()])
def test_unknown_outcome_is_not_sent_to_reserve_classifier(error):
    providers = FakeProviders()
    providers.fail("classifier:claude-haiku-5-5", error)
    result = evaluate(providers)
    assert result["ok"] is False
    assert "unknown" in result["error"]
    assert providers.count("classifier") == 1


def test_confirmed_refusal_uses_reserve_classifier():
    providers = FakeProviders()
    providers.fail("classifier:claude-haiku-5-5", connect_error())
    result = evaluate(providers)
    assert result["ok"] is True
    assert result["classifier_model"] == "gpt-6-luna"


def test_invalid_answer_is_retried_once():
    providers = FakeProviders()
    providers.fail("classifier", "invalid", "invalid")
    result = evaluate(providers)
    assert result["ok"] is False
    assert providers.count("classifier") == 2
