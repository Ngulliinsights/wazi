# WAZI

Public procurement is supposed to be open, but the people who need to read it, procurement managers signing off a tender and small suppliers deciding whether to bid, rarely get a clear, checkable picture. Wazi prepares one: a tender-readiness brief where every statement points to the record it came from, and a named human decides.

An agent reads a tender and plans its own checks. It looks up the buyer's award history, benchmarks the estimated value against past awards, measures the bidding window against comparable tenders, reads linked tender documents, and, when it sees a signal such as one supplier winning most awards, investigates whether peer buyers look the same before calling it a signal. It recovers from missing buyers, thin history, dead links and bad dates, and says what it could not check. It never awards, ranks, advises to bid, or judges legality; no tool exists that could.

It is built on MCP: our own server of narrow read tools plus one acting tool that queues a brief, and the official Fetch server for documents. LangGraph orchestrates; an open-weights model runs locally, with free-tier endpoints supported for development and recorded as REMOTE in every brief. The model only plans and writes; code enforces the rules. Every number must appear in the evidence, citations must point at fields the record really has, and verdict language is rejected.

Defensibility is the point. Each brief carries a manifest recording the data version, code version, model, every tool call with an output hash, the exact prompts, and the cost. A verify command replays the tool calls and re-derives the evidence; approval is bound to the exact brief reviewed, so an edited brief cannot be approved.

Quality is tested with regression tests, adversarial cases, and 104 deliberately broken variants of the code that the tests must catch. The eval harness is quota-aware, resumable, and prices a run before spending anything. The synthetic-model evaluation in EVALS.md is a historical baseline, not a post-change performance claim; its failures and limitations are reported.
