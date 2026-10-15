"""The local-model client: native Ollama API (JSON mode, thinking off, stats) and the OpenAI-compatible path."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from bidbox.agent.llm import LLMError, OllamaLLM

OBJ = '{"checks":["history","benchmark"],"rationale":"r"}'


@pytest.fixture
def server():
    state = {"script": [], "seen": []}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["seen"].append((self.path, body))
            status, payload = state["script"].pop(0)
            self.send_response(status); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(json.dumps(payload).encode())
        def log_message(self, *a): pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{srv.server_address[1]}"
    yield state
    srv.shutdown()


def ok(content=OBJ, **kw):
    return (200, {"message": {"content": content}, "done_reason": "stop", "eval_count": 120, "prompt_eval_count": 800, "total_duration": 4_000_000_000, **kw})


def test_native_request_asks_for_json_with_thinking_off_and_a_fixed_context(server):
    server["script"] = [ok("<think>hmm</think>" + OBJ)]
    llm = OllamaLLM("qwen3:4b", server["url"])
    assert llm.complete_json("plan", {"facts": "x"})["checks"] == ["history", "benchmark"]
    path, body = server["seen"][0]
    assert path == "/api/chat" and body["format"] == "json" and body["think"] is False and body["stream"] is False
    assert body["options"] == {"temperature": 0, "num_ctx": 4096, "num_predict": 1400} and body["keep_alive"]
    assert "/no_think" not in json.dumps(body)                          # only added on the OpenAI-compatible path


def test_token_and_time_stats_are_recorded_for_planning_eval_time(server):
    server["script"] = [ok(), ok(eval_count=80, prompt_eval_count=500, total_duration=2_000_000_000)]
    llm = OllamaLLM("m", server["url"]); llm.complete_json("plan", {"facts": "x"}); llm.complete_json("plan", {"facts": "x"})
    assert (llm.calls, llm.tokens_in, llm.tokens_out) == (2, 1300, 200) and llm.model_seconds == 6.0


def test_a_model_without_a_thinking_mode_is_retried_without_the_think_flag_once(server):
    server["script"] = [(400, {"error": "\"m\" does not support thinking"}), ok(), ok()]
    llm = OllamaLLM("m", server["url"])
    assert llm.complete_json("plan", {"facts": "x"})["checks"]
    assert "think" in server["seen"][0][1] and "think" not in server["seen"][1][1]
    llm.complete_json("plan", {"facts": "x"})
    assert "think" not in server["seen"][2][1]                          # remembered: no wasted round trip afterwards


def test_other_http_errors_are_llm_errors_not_crashes(server):
    server["script"] = [(500, {"error": "boom"})]
    with pytest.raises(LLMError, match="HTTP 500"):
        OllamaLLM("m", server["url"]).complete_json("plan", {"facts": "x"})
    with pytest.raises(LLMError):
        OllamaLLM("m", "http://127.0.0.1:1", timeout=2).complete_json("plan", {"facts": "x"})


def test_empty_content_and_truncated_output_are_named_failures(server):
    server["script"] = [ok(content="")]
    with pytest.raises(LLMError, match="empty content"):
        OllamaLLM("m", server["url"]).complete_json("plan", {"facts": "x"})
    server["script"] = [ok(content=OBJ[:-8], done_reason="length")]
    with pytest.raises(LLMError, match="cut off"):
        OllamaLLM("m", server["url"]).complete_json("plan", {"facts": "x"})


def test_openai_compatible_path_still_works_and_only_qwen3_gets_the_soft_switch(server):
    for model, expect in (("qwen3:8b", True), ("llama3.2:3b", False)):
        server["script"] = [(200, {"choices": [{"message": {"content": OBJ}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 50, "completion_tokens": 20}})]
        llm = OllamaLLM(model, server["url"], api="openai"); assert llm.complete_json("plan", {"facts": "x"})["checks"]
        path, body = server["seen"][-1]
        assert path == "/v1/chat/completions" and body["response_format"] == {"type": "json_object"}
        assert body["messages"][-1]["content"].endswith("/no_think") == expect
        assert (llm.tokens_in, llm.tokens_out) == (50, 20)


def test_bench_script_measures_and_projects_without_a_model():
    from bidbox import bench, evals
    from bidbox.agent.llm import StubLLM
    srv = evals.setup_data()
    try:
        res = bench.bench(lambda: StubLLM("invest_all"), runs=1, warm=False)
    finally:
        srv.shutdown()
    assert res["runs"] == 3 and res["seconds_per_run"] > 0 and res["fallbacks"] == 0 and res["not_filed"] == 0
    assert set(res["projected_minutes"]) == {"1 run(s) per case", "3 run(s) per case", "5 run(s) per case"}
    pm = res["projected_minutes"]      # each value is rounded to 0.1 min, so compare with the rounding error, not a relative %
    assert abs(pm["3 run(s) per case"] - 3 * pm["1 run(s) per case"]) <= 0.02       # rounded to 0.01 min
    assert abs(pm["5 run(s) per case"] - 5 * pm["1 run(s) per case"]) <= 0.03
