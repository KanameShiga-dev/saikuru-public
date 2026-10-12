# 共通Agent・Skillの階層化 — 2026-09-30

## Completed

添付の組織階層ガイド・トークン最適化ガイドを基に、共通資産の配置、配布、必要時読込、管理・復元を実装した。実モデルによる比較評価はまだ完了していない。

## Files and environment changed

- 正本: `C:\AI_Work\Projects\Operations\ai-harness-common`。`POLICY.md`、5職種のCodex/Claude adapter、4共通Skill、2チームSkill、プロジェクトテンプレート、評価ケースと管理スクリプト。
- Company相当: `POLICY.md`, `agents/`, `skills/`。Team相当: `teams/default/`。Project相当: 各プロジェクトの文書と `templates/project/`。個人層: 既存の個人方針・Skillを保持。
- Skill正本は `~/.agents/skills/<name>` と `~/.claude/skills/<name>` にNTFS junctionで公開。Codexの現行user-scopeは[OpenAI Docs](https://learn.chatgpt.com/docs/build-skills)、Claude側は[公式Skill資料](https://code.claude.com/docs/en/skills)に基づく。旧ガイドの `~/.codex/skills` へ重複登録していない。
- Agent adapterは `~/.codex/agents/` と `~/.claude/agents/` へ同期し、SHA256で差分検出。Skill本文をadapterへ埋め込まない。
- Agent Team: `team_shared_harness.py` に役割別のmetadata-only案内と限定したMarkdown読取を追加。ClaudeのRead経路でSkill/reference読取要求を記録。Codex/ClaudeのCLIトークン報告と入力文字数を記録し、詳細画面へ表示。
- `team_engine.py` の共通指示を整理し、質問形式、引き継ぎ・再試行・秘密情報保護の契約を保持。明示的な `harness-audit` / `ハーネス監査` の調査工程は専用auditorへ割り当てる。
- コンバートの新しいプレビューでは短いAGENTS/CLAUDE routerとPROJECT_CONTEXT入口を生成する。既存プロジェクトを一括上書きしていない。既存の文書・フォルダ移動機能はそのまま。
- 共通Gitにローカルコミット済み。作者はローカル処理用 `Agent Team Automation`。ユーザーのGit identityを変更せず、remote/pushは行っていない。

## Delegation

今回の移行で子Agentは起動していない。Agent Teamの既存実装→独立レビューの完了条件は保持。単純な作業への追加Skill・追加Agent起動を要求しない。

## Validation performed

- 管理スクリプトの `verify`: 6Skillのjunctionと10adapterの整合を確認。
- ネイティブCodex `skills/list`: 6Skillが認識された。本文をモデルへ投入した動作検証ではない。
- Python AST・JavaScript構文確認: 成功。
- Git差分の空白確認: 成功。本番の実行・待機タスクがない状態で再起動、`/health` 成功。
- `rollback` のプレビューが復元対象を返すことを確認。実際の復元は実施していない。
- 既存8adapter合計は7,752→6,890バイト。新しいauditorを除いた比較であり、全起動contextの比較ではない。
- 共通指示のソース部分は2,171→1,322文字。生成された全prompt・provider tokensの比較ではない。
- 証拠: `data/backups/shared-harness-20260930-220204/` 内の `baseline.json`, `after-audit.json`, `instruction-size.json`, `codex-discovery.json`。

## Human-centered considerations

Skill案内は役割に関係する候補のみ。詳細画面では文字数とCLIトークンを区別し、アカウント残量と混同しない。取得できない指標をゼロと表示しない。

## Security and privacy

個人ポリシー本文と安全・承認条件は保持し、共通routerの案内のみ追記。モデル、fast mode、Windows sandbox、認証、グローバルCLI設定は変更しない。Skill正本内のMarkdown文書だけを読み取り専用で許可し、プロジェクト外への編集は許可しない。共通Gitには認証情報、DB、バックアップ、会話履歴を格納していない。

この階層は管理上の責任区分であり、Markdownによる会社規則がadministrator enforcementになったわけではない。管理ファイルを書き換えられるユーザーには迂回余地がある。既存の明示的利用者権限より資料の階層説明を優先していない。

## Residual risks and unverified items

- 今回の変更後のユニットテスト、実モデルによるSkill本文・referenceの選択読込、取消・再試行の回帰確認は未実行。
- UIの実画面確認は未実行。構文・HTTP起動確認とは区別する。
- 同一タスク3ケース以上での成功率、実入力/output tokens、cache、時間、compactionのBefore/After比較は未実施。`evals/routing.md` は評価計画であり合格証拠ではない。
- CodexのSkill/reference読込数はまだ確実に集計していない。Claudeの値も読取許可要求の記録であり本文消費の証明ではない。
- 既存の全プロジェクト・全個人Skillを再設計したわけではない。新しいテンプレートの適用は通常のコンバートプレビューで個別に判断する。
- 週次利用枠の削減率は保証しない。新しいSkillのmetadataやrouter追加分もあるため、総消費は実測が必要。

## Recovery or rollback

`ai-harness-common/scripts/manage.py rollback` はプレビューのみ。実行する場合はAgent Teamを停止後に `rollback --apply`。ローカル変更があるadapter/policyや、対象が違うjunctionは拒否する。junction自身だけを外し、正本フォルダを再帰削除しない。元の方針とAgent Teamソースはバックアップから復元し、共通リポジトリ・バックアップは保持する。

更新は `update-agents`。配置済みadapterが変更されていたら停止し、上書きしない。Skill本体は同じ正本を参照するため編集が双方へ即時反映する。正本の変更はGit差分で確認してから行う。
