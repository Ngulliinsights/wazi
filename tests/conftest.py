import os
import tempfile

os.environ["BIDBOX_HOME"] = tempfile.mkdtemp(prefix="bidbox_test_")  # must be set before bidbox imports
os.environ["BIDBOX_LLM"] = "stub"  # API tests never need a real model
os.environ["BIDBOX_ALLOW_PRIVATE_FETCH"] = "1"  # tests serve documents from 127.0.0.1; the policy itself is tested explicitly
os.environ["BIDBOX_INPROC"] = "1"  # fast in-process tool session for agent-logic tests; tests marked `mcp` use the real servers

import pytest


@pytest.fixture(autouse=True)
def _real_mcp_for_marked_tests(request, monkeypatch):
    if request.node.get_closest_marker("mcp"):
        monkeypatch.setenv("BIDBOX_INPROC", "0")
os.environ["BIDBOX_MAX_RETRIES"] = "0"   # tests never sleep through backoff; the retry tests set it explicitly


@pytest.fixture(autouse=True)
def _fresh_quota_and_cache(monkeypatch):
    from bidbox.agent import llm
    llm.QUOTA.reset()
    for v in ("BIDBOX_RPM", "BIDBOX_MAX_CALLS", "BIDBOX_MAX_TOKENS", "BIDBOX_LLM_CACHE", "BIDBOX_LLM_CACHE_MODE", "BIDBOX_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    yield
    llm.QUOTA.reset()
