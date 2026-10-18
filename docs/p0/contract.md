# P0 job/UI contract (2026-10-18)

Base: `p0/base` from local public `6101fc1`, plus selected No.007 runtime, safety, approval, and regression changes. The authoritative commit is the current `p0/base` HEAD. This is a local implementation base; no live 8790 source or runtime database is changed.

## Existing fields retained

- `job.goal`: immutable original request for a P0 development job.
- `job.status`, `task.status`, `task.after`, `approval.kind/status`, and the existing G2/G3 revision and fingerprints retain their meanings.
- The `jobs`, `tasks`, and `approvals` arrays remain at the top level of `/api/state`.
- Formal acceptance remains a separate explicit user action. A successful task or review does not accept the job.

## New development jobs

`job.p0_contract_version === 1` identifies new full-route development jobs. Legacy and document jobs keep their established paths. A new job stores `job.goal_contract` with:

| Key | Type | Meaning |
|---|---|---|
| `original_request` | string | Same request text as `job.goal`; never changed by a model |
| `primary_goal` | string | User-visible objective derived from the request |
| `deliverables` | string[] | Expected final deliverables; filled by the planner before G3 |
| `acceptance_criteria` | string[] | Observable completion conditions; filled by the planner before G3 |
| `non_goals` | string[] | Explicit exclusions; empty if none |
| `constraints` | string[] | Original request and policy limits |
| `completion_status` | string | `in_progress`, `awaiting_user_acceptance`, or `accepted` |

`primary_goal`, deliverables, and acceptance criteria in the AI plan are proposals until the existing G3 plan approval. They cannot silently replace `original_request`. If the planner omits deliverables or acceptance criteria, G3 is not issued. Older jobs without `p0_contract_version` are displayed with legacy status and are not migrated implicitly.

## Decision records

New approvals carry `category`, `intended_role`, `business_summary`, and `requires_explicit_consent`. Categories are `BUSINESS`, `ACCEPTANCE`, `RISK`, `TECHNICAL`, `MAINTENANCE`, `IMPROVEMENT`; roles are `end_user`, `authorized_person`, `maintainer`, and `none`. Existing `kind`, `payload`, and digest remain authoritative for the operation. Unclassified or suspicious questions are held for maintenance rather than shown as an end-user decision. The API checks the role before applying a decision; a browser-supplied role label is never trusted as authorization. The standard `/api/state` response must not expose maintenance question payloads.

The P0 backend currently classifies technical repair questions as `MAINTENANCE` and separates them with the `SAIKURU_MAINTENANCE_TOKEN` capability. An operator supplies the token through the process environment at startup; the server removes it from the child process environment. Maintainer requests send it in `X-Saikuru-Maintenance`. `/api/maintenance/state` requires that capability. `/api/state`, `/api/tasks/detail`, and detailed `/api/jobs/archive` redact maintenance task and question diagnostics for standard sessions. The normal session cookie remains required for ordinary user actions. Deployment must provision the maintenance capability before relying on this route.

## Interruption and progress

`job.p0_interruption`, when present, carries `interrupted_task_id`, `interruption_reason`, `recovery_status`, `resume_target`, and a bounded failure signature/count. A transient local operation may be retried only when it is known to be idempotent. AI invocations and uncertain writes are never replayed automatically. The job resumes the same pending task after successful recovery; previously succeeded tasks remain succeeded.

`job.p0_progress` is a server projection derived from the current approved plan, task state, artifacts, and validation evidence. It contains `completed`, `current`, `remaining`, `next_action`, `completion_status`, and `unmet_acceptance_criteria`. The standard UI displays these fields, not internal G2/G3 codes. Unknown evidence is displayed as unverified, never as success. The server decides whether a completion approval can be issued and formally accepted; the UI cannot make that decision.

Builder reports for P0 jobs include `artifacts`, a list of project-relative files. The server stores paths and SHA-256 hashes in `job.p0_artifacts`, rejecting paths outside the project and secret or internal-data paths. The final review and formal receipt recheck those files. The completion approval retains pending launch/operation and approved acceptance checks until the user records evidence for each through the existing completion-evidence API. Evidence submission creates a new approval; the user must acknowledge its current review and explicitly accept it.

Warnings and out-of-scope improvements reuse `job.warning_issues` and do not block execution. A blocking item must identify a user requirement or acceptance criterion, a concrete effect on the current result, or an immediate safety/authorization issue. The existing safety and approval gates always take precedence.

## Fixture and interface compatibility

`tests/fixtures/p0/state.json` is synthetic UI input. It contains only invented jobs and no operational logs, tokens, or real identifiers. Claude may implement L1–L4 against that fixture while Codex implements the API. Missing P0 fields on a legacy record must not be interpreted as success. Contract changes require an updated fixture and a note to the other owner before integration.
