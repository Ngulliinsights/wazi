# Architecture (one page)

**What it is.** An agent that prepares a *sourced tender-readiness brief* for a procurement manager (primary reader) or a small supplier. A named human decides. It never awards, ranks bidders, advises to bid, or judges legality, and no tool exists that could.

**Flow.** intake (scope guard) -> plan (model, falls back to a default) -> notice -> buyer history (buyer-id fallback; widens if thin) -> price benchmark -> bidding window -> documents (borrowed Fetch server) -> investigate (loop mode only: when the data raises a signal, the model chooses a check from an allowlist) -> evidence (built by code) -> synthesize (model) -> verify (code, two attempts, template floor) -> `file_brief` (replay-checked) -> **stop** -> a person approves the exact brief fingerprint.

**Components.**
- **Own MCP server** (`bidbox.server`): five read tools (`get_tender_notice`, `buyer_award_history`, `price_benchmark`, `peer_baseline`, `window_baseline`) over a normalised SQLite store, and one acting tool (`file_brief`). Narrow tools, not a proxy of any portal.
- **Borrowed MCP server**: the official `mcp-server-fetch`, used to read tender document links. Robots.txt, redirects, content types and truncation are done and maintained by others; rewriting them would add risk and no value. Document text is untrusted data.
- **Orchestration**: LangGraph (explicit graph, bounded retries, human stop). **Model**: open weights, run locally by default (native Ollama API, or any OpenAI-compatible endpoint); free-tier aware (keys, backoff, quotas, cache); every brief records local or REMOTE.
- **Gate, API, UI**: the approval gate is not an MCP tool, so the agent cannot approve itself. FastAPI plus one static page.

**Where the model sits.** Two or three narrow JSON jobs. It never calls tools, never sees raw tool output as truth, cannot approve, and cannot raise a signal the data did not raise.

**Guarantees enforced in code, not prompts.** Every finding cites evidence and a real citation (a field the cited record actually has); every number, in digits or words, must appear in its cited evidence; no verdict or advice language; award requests refused; untrusted text normalised to one line; document links to private addresses refused; a used-up quota stops the run instead of filing a fallback; replay at filing; decisions bound to the brief's content hash.

**Defensibility.** Each filing carries a hash-linked manifest (run, data version, code version, model and settings, every tool call with output hash, evidence, exact prompts and responses, cost). `verify` replays the tool calls, re-derives the evidence, and re-checks every finding. 104 mutants and ~290 tests guard these properties.

**Decisions considered and rejected.** A red-flag accuser (false-accusation risk) for a readiness brief. Fixed legal minimum windows (jurisdiction-dependent) for peer comparison. A free tool-calling agent on a small model for code-driven calls with model-written JSON. A multi-agent critic for one bounded loop plus a code verifier. LLM-as-judge for programmatic checks. A hosted-only model (procurement data often cannot leave the country) for local-first. The archived SQLite MCP server for Fetch. A React build for one static page.

**Known limits.** Synthetic data and a small case set; the real feed is untested. Number grounding is checked, meaning is not. The approver is a typed name. No practitioner has reviewed the briefs. Tamper evidence ends where someone rewrites brief, manifest and decision together: anchor the fingerprint outside the folder.
