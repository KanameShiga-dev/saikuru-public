# Ollama Decision Layer — 2026-09-30

Operational selection: user selected local Ollama only. Default provider and the
Start-AgentTeam.ps1 launch environment now specify ollama; shadow=true, API calls
disabled. No automatic cloud fallback. On local failure the shadow preserves the
baseline and records the error. Auto mode remains available only as an explicit
configuration alternative. Other launchers/environment overrides can bypass these
operational defaults; they are not administrator enforcement.

UI update: Team settings now includes Decision Provider, Tev1 model, fixed shadow
indicator, Save and local connection check. Saved config.decision takes precedence
over launch environment and applies to tasks as they begin, without restarting.
Older config files fall back to local Ollama/0.8b/shadow=true. Save uses the existing
authenticated/CSRF-protected settings path and config backup mechanism. Cloud settings
never enable API approval or write credentials. Preview unavailable; 4b is not auto-pulled.
UI changes received Python/JavaScript syntax and server startup checks; no browser
interaction or new regression test was performed for this UI request.

## Completed

Added OllamaSystemOneProvider using the officially documented /v1/systemone API.
Auto order: Ollama → Decisions → Luna → HUMAN. Mandatory risk gates precede the
provider chain. Shadow mode preserves existing decisions. Modes:
auto/ollama/decisions/luna/disabled. Fixed unexpected-provider exceptions and rejection
of responses arriving after the total monotonic deadline. No deadline claim applies
to the wall time of blocking OS DNS/socket calls.

Choice batch supports 1–64 related requests with identical metadata. HUMAN is added
as an escape option (Tev1 needs at least two choice options). Batch API is available
but the engine currently uses a single Agent-route request, not all route kinds.
Noul/score are not used to determine permissions or risk: deterministic gates stay
authoritative. Task/Skill/model/loop integrations remain pending from the earlier work.

## Files and environment changed

team_ollama_decisions.py, team_decisions.py, decision.env.example, decision_metrics.py,
web/app.js, test_decisions.py, test_ollama_decisions.py and these docs. No SDK dependency.
Settings are process environment variables; example env is not loaded automatically.
Default local model tev1:0.8b; selectable accurate model tev1:4b. Allowed model names
are deliberately bounded to these two. Ollama defaults enabled, shadow defaults true.

Initial version found: CLI/server 0.34.4. Required version: 0.35+. Downloaded official
0.35.0 installer to data/backups/ollama-decision-20260930/OllamaSetup-0.35.0.exe.
Authenticode verified Valid, signer Ollama Inc. Installer execution was rejected by
automatic approval review as blocked by policy; no bypass attempted. User then ran
the installer and reported completion. CLI AND server verified 0.35.0. Pulled
tev1:0.8b successfully (812MB download, digest d45e875d63fe in ollama ps).
No loaded model was present before updating; other installed models were preserved.

## Delegation

No subagents used.

## Validation performed

31 existing/decision test methods plus 6 Ollama tests passed; the decision matrix
inside these includes 120 mode/kind/risk/shadow contract cases. Transport tests use
mocks and are not Japanese semantic accuracy results. JavaScript syntax passed.
Ollama version/list/loaded model status were read from this PC.

Actual System One smoke succeeded (Japanese typo task → DIRECT), cold latency 1870ms.
Four semantic smoke cases: Japanese simple/investigation/ambiguous all correct; the
embedded-instruction injection case FAILED (EXPLORER rather than DIRECT). Result 3/4
is a smoke observation, not a statistically meaningful accuracy estimate.
Warm latencies 444–555ms. Actual production-contract researcher/builder calls returned
valid Ollama choices in 456/422ms with shadow=true and unchanged effective Agents.
Local input/output usage and confidence/probabilities were returned and recorded.
No cloud calls. ollama ps: 896MB loaded footprint, 100% CPU, context 2050 tokens.
This is model residency metadata, not peak RAM/CPU utilization benchmarking.
Evidence: .test-output/ollama-decision-20260930/smoke.json and evaluation.json.

## Human-centered considerations

Board distinguishes local versus cloud tokens and labels confidence as concentration,
not correctness probability. Current inference metadata is only role/operation;
meaningful Japanese task-context routing needs separately reviewed context integration.
Do not claim token savings or enable live decision adoption from mock tests.

## Security and privacy

Local URL is restricted to HTTP loopback only, with proxies and redirects disabled.
No remote Ollama server, cloud-tagged model or model auto-pull is allowed. Actual
endpoint requests establish service/model usability; the enabled flag only allows
attempting a request. No separate inference probe doubles each route's workload.
Confidence/probabilities are telemetry, not acceptance gates. API fallback still needs
DECISION_API_CALLS_APPROVED=true and a private OPENAI_API_KEY. Both remained disabled/
absent, so no paid API call was made. High-risk batch requests are rejected before HTTP.

## Residual risks and unverified items

Upgrade, tev1:0.8b pull and System One smoke completed.
Model downloading consumes local disk/network, not OpenAI API quota. Optional 4b is
not downloaded automatically. Japanese/injection semantic evaluations (100 cases),
0.8b/4b resource/latency comparison and cloud comparison are not yet performed.
Injection failure means semantic adoption is NOT approved; keep shadow=true. Risk gates
remain authoritative. Environment settings can disable shadow, so this is an operational
default, not administrator enforcement. Do not enable adoption without passing evaluations.
Cold model startup may exceed 1200ms and cause safe fallback. Keep warm only as needed.
Tev1 is experimental; never the sole safety classifier. System One batch currently
implements choice; noul/score expansion awaits an actual routing need.

Official specification: https://ollama.com/library/tev1 . Official latest release
checked via https://api.github.com/repos/ollama/ollama/releases/latest (v0.35.0).

## Recovery or rollback

Set DECISION_PROVIDER=disabled or OLLAMA_DECISION_ENABLED=false and restart Agent Team
while idle to stop local semantic decisions. Preserve newer edits before restoring
team_decisions.py from data/backups/ollama-decision-20260930/. Existing model files
were not deleted. Backup of old CLI binary exists; it alone is not a complete rollback
of the installed Ollama runtime. For a full downgrade use official 0.34.4 installer
and preserve the model directory. No security-policy changes are needed.
