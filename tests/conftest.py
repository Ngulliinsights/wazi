import os
import tempfile

os.environ["WAZI_HOME"] = tempfile.mkdtemp(prefix="wazi_test_")  # must be set before wazi imports
os.environ["WAZI_LLM"] = "stub"  # API tests never need a real model
os.environ["WAZI_ALLOW_PRIVATE_FETCH"] = "1"  # tests serve documents from 127.0.0.1; the policy itself is tested explicitly
os.environ["WAZI_INPROC"] = "1"  # fast in-process tool session for agent-logic tests; tests marked `mcp` use the real servers

import pytest


@pytest.fixture(autouse=True)
def _real_mcp_for_marked_tests(request, monkeypatch):
    if request.node.get_closest_marker("mcp"):
        monkeypatch.setenv("WAZI_INPROC", "0")
os.environ["WAZI_MAX_RETRIES"] = "0"   # tests never sleep through backoff; the retry tests set it explicitly


@pytest.fixture(autouse=True)
def _fresh_quota_and_cache(monkeypatch):
    from wazi.agent import llm
    llm.QUOTA.reset()
    for v in ("WAZI_RPM", "WAZI_MAX_CALLS", "WAZI_MAX_TOKENS", "WAZI_LLM_CACHE", "WAZI_LLM_CACHE_MODE", "WAZI_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    yield
    llm.QUOTA.reset()
