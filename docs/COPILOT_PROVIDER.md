> 添付パッチの提供元資料です。実施済み・導入先の設定に関する記載は、このPCでの完了を意味しません。現在の統合内容と確認範囲は [2026-10-09統合記録](CHANGE_REQUEST_MERGE_20261009.md) を参照してください。資料レビュー緩和は既定で無効です。

# GitHub Copilot provider (2026-10-08)

## Scope
- Selectable in チーム設定 under 「使用するプロバイダ」 (Codex / Claude Code / GitHub Copilot). Unchecked providers are removed from role choices, usage chips/panel and usage polling.
- Copilot may be assigned to **計画 (planner)**, **実装 (builder)** and **レビュー (reviewer)**, and used in 相談. The researcher is refused at save time (`team_config.COPILOT_ROLES`): its web search could not pass 采来's query check.
- Copilot prompt mode has no hook that asks 采来 before a tool runs, so Copilot's own edit/shell/URL tools are always denied and it works only through 采来's tools (MCP):
  - planner / reviewer / 相談: the read-only broker (`consultation_read_tools.py`);
  - builder of a document job: the document tools (`document_tools.py --write`; output folder only, no commands). Claude Artifact formats are refused (they need Claude's own tools);
  - builder of a development job (2026-10-09): `copilot_work_tools.py`. Reads use the read broker. `write_file` / `edit_file` / `run_command` first ask 采来's `/worker/tool` as **Write / Edit / Bash** — the same request a Claude builder's PreToolUse hook sends — so the same checks apply (project boundary, protected files, outbound guard, automatic or human approval, backups before an edit). Only when allowed does the tool server perform the operation; any failure denies. Commands run in Windows PowerShell (UTF-8 output) in the project folder. A request Copilot cancels while 采来 waits for the user is not performed afterwards. The run token is passed in a file of the temporary folder, never on a command line.

## Report format
Copilot CLI has no output-schema option (Claude: `--json-schema`, Codex: `outputSchema`). Every provider's report passes the same schema gate in the engine (`team_adapters.schema_errors`); only "no question / no update" fields may be absent. A Copilot report with a missing or malformed item is re-output once by the same model in the same session (`--resume`, no tools); 采来 never fills information fields itself.

## How it runs (`team_adapters.CopilotAdapter`)
- `copilot.exe` found on PATH or under `%LOCALAPPDATA%\Microsoft\WinGet\Packages\GitHub.Copilot_*`.
- Fail-closed flags: `--available-tools=<only project_read-* tools>` (or `__none__`), `--deny-tool=shell/write/url/read/memory`, `--disable-builtin-mcps`, `--no-ask-user`, `--no-custom-instructions`, `--no-auto-update`.
  - Note: an empty `--available-tools=` does **not** disable tools (verified: the built-in viewer read a file).
- Project access only through 采来's read broker (`consultation_read_tools.py` / `document_tools.py` read-only tools) via `--additional-mcp-config`. Any other tool call stops the run.
- Runs in an empty temporary folder; prompt (instructions + task + JSON schema) is sent on stdin (no command-line length limit).
- Attachments are not supported (refused before start).
- Agent definitions and policy files: the shared Claude ones (`~/.claude/agents`, `CLAUDE.md`).

## Models
`team_config.COPILOT_MODELS` (all 29 catalog entries probed 2026-10-08): 22 usable — gpt-5-mini, gpt-5.4-mini, gpt-5.4, gpt-5.5, gpt-5.3-codex, gpt-5.6-luna/terra/sol, gpt-6-luna, gpt-6-sol, claude-haiku-4.5 (no reasoning-effort flag), claude-sonnet-5, claude-sonnet-5.5, claude-opus-4.8, claude-opus-5, claude-opus-5.5, gemini-3.7-flash, gemini-3.8-flash, grok-4.5/4.6/4.7, mai-code-1.1-flash. Shown only if also listed by `copilot help config`.
Each label shows a credit guide `（約Nクレジット/回）`: the AI credits measured for one short prompt (uncached, effort low; third tuple value in `COPILOT_MODELS`). Real tasks cost several to tens of times more.
Refused by the plan: gpt-6.1-sol, claude-fable-5.1, claude-fable-5, claude-opus-4.8-fast, claude-sonnet-4.6, kimi-k3. Excluded by policy: gpt-6-astra.

## Monthly credits
- Each run's consumption (`session.usage_checkpoint.totalNanoAiu` / 1e9 = AI credits, premium requests) is added to `data/copilot-credits.json` (numbers only), reset on the 1st of the month (UTC).
- Remaining % = 100 − used / (月間の最大クレジット set in チーム設定) × 100. Without a limit the remaining value is unknown (never 100%).
- Only 采来's own runs are counted; use in VS Code etc. is not included. Account-wide figures would need GitHub's internal quota API with the CLI token, which was not adopted.

Backup of the previous files: `data/backups/copilot-provider-20261008/`. Tests: `test_copilot_provider.py`.
