---
name: change-request
description: 采来（saikuru）の変更を、公開版へ取り込んでもらうための変更依頼ZIP（依頼書・差分パッチ・変更後のファイル一式）にまとめる。「変更依頼をまとめて」「今日の変更をZIPにして」「修正依頼を作って」といった依頼で使う。
---

# 変更依頼ZIPの作成

導入先PCで加えた変更を、公開版 `KanameShiga-dev/saikuru-public` へ取り込んでもらうための一式を作る。
これまでの成果物（`saikuru-change-request-20261005〜07.zip`、`docs/CHANGE_REQUEST_YYYYMMDD.md`、`patches/saikuru-YYYYMMDD.patch`）と同じ形にそろえる。

## 0. 先に利用者へ確認すること
- **基準**：公開版のどのコミットとの差分にするか（標準は公開版の最新コミット）。公開版にまだ入っていない前回までの依頼は、今回の差分にも含める（累積）。
- **第何弾か**と、対象の期間（標準は前回の変更依頼のあとから今日まで）。
- 迷う変更（社外に出してよいか分からないもの）は、入れる前に1件ずつ確認する。

## 1. 変更の洗い出し
- 采来のローカルgitで、前回の変更依頼のタグ `change-request/YYYYMMDD` から今までのコミットと差分を一覧にする（`git log`、`git diff --stat <タグ>..HEAD`）。タグが無い場合は、コミットの履歴と `docs/` の各機能の説明から洗い出し、利用者に範囲を確認する。
- 未コミットの変更が残っていれば、先にコミットするか利用者に確認する。

## 2. 入れないもの（このPC固有・社外に出さないもの）
- `data/` 以下（設定・DB・控え・添付）、`.env`、変更依頼ZIP自体、`__pycache__`。
- 台帳ルートの値：導入先は `C:\Projects`、公開版は `C:\AI_Work`。`team_ledger.py`・`team_elicitation.py`・`web/index.html`・`web/ledger.html`・`web/app.js` の台帳ルートは公開版の値のまま出す。
- 情報漏洩リスクの点検報告（PCの弱点を含む）、ライセンスの検討メモ、作業の引き継ぎメモ、下書き類（例：`docs/SECURITY_RISK_REVIEW_*.md`、`docs/TEAM_LICENSE_DISCUSSION.md`、`docs/SESSION_HANDOFF_*.md`）。
- 第三者のスキル（例：`skills/yomiyasu`）。取り込みは各環境で「スキルの取り込み」の手順で行う。
- 個人名・メールアドレス・社内のホスト名やパスが文書やコメントに入っていないかを確かめ、見つけたら利用者に確認する。

## 3. 差分パッチを作る
1. 作業用の一時フォルダ（スクラッチパッド）に公開版を読み取り専用で複製し、基準のコミットに合わせる（`git clone`、`git checkout <基準>`）。公開版への書き込み・push はしない。
2. 複製に、2の除外を守って変更後のファイルを写し、`git diff` で `patches/saikuru-YYYYMMDD.patch` を作る。
3. 別の新しい複製で `git apply --check` を行い、基準にそのまま当てられることを確かめる。

## 4. テスト
- 基準にパッチを当てた状態で単体テストを実行し、結果（件数・合否、失敗があれば内容）を依頼書に書く。
- 実機で確かめたこと（実際の依頼での確認、画面の確認）と、確かめていないことを分けて書く。

## 5. 依頼書 `CHANGE_REQUEST_YYYYMMDD.md`（日本語）
これまでの依頼書と同じ構成にする。
- 見出し：`# 修正依頼（第N弾）：GitHub 公開版との差分一式（YYYY-MM-DD）`
- 基準のコミット、差分・ファイル一式の場所、追加ライブラリ
- 「パッチに含めないもの」（2の内容）
- 「依頼する変更（N件）」：1件ごとに `#### 番号.【要望】／【不具合】／【その他】題名（主なファイル）` と、現象・変更・詳細の資料（`docs/…`）
- 「確認状況（導入先PCで実施）」：4の結果
- 「適用方法」：`git checkout <基準>`、`git apply --check`、`git apply`、必要なら `pip install`

同じ内容を `docs/CHANGE_REQUEST_YYYYMMDD.md` にも置く。

## 6. ZIPにまとめる
- ZIPは Python の `zipfile` で作る（PowerShell のスクリプトや実行ポリシーの回避は使わない）。
- 名前：采来のフォルダ直下の `saikuru-change-request-YYYYMMDD.zip`（`.gitignore` で除外済み）。
- 中身（フォルダ `saikuru-change-request-YYYYMMDD/` の下）：
  - `CHANGE_REQUEST_YYYYMMDD.md`
  - `patches/saikuru-YYYYMMDD.patch`
  - `docs/`：今回の変更の説明資料
  - `files/`：変更後のファイル一式（2の除外を守る）
- 作ったあとでZIPを開き直し、除外したものが入っていないことと、ファイル数を確かめる。

## 7. 仕上げと報告
- `docs/CHANGE_REQUEST_YYYYMMDD.md` と `patches/` をローカルgitにコミットし、タグ `change-request/YYYYMMDD` を付ける（次回の差分の起点）。
- 報告：ZIPの場所と大きさ、中身の一覧、依頼する変更の件数、テスト結果、入れなかったもの。
- GitHub の Issue・Pull Request への投稿や、メール等での送付はしない。送る場合は利用者が行うか、利用者の明示の許可を得てから行う。
