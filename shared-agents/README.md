# 共通Agent定義

Codex用TOML・Claude Code用Markdownの5役割を収録しています。

- common-explorer：読み取り専用の調査
- common-implementer：許可された範囲の実装
- common-reviewer：独立した変更レビュー
- common-security-reviewer：セキュリティレビュー
- common-harness-auditor：ハーネスの権限・完了条件の点検

個人名・端末固有パス・接続先・認証情報・導入記録・ログは収録していません。役割のツール権限など、定義として必要な共通設定を含みます。利用者とプロジェクトの指示・承認境界が優先されます。

導入先や有効化の方法は利用するCLIの設定に合わせて指定してください。既存の同名定義を無確認で上書きしないでください。定義が参照するSkill本体は、このフォルダーには収録していません。この追加でローカルのAgent定義や権限を変更していません。
