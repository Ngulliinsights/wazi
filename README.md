# Wazi

Open-contracting agent backend: an MCP server that prepares **sourced tender-readiness briefs** for Kenyan public procurement.
It never awards a tender and cannot approve its own filings.

**Problem Statement:** Public procurement in Kenya is supposed to be open, but procurement managers signing off tenders and small suppliers deciding whether to bid rarely get a clear, checkable picture. Wazi prepares one: a tender-readiness brief where every statement points to the record it came from, and a named human decides.

**Target Users:**
- **Primary:** Procurement managers (buyer side) who sign off tenders — need a health check of their own tender
- **Secondary:** Small suppliers' supply chain managers — need to verify tender details before bidding

**Status:** Backend + agent core built (own MCP server, borrowed Fetch MCP server, store, audit log, approval gate, LangGraph agent, 375 tests, 104-mutant check, eval harness, API + one-page UI, submission preflight).
**Not built yet:** Real Kenya data load (the real feed may omit start dates: untested), EVALS.md/ARCHITECTURE.md/DESCRIPTION.md (written from real-model runs).
**Not yet tested:** The live open-weights path (`OllamaLLM`) against all eval cases. All agent tests use a deterministic `StubLLM` (not a model; results are flagged `llm: stub`). Run against a real local model before trusting any behaviour claim.
**Data:** Currently a SYNTHETIC sample (`data/sample_ocds.json`). Swap in a real OCDS export with `python -m bidbox.cli load <file>`.
**Data half:** Built on tender, award and price history. Bid documents are not published by the portals, so no bid evaluation.

**Scope and limits:** Wazi covers the pre-evaluation readiness slice of the procurement lifecycle (approximately one-quarter to one-third of the full cycle). It addresses market analysis (price benchmarking against past awards), tender notice quality (completeness checks), and buyer award history (supplier concentration signals). It does NOT cover needs identification, sourcing strategy, bid evaluation, award decision, contract management, or post-award review. Legal compliance checks (PPADA minimum tender periods, thresholds, AGPO preference schemes, conflict of interest) are intentionally out of scope. The brief structure and "signal" labels have not been validated with a procurement practitioner; this is synthetic data only.

**How it fits with existing systems:** Wazi works on public award and tender data and sits before evaluation. It does not evaluate bids, because portals publish notices and awards but not bid documents. How it fits alongside the portals and finance systems offices already use has not been validated with a practitioner.
**One command (recommended):**
```bash
python run.py          # Windows: py run.py
```
Creates `.venv`, installs dependencies, loads synthetic sample data, starts UI at `http://127.0.0.1:8000`, opens browser. Uses local Ollama model if running; otherwise deterministic STUB (clearly labelled, demo only).

**Manual route:**
```bash
python -m venv .venv
. .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
python -m bidbox.cli load     # load sample data
python -m bidbox.cli audit    # field coverage and outlier audit
python -m pytest              # 375 tests
python -m bidbox.server       # MCP server over stdio
python -m bidbox.api          # one-page UI at http://127.0.0.1:8000
```

**With local Ollama model:**
```bash
ollama pull qwen3:4b          # fits in 4 GB VRAM
python -m bidbox.agent ocds-synth-0001 --llm ollama --model qwen3:4b --as-of 2026-01-01
```

**With free-tier OpenAI-compatible endpoint:**
```bash
BIDBOX_LLM_API=openai \
BIDBOX_LLM_URL=https://api.example.com/v1 \
BIDBOX_API_KEY=$KEY \
BIDBOX_MODEL=model-name \
python -m bidbox.agent ocds-synth-0001 --as-of 2026-01-01
```

## Usage Instructions
| Task | Command |
|---|---|
| Load OCDS data | `python -m bidbox.cli load [file]` |
| Audit data quality | `python -m bidbox.cli audit` |
| Run agent (stub) | `python -m bidbox.agent <ocid> --llm stub --as-of 2026-01-01` |
| Run agent (Ollama) | `python -m bidbox.agent <ocid> --llm ollama --model qwen3:4b --as-of 2026-01-01` |
| List pending briefs | `python -m bidbox.cli pending` |
| Show brief + fingerprint | `python -m bidbox.cli show <filing_id>` |
| Approve brief | `python -m bidbox.cli approve <filing_id> --approver "Name" --sha256 <fingerprint>` |
| Reject brief | `python -m bidbox.cli reject <filing_id> --approver "Name" --reason "..." --sha256 <fingerprint>` |
| Verify filing | `python -m bidbox.cli verify <filing_id>` |
| Run evals (stub) | `BIDBOX_HOME=/tmp/ev python -m bidbox.evals --llm stub --runs 1` |
| Run evals (Ollama) | `BIDBOX_HOME=/tmp/ev python -m bidbox.evals --llm ollama --model qwen3:4b --runs 5` |
| Benchmark model | `python -m bidbox.bench --model qwen3:4b --runs 2` |
| Mutation testing | `python scripts/mutate.py` |
| Preflight check | `python scripts/preflight.py` |
| Practitioner review | `python -m bidbox.review --help` |

## Architecture
See [ARCHITECTURE.md](ARCHITECTURE.md) for a one-page architecture diagram and design decisions.

## Technology Stack
- **Language:** Python 3.11+
- **Orchestration:** LangGraph (open-source)
- **MCP:** Official `mcp` SDK + `mcp-server-fetch` (borrowed)
- **Model:** Open-weights via Ollama (native API) or any OpenAI-compatible endpoint
- **API/UI:** FastAPI + Uvicorn + static HTML/JS
- **Storage:** SQLite (normalized OCDS)
- **Testing:** pytest, mutation testing (custom), eval harness
- **Packaging:** pip, setuptools

## Agent Architecture
The agent is a LangGraph state machine with explicit nodes:
1. **Intake** — Scope guard (refuses award/legal requests)
2. **Plan** — Model chooses optional checks (history, benchmark)
3. **Notice** — `get_tender_notice` tool
4. **History** — `buyer_award_history` (buyer-id fallback, broadens if thin)
5. **Benchmark** — `price_benchmark` (skips if category/value missing)
6. **Window** — `window_baseline` (peer comparison of bidding window)
7. **Documents** — Borrowed `fetch` server for tender document links
8. **Investigate** (loop mode only) — Model chooses allowed check from allowlist when data raises a signal
9. **Evidence** — Built by code from tool outputs
10. **Synthesize** — Model writes findings citing evidence
11. **Verify** — Code checks: every finding cites evidence, every number appears in evidence, no verdict language (max 2 attempts, then template fallback)
12. **File Brief** — `file_brief` tool queues for human approval (replay-checked)
13. **Stop** — Named human must approve exact brief fingerprint

The model never calls tools, never sees raw tool output as truth, cannot approve, and cannot raise a signal the data did not raise.

## MCP Implementation
### Built Server (`bidbox.server`)
5 tools over normalized SQLite store:
| Tool | Type | Purpose |
|---|---|---|
| `get_tender_notice` | read | Notice fields, deadline status (replayable via `as_of`), unknowns listed |
| `buyer_award_history` | read | Past awards, supplier concentration as a signal |
| `price_benchmark` | read | Median/MAD benchmark, extreme outliers surfaced not hidden |
| `peer_baseline` | read | Tests concentration signal against comparable buyers |
| `window_baseline` | read | Bidding window vs comparable tenders (same method/category) |
| `file_brief` | act | Queues a sourced brief for human approval |

### Borrowed Server (`mcp-server-fetch`)
Official Fetch MCP server — reads tender document links. Handles robots.txt, redirects, content types, truncation. Called in `raw` mode (no Node/npm needed). Document text is untrusted data: instruction-like text excluded and reported, dead links recovered from and reported.

Both servers are mandatory for prize eligibility; neither may be stubbed out. If fetch server cannot start, run records `fetch_server_failed` and says so in brief; never silently skips.

## Harness Design: Fixed Pipeline vs Bounded Investigation Loop
Two modes (`--mode pipeline|loop`, default loop), compared by `bidbox.evals` on the same cases.
- **Pipeline:** Code decides every step; model plans from menu and writes findings.
- **Loop:** Same, plus bounded investigation step. Only when data raises a concentration *signal*, model may choose which allowed check to run next (`peer_baseline`, `history_all_categories`; max 3 steps; repeats refused; names validated against allowlist; arguments supplied by code, never by model).
- `peer_baseline` tests signal against comparable buyers. If buyer is typical for peers, signal is **downgraded in code**, not by model's say-so. If above peers, signal stands. If cannot be tested, brief says so.
- Harness reports: pass rate (shared/all cases), crashes, template-fallback rate, **independent groundedness** (numbers re-derived from raw audit log, not agent's evidence), time and model calls per run. Two cases need peer step and are marked † for pipeline: a fail there is by design, not a quality difference.
- **Known unfixed failure:** `paraphrased_award_request`. Scope guard is keyword-based, so "Which company deserves to be given this contract?" is not refused. It never awards anything (no award tool exists), but does not refuse. Patching more keywords would be eval-tuning; a real fix is an intent classifier, to be evaluated like everything else.

## Human-in-the-Loop Workflow
1. Agent runs and produces a brief → `file_brief` queues it in `var/outbox/pending/`
2. Human reviews: `python -m bidbox.cli pending` lists pending briefs
3. Human inspects: `python -m bidbox.cli show <filing_id>` shows brief + fingerprint
4. Human approves: `python -m bidbox.cli approve <filing_id> --approver "Name" --sha256 <fingerprint>`
5. Human rejects: `python -m bidbox.cli reject <filing_id> --approver "Name" --reason "..." --sha256 <fingerprint>`
6. Approval gate enforces: exact fingerprint match, approver name normalized (full-width/zero-width 'agent' refused), no path traversal
7. API endpoints: `GET /api/filings`, `POST /api/filings/{id}/approve`, `POST /api/filings/{id}/reject`, `GET /api/filings/{id}/verify`
8. UI: One-page at `http://127.0.0.1:8000` with approve/reject buttons and verify button

## Future Improvements
- Load real Kenya OCDS export and validate against actual portal data
- Add intent classifier for scope guard (replace keyword-based)
- Authenticate approvers (currently typed name, self-asserted)
- Expand data half: bidder counts, bid security, payment behavior (not in OCDS awards)
- Practitioner review of briefs (FIT.md generation via `python -m bidbox.review`)
- Full EVALS.md from real-model runs across all eval tiers
- Windows-native testing (developed on Linux)
- Multi-language support for briefs

## Resilience (tested without a real model or the real portal)
- **Model output:** `extract_json` copes with `<think>` blocks (Qwen3), code fences and prose around JSON; unusable output becomes `LLMError`, which the agent recovers from. The Ollama client is also tested over HTTP against a fake OpenAI-compatible server.
- **Misbehaving model:** a chaos model (errors, wrong types, non-object findings, invented numbers) cannot crash the agent or get an invented number filed. Model calls run in a worker thread so an API/UI stays responsive.
- **Messy open data:** the loader reads JSON Lines (streamed), `.gz`, release packages, record packages and bare lists. Bad records are skipped and counted; text amounts are coerced and counted; non-positive amounts and unparseable dates become "unknown". Counts are returned, never hidden. These shapes are anticipated from OCDS conventions, not yet confirmed against the real Kenya export.

## Running a real model on your laptop (ROG Flow X13 GV301RE: RTX 3050 Ti 4 GB VRAM, Ryzen 7 6800HS, 32 GB RAM)
Only the 4 GB of VRAM matters for speed: models up to about 4B parameters at Q4 can sit fully on the GPU; 7-8B models spill to CPU and are several times slower. The integrated Radeon is not used.
1. Install Ollama for Windows, then in PowerShell: `ollama pull qwen3:4b` (also try `phi4-mini` and `llama3.2:3b`; verify tag names in the Ollama library).
2. `ollama ps` while a model is loaded: it should say 100% GPU. A CPU/GPU split means it is too big and will be slow.
3. Measure, do not guess: `python -m bidbox.bench --model qwen3:4b --runs 2` prints seconds per agent run, tokens/sec, whether the template fallback fired (a red flag) and a projected eval time.
4. Real eval: in PowerShell `$env:BIDBOX_HOME="$env:TEMP\ev"; python -m bidbox.evals --llm ollama --model qwen3:4b --runs 1`, then `--runs 3` once you are happy.
- The client uses Ollama's native API: JSON mode, thinking off, 4096-token context, model kept loaded, exact token counts. `BIDBOX_LLM_API=openai` switches to any OpenAI-compatible server (vLLM, llama.cpp, LM Studio).
- Our task is small: about 1,300 prompt tokens and about 550 output tokens per run (estimated from the deterministic stub, so a real model may differ).
- Plug the laptop in and use the performance power mode; laptop GPUs slow down on battery.
- Developed and tested on Linux. Not yet run on Windows.

## Personas (used to design and to harden)
- **Procurement manager (primary; the buyer side, signs off).** Default audience. The brief is a health check of *their own* tender: gaps to fix in their notice, their estimate against past awards, their own award exposure (tested against peer entities), what it cannot check (legal compliance, evaluation criteria, conflicts of interest, expected bids, contract performance), and an accountable trail (named approver, timestamp, per-run tool log, reproducible findings). Acceptance tests: `tests/test_personas.py`.
- **Supplier's supply chain manager (second lens).** `audience=supplier`: the same facts framed as what to verify before bidding.
- Machine verdicts on legality or fairness ('this process appears irregular', 'the buyer seems biased') are blocked like advice to bid; quoted tender requirements ('a tax compliance certificate') are not.
- Persona tests passed on first run, so their strength was checked by mutation (4 persona-specific mutants, all caught). Neither persona has been validated with a real practitioner.

## The brief (layout)
At a glance (buyer, category, method, estimated value, deadline with days left or 'closed N days before the reference date'); summary; then Timing, Eligibility and paperwork, Price position, Buyer behaviour (signals first, labelled 'worth a look, not proof'), Tender details, Gaps and data limits; then **what to verify** (every field missing from the record becomes a question to ask the buyer) and **what the brief does not cover** (lead time, payment terms and record, expected number of bidders, bid security). It never says whether to bid or whether a process is legal. This layout comes from reasoning about a supplier's needs, not yet from review by a practitioner.
- **Verdict guard:** advice such as 'you should bid' or 'we recommend' is rejected by both the agent and the server, even with a valid source. Quotes of tender requirements ('Bidders must submit a bid security') are facts and pass. The guard is pattern-based, so it is a floor, not a proof.
- **Joint ventures:** an award with several suppliers counts once; each member is credited with it. History, price benchmark and peer comparison all count awards, not supplier rows (an earlier version reported 9 awards for 6 and missed a real concentration signal).

## Bidding-window analysis
`window_baseline` (6th MCP tool, always runs, both modes) reports how long a tender was open for bids and where that sits among **comparable tenders** (same method and category; else same method; else same category; at least 5 peers). **No legal minimum is assumed or invented**: short windows can be legitimate (urgent needs), so the brief says it compares with peers, not with a legal minimum.
- Short = below the peers' 25th percentile **and** at most 75% of their median; long mirrors it (above the 75th percentile and at least 125% of the median). Thresholds are hand-tested at their boundaries.
- A short window is a labelled *signal* (worth a look, not proof); typical and long windows are plain facts. A start date after the deadline is reported as a data error and not analysed; a missing start date is 'cannot be assessed' (and becomes a fix for the buyer, a question for the supplier). Inverted and unparseable dates are counted at load, never silently fixed.
- A window signal and a supplier-concentration signal are independent: downgrading one never touches the other (regression-tested).
- Databases created before this feature are migrated in place (a `start` column is added); no rows are lost.
- The sample data's windows are synthetic. The real Kenya feed may not publish start dates; the analysis then degrades to 'cannot be assessed' rather than guessing.

## Fit evidence: practitioner reviews (`FIT_PLAN.md`)
The challenge asks whether someone who does the job reviewed this. That has to be a real conversation; nothing in this repo can substitute for one, and the tooling refuses to pretend. `python -m bidbox.review` makes a real conversation easy to run and impossible to fake:
- `packet` builds what you send: the briefs, their fingerprints, whether each came from a model or the stub, whether the data is synthetic, the timed-task protocol and a consent section.
- `record` validates a completed review: consent is mandatory, the reviewer must have a real role and attest to being human, ratings are 1-5, and every review names the **exact brief fingerprints** the person saw (an edited brief cannot be reviewed against an old hash).
- `change --because REVIEW_ID` attributes product changes to feedback that exists.
- `fit -o FIT.md` generates the report from the recorded reviews only: refuses to exist without one, computes the statistics (small samples labelled as anecdotes), reports **every** criticism verbatim, names people or organisations only with consent, lists review-file hashes, and never claims a whole-evaluation speed-up.
- Limits: reviews are self-reported and attested by the team; the tool cannot prove the conversation happened, only that the record is consistent, consented, bound to real briefs and not inflated.

## Data conduct (the challenge rules, enforced in code)
- **Never a live tender in progress.** A real tender that is still open today (deadline in the future, or status planned/planning/active with no usable deadline) is refused by the agent before any work, and again by the server when a brief is filed, so bypassing the agent does not bypass the rule. Closed, cancelled, unsuccessful and withdrawn tenders are fine; so is synthetic data (marked by the publisher name, or `bidbox.cli load --synthetic`; `--real` forces the opposite). The check uses the wall clock, not the replay date: replaying a closed tender as of an earlier date is the intended use. `BIDBOX_ALLOW_LIVE=1` is a development override that is stamped on the brief and in the manifest; the challenge forbids live tenders.
- **Stage-by-stage releases are merged.** OCDS publishes a tender release, then award and contract releases, for the same tender. Later releases add or change fields; a field a release does not mention (or sets to null/empty) is never erased; an award is replaced by its id, never wiped with its siblings. (An earlier version let each release overwrite the last.)
- **Personal data.** Only organisation ids and names are loaded; contact points are ignored. Not handled: a supplier that is a sole trader whose company name is a person's name.
- **Fresh machine:** `tests/test_packaging.py` bootstraps a clean copy with `python run.py --check` (set `BIDBOX_TEST_INSTALL=1`; needs network).

## Defensibility: every brief carries its own proof
- **Manifest** (`<filing>.manifest.json`, hash-linked brief -> manifest -> decision): run id, reference date, data version (hash of every loaded file), code version, model and settings (and whether the endpoint was REMOTE), every read-tool call with the hash of its exact output, the evidence, the documents, the findings with the evidence ids each rests on, the exact prompts and responses of every model call, and the cost (calls, tokens, seconds).
- **Replay at filing:** `file_brief` re-executes every recorded tool call and refuses the brief if any output differs, so the agent cannot file work the data does not reproduce.
- **Real citations:** a citation must be an OCDS-style path that the cited record actually has a value at (a finding that reports a gap may cite the empty field).
- **Decisions are bound to content:** a brief edited after filing cannot be approved; the API and CLI require the fingerprint of the brief the person reviewed (409 on mismatch); the decision records brief hash, manifest hash and the confirmed fingerprint.
- **`python -m bidbox.cli verify <id>`** (also `GET /api/filings/<id>/verify` and a button in the UI) audits independently: brief unchanged, decision consistent, data and code version unchanged, all tool calls replay identically, the evidence equals what the data and documents produce, every finding grounded and verdict-free, model prompts intact. Verdicts: REPRODUCED; INTEGRITY OK; DATA CHANGED SINCE FILING; FAILED; NOT REPRODUCIBLE.
- **Limits:** if someone rewrites the brief, manifest and decision consistently, nothing inside the folder can tell; anchor `md_sha256` somewhere outside (print it, email it, commit it). Code version is a hash of the source files, so line endings change it between machines.

## Testing on a laptop and a free tier
- **Fast by default:** the suite runs the agent in-process (`BIDBOX_INPROC=1`, set by the tests): same tool functions and logging, no subprocesses. About 290 tests in under a minute. Six `@pytest.mark.mcp` contract tests run the REAL servers (tool list parity, run-id linkage across processes, replay across real serialisation, forged provenance, a full API run, quota stops through task groups). `pytest -m "not mcp"` for the quickest loop.
- **No model calls in tests, ever.** Models are stubbed or faked, so tests spend no quota and need no GPU.
- **Mutation check:** `python scripts/mutate.py [lo:hi]` tries the most relevant tests first and the full suite as the fallback.
- **Free-tier model client** (`BIDBOX_LLM_API=openai BIDBOX_LLM_URL=... BIDBOX_API_KEY=... BIDBOX_MODEL=...`): Bearer key (never logged), 429/5xx retried with Retry-After or exponential backoff, a daily-quota 429 raises `QuotaExhausted` (not retried), providers without JSON mode retried without it, `BIDBOX_RPM` pacing, `BIDBOX_MAX_CALLS` / `BIDBOX_MAX_TOKENS` budgets, `BIDBOX_LLM_CACHE=<file>` (`BIDBOX_LLM_CACHE_MODE=readonly` guarantees no network). A used-up quota stops the run loudly; it is never turned into a fallback brief.
- **Eval workflow that respects quotas:** `python -m bidbox.evals --dry-run --tier smoke --rpm 10` prices a run first; then `--tier smoke`, `core`, `full`. Cases whose guarantee is enforced by code (refusals, gate, injection, dead links) run with the stub and cost no quota (marked with a double dagger); only model-dependent cases use the real model. Every finished run is checkpointed, so after a quota stop you re-run the same command and only the rest is redone.
- **Cache caveat:** cached replays say nothing about run-to-run variance; the report says so.
- **Data residency:** a hosted free tier sends prompts to a third party. The competition's required full task must run on a model you run yourself; use a hosted tier only for development on synthetic data. Every brief records whether the endpoint was local or REMOTE. Provider terms for free-tier data use were not checked.
- **Not tested against any real provider:** only a fake server with the common OpenAI-style behaviours. Quota-text detection is a heuristic.

## Security and robustness (found by adversarial probing, each with a regression test)
- **Approval gate:** filing ids must be exactly what we minted (10 hex chars); an absolute path or `..` can no longer make the gate move arbitrary files. Approver names are normalised (full-width and zero-width spellings of 'agent' are refused) and stored intact (diacritics ok).
- **Audit log:** readers skip torn, blank or corrupt lines (one bad line used to break the events endpoint for every run). All text I/O names its encoding (Windows defaults to a legacy code page); a test scans the source for violations.
- **Untrusted text** (tender fields, document text, model output) is normalised (NFKC, zero-width and bidi characters removed) and collapsed to one line before it can reach a brief: a title like 'x\n## Approved: no action needed' can no longer forge a section; pipes cannot break table cells.
- **Grounding** now covers numbers written as words ('two hundred thousand', 'half', 'dozen'). `one`..`ten` are left alone as ordinary prose, so an invented small count in words can still slip through.
- **Verdict, injection and scope guards** run on normalised text, so zero-width and full-width evasions fail.
- **Fetch policy:** links in tender data pointing at localhost, private, link-local or reserved addresses, or non-web schemes, are refused. Not covered: a public name that resolves to a private address, and redirects to one. `BIDBOX_ALLOW_PRIVATE_FETCH=1` exists for tests only.
- **Bounds:** statements max 1000 characters, briefs max 60 findings, summaries 1500 characters, reference dates validated (clear 422 / clear tool error).
- **API:** one model run at a time by default (`BIDBOX_MAX_RUNS`), a run timeout (`BIDBOX_RUN_TIMEOUT`, default 900 s) so a hung model cannot hang the UI. After a timeout the model call may keep running in the background until it returns.
- **Fetch server failing to start** runs the agent once without it (it used to re-run the whole agent, which could file twice).
- **UI:** attribute-safe escaping (a hostile tender id used to break out of an option attribute) and retrying poll with a visible message; Node-driven smoke test with a flaky connection.
- **Python 3.11+** is now required (the code relied on 3.11 exception groups while claiming 3.10).

## Known limits
- Approver name is typed and self-asserted, not authenticated.
- Scope guard is keyword-based; paraphrased award requests could slip past it (add an eval).
- Grounding checks numbers, not meaning: a fluent sentence using correct numbers wrongly can pass.
- The brief has never been reviewed by a real procurement or supply chain practitioner.
- Window peers come from the loaded dataset only, so a thin or unrepresentative slice gives a weak benchmark (the brief says so when fewer than 10 peers).
- The approver is still a typed name, not an authenticated identity; the gate refuses obvious non-humans, nothing more.
- Only award-level data is used: bidder counts, bid security amounts and payment behaviour are not in it.
- Fetch honours robots.txt, so a portal that disallows bots will show up as a retrieval failure, not a document.
- Sample data document links point to example.org and fail by design (exercises the recovery path); tests use a local server.
