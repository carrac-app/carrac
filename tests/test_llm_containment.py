"""The guard on the LLM guard.

The suite made 11 real POSTs to api.anthropic.com per run: the report-generation
tests fake the Sonnet/Opus provider, but the Haiku grounding verifier builds its
OWN client when none is injected. Locally ANTHROPIC_API_KEY is unset so they
401 and everything stays green — which is why it went unseen. On a box with the
key, `make test` spends real money and couples a deploy to Anthropic being up.

Sibling of tests/test_notification_containment.py. These are the
positive controls: if someone deletes or weakens the guard in conftest.py, a
test fails rather than the leak quietly coming back.
"""
import pytest
import requests


def test_anthropic_post_is_blocked(llm_calls):
    """Positive control: the guard actually intercepts, and names the host."""
    with pytest.raises(requests.exceptions.ConnectionError) as exc:
        requests.post("https://api.anthropic.com/v1/messages", json={})
    assert "Blocked a real LLM call" in str(exc.value)
    assert llm_calls == ["https://api.anthropic.com/v1/messages"]


def test_session_post_is_blocked_too(llm_calls):
    """Every provider uses `self.session = requests.Session()`, so guarding the
    module-level requests.post alone would have missed all of them."""
    s = requests.Session()
    with pytest.raises(requests.exceptions.ConnectionError):
        s.post("https://api.x.ai/v1/chat/completions", json={})
    assert llm_calls == ["https://api.x.ai/v1/chat/completions"]


def test_blocked_error_is_a_connectionerror_so_providers_behave_normally():
    """The exception type is deliberate: each provider already handles a
    network failure, so blocking exercises the SAME branch a real outage
    would. If this stops being a ConnectionError, providers start raising
    unexpected errors and tests fail for the wrong reason."""
    from tests.conftest import RealLLMCallBlocked
    assert issubclass(RealLLMCallBlocked, requests.exceptions.ConnectionError)


def test_non_llm_hosts_are_not_intercepted(llm_calls):
    """The guard matches on host. It must not become a blanket network block —
    plenty of tests legitimately post elsewhere, and a broad guard would be
    switched off within a week.

    Asserts only that the GUARD stayed out of the way; deliberately says
    nothing about what the real transport then does, so this still holds when
    the suite runs under an outbound-HTTP recorder.
    """
    try:
        requests.post("http://127.0.0.1:9/anything", data=b"x", timeout=2)
    except Exception as e:  # noqa: BLE001 — any transport outcome is fine
        assert "Blocked a real LLM call" not in str(e), "the guard blocked a non-LLM host"
    assert llm_calls == []


def test_sdk_client_path_is_blocked_too(llm_calls):
    """carrac/llm/anthropic.py goes through the Anthropic SDK (httpx), which
    the requests-level patch cannot see. It raises without a key today — this
    pins that it also raises WITH one."""
    import carrac.llm.anthropic as anthropic_mod

    with pytest.raises(requests.exceptions.ConnectionError):
        anthropic_mod._get_sdk_client()
    assert llm_calls and "SDK client" in llm_calls[0]


def test_verifier_builds_its_own_client_when_none_is_injected(llm_calls,
                                                              monkeypatch):
    """The actual defect, pinned. verify_against_source falls back to a real
    MorningAnthropicClient — so any caller that forgets to inject one makes a
    billed call. If that fallback is ever removed, this test should be updated,
    not deleted: it is the reason the guard exists.
    """
    monkeypatch.setenv("GROUNDING_VERIFY_ENABLED", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    from carrac.grounding.verify import verify_against_source

    result = verify_against_source("generated text", "source text")

    # It fails open (verdict ERROR).
    assert result.verdict == "ERROR"
    assert any("api.anthropic.com" in c for c in llm_calls), llm_calls


def test_unopted_llm_call_is_reported_as_a_failure():
    """The guard's teardown must FAIL a test that calls out without opting in.

    Asserted structurally — the conftest fixture is the enforcement point, and
    this pins the two things an edit could quietly drop.
    """
    import inspect

    from tests import conftest

    src = inspect.getsource(conftest._no_real_llm_calls)
    assert 'pytest.fail' in src, (
        "the LLM guard no longer fails the test — real LLM calls would "
        "go back to being invisible"
    )
    assert "'llm_calls' not in request.fixturenames" in src, (
        "the opt-in check changed shape; every LLM call would now be allowed"
    )
