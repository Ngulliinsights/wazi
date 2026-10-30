"""Free-tier realities: keys, 429/5xx with Retry-After, daily quotas, providers without JSON mode, pacing, budgets, caching,
remote endpoints. A fake server and injected clock/sleep: no real network, no real waiting."""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from wazi import config
from wazi.agent import run_agent
from wazi.agent.llm import CACHE, QUOTA, LLMError, OllamaLLM, QuotaExhausted, StubLLM, is_remote

OBJ = '{"checks":["history","benchmark"],"rationale":"r"}'


@pytest.fixture(scope="module", autouse=True)
def _data(tmp_path_factory):
    from pathlib import Path
    from wazi.evals import build_fixture_releases
    from wazi.store import connect, load_ocds
    f = tmp_path_factory.mktemp("d") / "fx.json"; f.write_text(json.dumps({"releases": build_fixture_releases()}), encoding="utf-8")
    c = connect(); load_ocds(Path(__file__).resolve().parent.parent / "data" / "sample_ocds.json", c); load_ocds(f, c)


@pytest.fixture
def srv():
    st = {"script": [], "seen": []}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            st["seen"].append({"path": self.path, "body": body, "auth": self.headers.get("Authorization")})
            status, payload, hdrs = (st["script"].pop(0) + ({},))[:3] if st["script"] else (599, {"error": "script exhausted"}, {})
            self.send_response(status); self.send_header("Content-Type", "application/json")
            for k, v in (hdrs or {}).items(): self.send_header(k, v)
            self.end_headers(); self.wfile.write(json.dumps(payload).encode())
        def log_message(self, *a): pass
    s = ThreadingHTTPServer(("127.0.0.1", 0), H); threading.Thread(target=s.serve_forever, daemon=True).start()
    st["url"] = f"http://127.0.0.1:{s.server_address[1]}"
    yield st
    s.shutdown()


def native(content=OBJ, **kw): return (200, {"message": {"content": content}, "done_reason": "stop", "eval_count": 100, "prompt_eval_count": 800, "total_duration": 2_000_000_000, **kw})
def oai(content=OBJ, **kw): return (200, {"choices": [{"message": {"content": content}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 50, "completion_tokens": 20}, **kw})
def client(srv, api="native", **kw):
    c = OllamaLLM("m", srv["url"], api=api, **kw); c.slept = []; c._sleep = c.slept.append; return c
def ask(c): return c.complete_json("plan", {"facts": "x"})


# ---------- keys
@pytest.mark.parametrize("api,mk", [("openai", oai), ("native", native)])
def test_api_key_is_sent_as_a_bearer_token_only_when_configured(srv, api, mk):
    srv["script"] = [mk(), mk()]
    ask(client(srv, api, api_key="sk-test-123")); ask(client(srv, api))
    assert srv["seen"][0]["auth"] == "Bearer sk-test-123" and srv["seen"][1]["auth"] is None


def test_the_key_never_appears_in_settings_or_the_recorded_trace(srv):
    srv["script"] = [native()]; c = client(srv, api_key="sk-SECRET-999"); ask(c)
    assert "sk-SECRET-999" not in json.dumps(c.settings) and "sk-SECRET-999" not in json.dumps(c.trace)


def test_an_unauthorised_key_gives_a_clear_message_and_is_not_retried(srv):
    srv["script"] = [(401, {"error": "bad key"})]; c = client(srv, max_retries=3)
    with pytest.raises(LLMError, match="authentication failed.*WAZI_API_KEY"): ask(c)
    assert len(srv["seen"]) == 1 and c.slept == []


def test_openai_base_url_ending_in_v1_is_not_doubled(srv):
    srv["url"] += "/v1"; srv["script"] = [oai()]; ask(client(srv, "openai"))
    assert srv["seen"][0]["path"] == "/v1/chat/completions"


# ---------- rate limits and flaky servers
def test_429_honours_retry_after(srv):
    srv["script"] = [(429, {"error": "slow down"}, {"Retry-After": "7"}), native()]; c = client(srv, max_retries=2)
    assert ask(c)["checks"] and c.slept == [7.0] and len(srv["seen"]) == 2


def test_429_without_a_hint_backs_off_exponentially_then_gives_up_clearly(srv):
    srv["script"] = [(429, {"error": "busy"})] * 3 + [native()]; c = client(srv, max_retries=3)
    assert ask(c)["checks"] and c.slept == [1.0, 2.0, 4.0]
    srv["script"] = [(503, {"error": "down"})] * 3; c2 = client(srv, max_retries=2)
    with pytest.raises(LLMError, match="HTTP 503"): ask(c2)
    assert c2.slept == [1.0, 2.0]


def test_a_daily_quota_429_is_not_retried_and_raises_quota_exhausted(srv):
    srv["script"] = [(429, {"error": {"message": "You exceeded your current quota; daily limit reached"}})]; c = client(srv, max_retries=4)
    with pytest.raises(QuotaExhausted, match="quota"): ask(c)
    assert len(srv["seen"]) == 1 and c.slept == []


def test_a_provider_without_json_mode_is_retried_without_it_and_remembered(srv):
    srv["script"] = [(400, {"error": {"message": "response_format json_object is not supported"}}), oai(), oai()]; c = client(srv, "openai")
    assert ask(c)["checks"] and "response_format" in srv["seen"][0]["body"] and "response_format" not in srv["seen"][1]["body"]
    ask(c); assert "response_format" not in srv["seen"][2]["body"] and c.settings["json_mode"] is False


# ---------- pacing and budgets
def test_requests_per_minute_pacing_spaces_calls_without_real_waiting(srv):
    srv["script"] = [native()] * 3; c = client(srv)
    class Clock:
        t = 0.0
        def __call__(self): return self.t
        def sleep(self, s): c.slept.append(s); self.t += s
    clk = Clock(); QUOTA.clock = clk; c._sleep = clk.sleep; QUOTA.configure(rpm=30)
    for _ in range(3): ask(c)
    assert c.slept == [2.0, 2.0]


def test_call_and_token_budgets_stop_the_run_before_the_network(srv):
    srv["script"] = [native()] * 5; QUOTA.configure(max_calls=2); c = client(srv)
    ask(c); ask(c)
    with pytest.raises(QuotaExhausted, match="call budget"): ask(c)
    assert len(srv["seen"]) == 2
    QUOTA.reset(); QUOTA.configure(max_tokens=100); srv["script"] = [native()] * 3; srv["seen"].clear(); c2 = client(srv)
    ask(c2)                                                       # 800 + 100 tokens spent
    with pytest.raises(QuotaExhausted, match="token budget"): ask(c2)


# ---------- the cache: repeat evals cost nothing
def test_repeated_prompts_are_served_from_the_cache_with_no_quota(srv, tmp_path, monkeypatch):
    monkeypatch.setenv("WAZI_LLM_CACHE", str(tmp_path / "c.jsonl")); QUOTA.configure(max_calls=1)
    srv["script"] = [native()]; a = client(srv); ask(a)
    b = client(srv); out = ask(b); ask(b)                          # a NEW client, same prompt: still free
    assert out["checks"] and len(srv["seen"]) == 1 and QUOTA.calls == 1
    assert [t["cached"] for t in a.trace] == [False] and [t["cached"] for t in b.trace] == [True, True] and b.tokens_out == 200


def test_cache_misses_on_a_different_model_or_prompt_and_survives_corruption(srv, tmp_path, monkeypatch):
    path = tmp_path / "c.jsonl"; monkeypatch.setenv("WAZI_LLM_CACHE", str(path)); srv["script"] = [native(), native(), native()]
    ask(client(srv))
    other = OllamaLLM("other-model", srv["url"]); other._sleep = lambda s: None; ask(other)
    client(srv).complete_json("plan", {"facts": "a different question"})
    assert len(srv["seen"]) == 3
    with open(path, "a", encoding="utf-8") as f: f.write("not json\n{\"key\": \n\n")
    CACHE.path = None                                              # force a reload from disk, corrupt tail included
    ask(client(srv)); assert len(srv["seen"]) == 3


def test_readonly_cache_guarantees_no_network_and_replays_exactly(srv, tmp_path, monkeypatch):
    monkeypatch.setenv("WAZI_LLM_CACHE", str(tmp_path / "c.jsonl")); srv["script"] = [native()]; ask(client(srv))
    monkeypatch.setenv("WAZI_LLM_CACHE_MODE", "readonly")
    dead = OllamaLLM("m", "http://127.0.0.1:1", timeout=1); assert ask(dead)["checks"]                    # hit: works with the server gone
    with pytest.raises(LLMError, match="readonly"): dead.complete_json("plan", {"facts": "never asked before"})   # miss: refuses, never touches the network


def test_an_unparseable_reply_is_never_cached(srv, tmp_path, monkeypatch):
    path = tmp_path / "c.jsonl"; monkeypatch.setenv("WAZI_LLM_CACHE", str(path)); srv["script"] = [native("this is not json"), native()]
    c = client(srv)
    with pytest.raises(LLMError): ask(c)
    assert not path.exists() or path.read_text(encoding="utf-8") == ""
    assert ask(c)["checks"] and len(srv["seen"]) == 2


# ---------- data residency
@pytest.mark.parametrize("url,remote", [("http://localhost:11434", False), ("http://127.0.0.1:8080", False), ("http://192.168.1.5:11434", False), ("http://10.0.0.2", False),
                                        ("http://[::1]:11434", False), ("http://box.local:1234", False), ("https://api.groq.com/openai/v1", True), ("http://8.8.8.8", True),
                                        ("https://example.org", True)])
def test_remote_endpoints_are_recognised(url, remote):
    assert is_remote(url) is remote and OllamaLLM("m", url).settings["remote"] is remote


@pytest.mark.parametrize("remote,words", [(True, "REMOTE endpoint: prompts left this machine"), (False, "(local)")])
def test_every_brief_says_whether_the_model_was_remote(remote, words):
    class S(StubLLM):
        @property
        def settings(self): return {"stub": True, "remote": remote}
    r = asyncio.run(run_agent("ocds-synth-0001", "x", "2025-05-01", S("invest_all")))
    footer = (config.OUTBOX / "pending" / f"{r['filing_id']}.md").read_text(encoding="utf-8").splitlines()[-1]
    assert words in footer


# ---------- a used-up quota stops the run; it is never papered over with a fallback brief
@pytest.mark.parametrize("task", ["plan", "investigate", "synthesize"])
def test_quota_exhaustion_in_any_step_stops_the_run_and_files_nothing(task):
    class Q(StubLLM):
        def complete_json(self, t, p):
            if t == task: raise QuotaExhausted("daily quota used up")
            return super().complete_json(t, p)
    before = sorted(p.name for p in (config.OUTBOX / "pending").glob("*.json"))
    with pytest.raises(QuotaExhausted):
        asyncio.run(run_agent("ocds-eval-abv-target", "x", "2025-05-01", Q("invest_all")))
    assert sorted(p.name for p in (config.OUTBOX / "pending").glob("*.json")) == before


def test_the_api_reports_a_used_up_quota_as_its_own_status_not_a_vague_error(monkeypatch):
    import time as _t
    from fastapi.testclient import TestClient
    from wazi import api
    class Q(StubLLM):
        def complete_json(self, t, p): raise QuotaExhausted("daily quota used up")
    monkeypatch.setattr(api, "make_llm", lambda: Q("invest_all"))
    with TestClient(api.app) as c:
        rid = c.post("/api/runs", json={"ocid": "ocds-eval-abv-target", "as_of": "2025-05-01"}).json()["run_id"]
        for _ in range(100):
            r = c.get(f"/api/runs/{rid}").json()
            if r["done"]: break
            _t.sleep(0.1)
    assert r["status"] == "quota_exhausted" and "daily quota" in r["error"]
