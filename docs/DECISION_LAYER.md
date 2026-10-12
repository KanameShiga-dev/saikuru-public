# Decision layer — 2026-09-30

Updated for local-first Ollama: see OLLAMA_DECISION_LAYER.md. The earlier auto-chain
description below is superseded: auto now tries Ollama, Decisions, Luna then HUMAN;
shadow still preserves the baseline. Unexpected exceptions and late responses now
fall back safely. API access remains opt-in, independent from local inference.

## Integration

`team_decisions.py` provides a finite-choice contract for task/agent/skill/model/loop
decisions. Currently only Agent selection at task execution is connected. Researcher
can select explorer or harness-auditor with the same read-only boundary; other roles
are pinned to their existing Agent. Security review and completion/retry gates remain
in the existing engine. No model switching, permission approval or execution tool is
implemented by this layer. Further routing points need their own adoption evaluation.

## Configuration and rollout

Defaults: auto, shadow=true, Decisions disabled, API calls disabled. A task's detail
shows provider, baseline, candidate, effective result, difference, latency and actual
API token usage if returned. Records are stored with tasks in the existing local DB;
raw prompts, responses, keys and source are not logged. Existing run history remains.

`decision.env.example` lists process environment variables; it is NOT automatically
loaded. Set them in the service launch environment and restart during an idle period.
Never put keys in a committed file. API billing is separate from CLI subscription.
Only after explicit API cost/transmission authorization set
`DECISION_API_CALLS_APPROVED=true` and privately supply `OPENAI_API_KEY`. Shadow mode
also costs API tokens when enabled. No API requests were made during implementation.

- auto: available Decisions then Luna, then existing rule baseline on failure.
- decisions: unavailable is an error; never silently uses Luna.
- luna: unavailable is an error; never silently uses Decisions.
- disabled: existing baseline, no API request.
- shadow=true: effective decision stays the baseline, including provider errors.
- shadow=false: strict-mode errors produce HUMAN and stop the task with a recoverable
  provider failure. Fix config, or select disabled, and retry from the board.

Timeout is a total monotonic request budget (100–10000ms), retries 0–1. Standard
service, no tools, store=false, bounded response/output, fixed HTTPS endpoint and no
redirects. Network/DNS operations may exceed the nominal socket timeout on some OSs;
this is not a hard process deadline. High-risk or risk-hint requests never reach a
semantic provider. HUMAN is a gate outcome, not an executable choice. Existing engine
security gates are authoritative; callers must classify risk before calling.

## Official API boundary

Luna's Responses/structured-output/none support was checked at
https://developers.openai.com/api/docs/models/gpt-6-luna and
https://developers.openai.com/api/docs/guides/structured-outputs .
No public Decisions contract was established in the official-doc search. Preview
provider always reports unavailable, even if the feature flag is true. Replace only
its implementation after endpoint/schema/auth/quota/privacy are verified; keep all
gates and contract validation. No guessed preview endpoint is used.

## Evaluation and remaining gates

No tests or paid live calls were requested or performed for this implementation.
Before adopting semantic decisions, evaluate at least: all four modes, unavailable
providers, timeout, refusal, invalid/out-of-choice JSON, high-risk context, no secret
logging, shadow invariance, researcher permissions, cancellation/retry, mandatory
security review, baseline differences and task outcomes. Compare real tokens/latency
and human interventions; do not infer quota savings from character counts. Accumulate
the guide's suggested 100 golden/shadow cases before gradual low-risk adoption.

Read-only aggregate: `python decision_metrics.py`. Counts latest task decision records,
provider failures, shadow differences, HUMAN candidates, task success, mean/p95 latency
and reported API tokens. Missing tokens remain unknown. This does not measure semantic
accuracy or causality and is not a replacement for golden cases; historical attempts
are not included in this aggregate.

## Recovery

Set DECISION_PROVIDER=disabled in the service environment and restart while idle.
Backups of engine/UI before this change are in
`data/backups/decision-layer-20260930/`. Restore only after preserving newer edits;
the module and documentation can remain unused. Database history is retained.
