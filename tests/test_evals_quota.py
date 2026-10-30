"""The quota-aware eval harness: tiers, dry-run, stub-for-guarantees, checkpoint/resume, budget stops."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from wazi import evals
from wazi.agent.llm import QuotaExhausted, StubLLM

ROOT = Path(__file__).resolve().parent.parent
IDS = [c[0] for c in evals.CASES]


@pytest.fixture(scope="module", autouse=True)
def _data():
    srv = evals.setup_data(); yield; srv.shutdown()


def test_tiers_are_nested_valid_and_the_model_sensitive_split_is_consistent():
    s, c, f = (set(evals.tier_cases(t)) for t in ("smoke", "core", "full"))
    assert s <= c <= f == set(IDS) and evals.MODEL_SENSITIVE <= set(IDS)
    assert {"award_request_refused", "gate_blocks_agent_names", "injected_instruction", "dead_document_link"} <= set(IDS) - evals.MODEL_SENSITIVE
    assert len(s) == 4 and len(c) == 11 and evals.tier_cases("smoke")[0] == "clean_tender"        # CASES order preserved


def test_dry_run_prices_a_real_eval_before_any_quota_is_spent():
    d = evals.dry_run(["loop"], 2, "smoke", None, rpm=10, rpd=250)
    assert d["real_model_cases"] == ["clean_tender", "short_window_flagged", "signal_kept_when_above_peers"] and d["stub_cases"] == ["award_request_refused"]
    assert d["model_runs"] == 6 and d["max_model_calls"] == 18 and d["est_tokens_out"] == 6 * 550 and d["minutes_at_rpm"] == 1.8 and d["days_at_rpd"] == 0.07
    both = evals.dry_run(["pipeline", "loop"], 1, "smoke", None)
    assert both["max_model_calls"] == 3 * (2 + 3) and "minutes_at_rpm" not in both
    full = evals.dry_run(["loop"], 1, "full", None, tok_per_sec=20)
    assert full["model_runs"] == len(evals.MODEL_SENSITIVE) and full["minutes_local"] > 0


def test_code_enforced_cases_use_the_stub_so_they_cost_no_quota():
    made = []
    def factory(): made.append(1); return StubLLM("invest_all")
    rows = evals.run_suite(factory, ["loop"], 1, {"clean_tender", "award_request_refused", "gate_blocks_agent_names"}, stub_for_guarantees=True)
    assert len(made) == 1 and {r["case"]: r["llm"] for r in rows} == {"clean_tender": "model", "award_request_refused": "stub", "gate_blocks_agent_names": "stub"}
    assert all(r["passed"] for r in rows)
    made.clear(); evals.run_suite(factory, ["loop"], 1, {"award_request_refused"}, stub_for_guarantees=False); assert len(made) == 1       # --all-real


class Dying(StubLLM):
    """Free tier runs out after `limit` model calls in total."""
    used = 0
    def __init__(self, limit): super().__init__("invest_all"); self.limit = limit
    def complete_json(self, task, payload):
        Dying.used += 1
        if Dying.used > self.limit: raise QuotaExhausted("daily quota used up")
        return super().complete_json(task, payload)


def test_a_quota_stop_checkpoints_everything_finished_and_resume_redoes_only_the_rest(tmp_path):
    ck = tmp_path / "ck.jsonl"; cases = {"clean_tender", "short_window_flagged", "signal_kept_when_above_peers"}
    Dying.used = 0
    rows = evals.run_suite(lambda: Dying(7), ["loop"], 1, cases, checkpoint=str(ck))               # loop runs use ~3 calls: two finish, third dies
    done = [r for r in rows if not r.get("not_run")]; stop = rows[-1]
    assert stop["not_run"] and "quota" in stop["reason"] and 1 <= len(done) < 3
    saved = [json.loads(l) for l in ck.read_text(encoding="utf-8").splitlines()]
    assert [r["case"] for r in saved] == [r["case"] for r in done] and not any(r.get("not_run") for r in saved)
    made = []
    def fresh(): made.append(1); return StubLLM("invest_all")
    rows2 = evals.run_suite(fresh, ["loop"], 1, cases, checkpoint=str(ck))
    assert sum(bool(r.get("resumed")) for r in rows2) == len(done) and len(made) == 3 - len(done)
    assert len(rows2) == 3 and all(r["passed"] for r in rows2) and len(ck.read_text(encoding="utf-8").splitlines()) == 3


def test_a_torn_checkpoint_line_only_means_that_one_run_is_redone(tmp_path):
    ck = tmp_path / "ck.jsonl"; evals.run_suite(lambda: StubLLM("invest_all"), ["loop"], 1, {"clean_tender"}, checkpoint=str(ck))
    with open(ck, "a", encoding="utf-8") as f: f.write('{"mode": "loop", "case": "clos')
    rows = evals.run_suite(lambda: StubLLM("invest_all"), ["loop"], 1, {"clean_tender", "closed_deadline"}, checkpoint=str(ck))
    assert [bool(r.get("resumed")) for r in rows] == [True, False]


def test_a_budget_hit_on_the_very_first_call_leaves_an_empty_checkpoint_and_a_clear_report(tmp_path):
    ck = tmp_path / "ck.jsonl"; Dying.used = 0
    rows = evals.run_suite(lambda: Dying(0), ["loop"], 1, {"clean_tender"}, checkpoint=str(ck))
    assert rows[-1]["not_run"] and not ck.exists()
    text = evals.report(rows, ["loop"], 1, "some-model")
    assert "INCOMPLETE" in text and "resume" in text.lower() and "not_run" not in text


def test_report_marks_stub_run_cases_and_cached_responses_honestly():
    rows = evals.run_suite(lambda: StubLLM("invest_all"), ["loop"], 1, {"clean_tender", "award_request_refused"}, stub_for_guarantees=True)
    text = evals.report(rows, ["loop"], 1, "qwen3:4b")
    assert "| award_request_refused | 1/1 ‡ |" in text and "| clean_tender | 1/1 |" in text and "‡ are enforced by code" in text
    assert "responses served from the cache" in text and "Stub model" not in text
    assert "Stub model" in evals.report(rows, ["loop"], 1, "stub")


def test_cli_dry_run_prints_the_plan_and_calls_no_model():
    out = subprocess.run([sys.executable, "-m", "wazi.evals", "--dry-run", "--tier", "smoke", "--modes", "loop", "--runs", "3", "--rpm", "10", "--llm", "ollama"],
                         capture_output=True, text=True, cwd=ROOT)
    assert out.returncode == 0 and '"max_model_calls": 27' in out.stdout and "No model was called" in out.stdout


def test_report_warns_that_cached_replays_say_nothing_about_run_to_run_variance():
    rows = [{"mode": "loop", "case": "clean_tender", "run": k, "passed": True, "llm": "model", "cached": 3} for k in range(3)]
    assert "Variance caveat" in evals.report(rows, ["loop"], 3, "m")
    assert "Variance caveat" not in evals.report(rows, ["loop"], 1, "m")
    assert "Variance caveat" not in evals.report([{**r, "cached": None} for r in rows], ["loop"], 3, "m")


def test_evals_md_is_generated_from_results_and_refuses_the_stub(tmp_path):
    rows = evals.run_suite(lambda: StubLLM("invest_all"), ["pipeline", "loop"], 1, {"clean_tender", "paraphrased_award_request", "award_request_refused"}, stub_for_guarantees=True)
    with pytest.raises(ValueError, match="stub"):
        evals.write_evals_md(rows, ["pipeline", "loop"], 1, "stub", tmp_path / "E.md")
    with pytest.raises(ValueError, match="no completed"):
        evals.write_evals_md([{"mode": "loop", "case": "x", "run": 0, "not_run": True, "reason": "q"}], ["loop"], 1, "qwen3:4b", tmp_path / "E.md")
    text = evals.write_evals_md(rows, ["pipeline", "loop"], 1, "qwen3:4b", tmp_path / "E.md", hardware="RTX 3050 Ti 4 GB", meta={"tier": "smoke"})
    assert (tmp_path / "E.md").read_text(encoding="utf-8") == text
    for need in ("# EVALS: qwen3:4b", "RTX 3050 Ti 4 GB", "tier: smoke", "Data version:", "code version:", "## Per-case spread", "## Failures left unfixed",
                 "paraphrased_award_request", "intent classifier", "Trace run ids:", "What this does not show", "ran with the stub"):
        assert need in text, need
    assert text.lower().count("pass") >= 8 and "unfixed" in text.lower()
    assert "- clean_tender (loop): pass 1 of 1" in text


def test_evals_md_reports_an_incomplete_run_honestly(tmp_path):
    rows = evals.run_suite(lambda: StubLLM("invest_all"), ["loop"], 1, {"clean_tender"}) + [{"mode": "loop", "case": "closed_deadline", "run": 0, "not_run": True, "reason": "daily quota used up"}]
    assert "**INCOMPLETE:** daily quota used up" in evals.write_evals_md(rows, ["loop"], 1, "m", tmp_path / "E.md")


def test_cli_refuses_to_write_evals_md_from_the_stub(tmp_path):
    out = subprocess.run([sys.executable, "-m", "wazi.evals", "--llm", "stub", "--modes", "loop", "--runs", "1", "--tier", "smoke", "--write-evals", str(tmp_path / "E.md")],
                         capture_output=True, text=True, cwd=ROOT)
    assert out.returncode == 2 and "refusing to write EVALS.md" in out.stderr and not (tmp_path / "E.md").exists()
