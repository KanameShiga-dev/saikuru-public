# Requested decision-layer verification

## Completed

Inspected all decision call sites. Agent routing is connected; task, Skill, model
and loop routing are not connected. The KINDS enum is a contract, not integration.

## Files and environment changed

Added test_decisions.py and this record, plus isolated .test-output evidence.
Production implementation/configuration/database were not changed or restarted.

## Delegation

No assistant subagent delegation. The native Agent Team fixture launched its four
expected Codex worker sessions as part of the explicitly requested runtime check.

## Validation performed

- Existing native-runtime/engine regressions: 22/22 passed.
- Offline decision tests: 9 test methods; 7 passed, 1 failed, 1 errored.
  The passing matrix covers 120 kind/mode/shadow/risk combinations. These are
  fake-provider contract cases, not semantic golden cases or 120 live requests.
- Failure: unexpected provider RuntimeError escapes rather than safe fallback.
- Failure: a late provider result can be accepted beyond the monotonic deadline.
- HTTP assertions: 4 passed; teardown failed because handoff.sqlite3 stayed open.
  This is an isolated test cleanup error, not evidence of production outage.
- Native Codex fixture: planner/explorer, implementer, reviewer and security-reviewer
  completed with four distinct IDs; fixture marker changed and job was accepted.
  Default auto/shadow, no API credentials: existing-rule fallback was exercised.
- Production health returned HTTP 200; aggregate showed no production decision records.
- Luna request serialization was verified with a mocked transport, not the network.

Evidence: .test-output/decision-layer-verification-20260930/report.json and
offline-tests.txt; .test-output/decision-layer-native-20260930/report.json.

## Human-centered considerations

Do not label these results complete or activate semantic routing based on this run.
Missing integration and provider failures remain explicit remediation items.

## Security and privacy

Checked API-key presence only, never printed credentials. OPENAI_API_KEY absent in
process, user and machine environments. No Luna/API request was made. Native fixture
used synthetic project data, no production project edits or Computer Use.

## Residual risks and unverified items

Luna live verification blocked on API credentials. User was asked to configure the
key privately; CLI login is not equivalent. Decisions API remains an unavailable stub.
Four route integrations, the two router failure cases, HTTP fixture cleanup and
semantic accuracy evaluation remain unfinished. Native success proves fallback-chain
execution, not provider decision quality. No fixes were included in this inspection.

## Recovery or rollback

Production was unchanged. Keep evidence and tests for reproduction; no rollback is
required. Native fixture DB and outputs remain separate from production records.
