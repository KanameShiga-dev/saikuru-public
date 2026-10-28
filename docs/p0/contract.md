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

Every approval created for a `p0_contract_version === 1` job includes `category` and `intended_role` in standard `/api/state`; redaction of maintenance payloads preserves those two fields. Approvals belonging to legacy jobs may omit them and retain legacy display behavior. A P0 approval missing either field is incomplete data and must not be treated as an end-user decision.

The P0 backend currently classifies technical repair questions as `MAINTENANCE` and separates them with the `SAIKURU_MAINTENANCE_TOKEN` capability. An operator supplies the token through the process environment at startup; the server removes it from the child process environment. Maintainer requests send it in `X-Saikuru-Maintenance`. `/api/maintenance/state` requires that capability. `/api/state`, `/api/tasks/detail`, and detailed `/api/jobs/archive` redact maintenance task and question diagnostics for standard sessions. The normal session cookie remains required for ordinary user actions. Deployment must provision the maintenance capability before relying on this route.

In standard `/api/state`, a maintenance task keeps `id`, `job_id`, `status`, `role`, `after`, `attempt`, safe `title`/`summary`, and empty structural values for `instruction`, `result`, `agent_name`, `common_agents`, and `profile`. The standard job omits `p0_interruption.failure_signature` and `.interruption_reason`. Technical details stay in the maintenance endpoint. An unresolved maintenance task is never an end-user approval, even when its task status is `failed` or `blocked`.

## Interruption and progress

`job.p0_interruption`, when present, carries `interrupted_task_id`, `interruption_reason`, `recovery_status`, `resume_target`, and a bounded failure signature/count. A transient local operation may be retried only when it is known to be idempotent. AI invocations and uncertain writes are never replayed automatically. The job resumes the same pending task after successful recovery; previously succeeded tasks remain succeeded.

`recovery_status` values emitted by this backend are `retrying` (automatic local recovery in progress), `maintenance_required` (bounded recovery stopped and a maintainer must act), `safety_blocked` (a safety or authorization refusal), and `recovered` (the original task has resumed). A missing interruption means normal processing. A future unknown value must be displayed as a non-success maintenance state; the UI must not infer the cause or grant an action from it.

`job.p0_progress` is a server projection derived from the current approved plan, task state, artifacts, and validation evidence. It contains `completed`, `current`, `remaining`, `next_action`, `completion_status`, and `unmet_acceptance_criteria`. The standard UI displays these fields, not internal G2/G3 codes. Unknown evidence is displayed as unverified, never as success. The server decides whether a completion approval can be issued and formally accepted; the UI cannot make that decision.

For L3 the projection also contains `available_actions` and `deliverable_access`. `available_actions` is an array of UI affordance hints: `cancel_job`, `review_completion`, and `record_completion_evidence`. It is not authorization; the existing API validates every request again. `review_completion` is emitted only for a pending completion approval intended for `end_user`; `record_completion_evidence` additionally requires `job.status === "awaiting_acceptance"` and pending checks. `completion_status` follows the server job status: `awaiting_acceptance` projects `awaiting_user_acceptance`, accepted jobs project `accepted`, and other jobs project `in_progress`.

`deliverable_access` lists registered P0 deliverables as `{path, method: "project_relative_file", launch_status: "unverified", launch_hint}`. `path` is relative to the job project. The only emitted `launch_status` is `unverified`; the server does not claim a launch result. `launch_hint` says that a launch procedure is not registered and instructs the user to verify launch and operation separately. No URL or launch command is promised by this field. `job.p0_artifacts` remains the authoritative path/hash record, and approved acceptance checks remain in the completion approval's `pending_items`.

The standard, authenticated `GET /api/job/p0-artifact?job_id=...&path=...` reads a registered P0 artifact as UTF-8 text and returns `{path, content, truncated: false}`. It accepts only a current SHA-256 match and the existing safe text-file types, with a 200,000-byte limit; unregistered, changed, oversized, binary, and sensitive-looking contents are refused. This endpoint does not launch the application. The existing `/api/job/artifact` reader continues to serve `job.artifacts` for document jobs only.

When no task is running or stopped, `current` is the next queued task and `remaining` excludes that current task. Maintenance titles are replaced with a safe label in this projection, including after a maintenance question has been resolved.

Builder reports for P0 jobs include `artifacts`, a list of project-relative files. The server stores paths and SHA-256 hashes in `job.p0_artifacts`, rejecting paths outside the project and secret or internal-data paths. The final review and formal receipt recheck those files. The completion approval retains pending launch/operation and approved acceptance checks until the user records evidence for each through the existing completion-evidence API. Evidence submission creates a new approval; the user must acknowledge its current review and explicitly accept it.

Rejecting a P0 completion approval requires a nonblank reason. The engine then creates a builder rework task followed by normal and security reviews, and returns the job to `running`. Without a reason, the API refuses the decision and leaves the approval pending. Legacy approval behavior is unchanged.

Warnings and out-of-scope improvements reuse `job.warning_issues` and do not block execution. A blocking item must identify a user requirement or acceptance criterion, a concrete effect on the current result, or an immediate safety/authorization issue. The existing safety and approval gates always take precedence.

## Fixture and interface compatibility

`tests/fixtures/p0/state.json` is synthetic UI input. It contains only invented jobs and no operational logs, tokens, or real identifiers. Claude may implement L1–L4 against that fixture while Codex implements the API. Missing P0 fields on a legacy record must not be interpreted as success. Contract changes require an updated fixture and a note to the other owner before integration.

## Workflow feature confirmation and state API (W05.01)

For `workflow_contract_version: 1`, the authenticated state feed adds `job.workflow`.
`GET /api/workflow/job?id=<job_id>` returns the same request-scoped projection.
These reads construct no worker Context, start no task, inspect no project files/Git,
and grant no execution permission. `file_validation: "not_performed"` and
`execution_permission: "none"` are explicit: write operations validate authority,
current files, target versions and stopped writers again. An action hint can become
stale between display and submission; a refusal requires refreshing the target.

The projection contains:

- Current `work_packages`: IDs/titles, goal links, done-when checks, plan/requirement
  revisions, execution/review states, current-generation task IDs, dependency IDs
  and names, and per-package `available_actions`.
- Canonical `progress` from current tasks, WPs, approvals and ordered typed decision
  events. `/api/state`'s `p0_progress` and `/api/quality/job` use these same canonical
  records, including for ended jobs whose visible task rows are slimmed. Technical
  completion, feature confirmation, UAT and formal receipt remain separate. Missing
  records are inconclusive, never complete.
- `wait_owner`: `user`, `saikuru`, `maintainer`, `authorized_person`, `dependency`,
  or `none`; `uncertain_operation`; fixed `stop_codes`; and pending decision
  IDs/kinds/roles with server-computed `actionable`. The top-level approval rows
  also receive `actionable`. No UI summary-text inference or role inference.
- `context`: approved business goal fields, plan/requirement revisions, approved
  input-version references, pending decision IDs and an `approved_records` or
  `unverified` basis. This is a business summary, not a native prompt/transcript,
  full Handoff export, reusable session binding or permission grant.
- `available_actions`: hints including `decide_feature_review`, `rework_job`,
  `new_request_from_job`, and (only in an authenticated maintenance view) a
  conservative `retry_task`. Per-WP `request_feature_review` prepares a target
  after technical checks. Preparing that target does not record consent.

`new_request_from_job` opens a draft only. It does not send a request or carry old
AI-start/plan approval into a new job. Unknown contracts/states must remain stopped.
Stopped/cancelled jobs may expose current WPs for reading; installation and dispatch
retain their stricter state fences.

New POST bodies have **exact** fields; extra actor, role or authority fields are
rejected. Every POST requires the existing session cookie, same-origin/Host/fetch
checks and `X-Agent-Team-UI: 1`. Worker bearer credentials are insufficient. The
server supplies the actor `authenticated_local_app_session` itself.

| Route | Exact body fields | Action |
|---|---|---|
| `/api/workflow/feature-review/prepare` | `job_id`, `work_package_id`, `work_package_revision`, `action` | `request_feature_review` |
| `/api/workflow/feature-review/decide` | `approval_id`, boolean `allow`, `confirmation`, `note`, `action` | `decide_feature_review` |
| `/api/workflow/feature-rework` | `job_id`, `work_package_id`, `work_package_revision`, `confirmation`, `note`, `action` | `rework_job` |

Preparation returns `approval_id`, `target_digest`, `permission_effect: "none"`.
Decision confirmation is exactly `{action: "feature_review_confirmation",
target_digest, decision: "approved" | "rejected"}` and must match `allow`.
Rework confirmation is exactly `{action: "request_feature_rework", target_digest}`.
Revision fields are current WP state revisions, not plan revisions. Expired targets
require a fresh preparation and explicit new confirmation; consent never carries.
Foreign approval pointers, stale versions/files, changed roles/categories, active
writers, cancelled/received/unknown jobs and duplicate decisions are refused.
A feature confirmation returns `formal_receipt: false`; it cannot receive the job.
Targeted rework preserves unaffected confirmation evidence, issues bounded repair
and rechecks, and may stop for explicit plan revision if actual changes affect
upstream evidence. The legacy whole-job `/api/jobs/rework` is refused for new
workflow jobs, which must identify the current feature target.

New-route validation errors return HTTP 400 with a fixed `code` and safe message;
unknown diagnostics are not echoed. Input protection refusals use
`workflow_sensitive_input_blocked`; other unclassified failures use
`workflow_input_or_maintenance_check_required`. DLP remains unchanged. Use section
or case IDs in examples; transcript-shaped `User:`/`System:` labels can be refused.
Technical-review text rejected before repair issuance remains a stopped diagnostic
case, not an automatic retry or permission to expose that text.

`GET /api/maintenance/workflow?id=<job_id>` additionally requires the existing
`X-Saikuru-Maintenance` capability and returns only validated fixed maintenance
guidance. Normal state views omit this guidance and redact maintenance diagnostics.
The UI must never infer, store or auto-grant maintenance authority. If no capability
is configured, this route refuses access; setting credentials/restarting the App
requires its separate authorization. Standard users can cancel or draft a new
request without impersonating a maintainer.

The existing maintenance-confirm route still requires exactly `id`, `task_revision`,
`job_revision`, `attempt_id`, `writer_stopped`, `checked_changes` and the existing
capability/browser guards. Confirmation does not queue execution. A separate
`/api/retry` operation uses `action: "retry_task"`, `checked_changes: true`,
`id`, and optionally `note`/`handoff_allowed`. Existing clients may omit the action
marker; an incorrect marker or extra authority fields are refused. Recovery cases require the capability
again and retain all Engine retry checks. No model/Effort/permission changes follow
from either operation. Checkpoint reservation/ready/binding/started are distinct;
the internal checkpoint-session helper is not a public resume shortcut. A newly
answered question changes bound authority and requires explicit re-preparation;
the UI must not silently refresh a session binding or repeat unknown operations.

Formal acceptance continues through the existing review acknowledgement, current
UAT evidence and explicit receipt endpoints. No feature decision, test result,
checkpoint or maintenance action can act for the actual recipient.
