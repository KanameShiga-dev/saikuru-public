---
name: pdf-design-learning
description: Learn a unified design (colours, fonts, type sizes, line height, page size, margins) from a reference PDF and apply it to PowerPoint (PPTX), PDF, Word (DOCX) and HTML deliverables. Use when asked to match, copy, unify or follow the design/look/style of a PDF — 参考PDFのデザインを学習、デザインを合わせる、統一デザイン、体裁をそろえる、同じ見た目で作る.
---

# PDFのデザインを学習して統一デザインを反映する

参考PDFから「デザインプロファイル」（色・書体・文字サイズ・行間・用紙・余白）を取り出し、成果物（PPTX・PDF・Word・HTML）に同じデザインを当てる。

## 仕組み
- 本体：`design_profile.py（采来の設置フォルダ）`
  - `learn(pdf)`：PDFを読み取るだけ（pypdf で文字・書体・サイズ・文字色、pypdfium2 でページを小さな画像にして配色）。PDFの中身は実行しない。
  - 出力：`<名前>.design.json`（プロファイル）と `<名前>.design.md`（日本語の要約）
  - 反映用：`slide_palette()`（PPTX・PDF・動画）、`apply_docx()`・`style_docx_table()`（Word）、`css()`（HTML・印刷用PDF）
- 采来（サイクル）の資料作成では、制作担当の道具 `learn_design` と、`generate_media`・`generate_office`・`write_document` の `design_profile` 引数として組み込み済み。

## 采来で使う（資料作成の依頼）
1. 参考PDFを、読み取り元フォルダか保存先フォルダに置く。
2. 依頼文に「参考PDF（ファイル名）のデザインに合わせる」と書く。形式は PPTX・PDF・Word・HTML のどれか。
3. 制作担当の手順（計画担当はこの手順を builder の instruction に書く）：
   1. `learn_design {"pdf_path": "<参考PDF>", "name": "<名前>"}` → `design/<名前>.design.json` と `.design.md` ができる（同じ名前があれば作らない）
   2. PPTX・PDF・MP4：`generate_media` に `"design_profile": "design/<名前>.design.json"`
   3. Word：`generate_office` に `"design_profile": ...`（新規ファイルは用紙・余白も。既存ファイルの編集は文字のスタイルだけ）
   4. HTML：`write_document` に `"design_profile": ...`（`<head>` にCSSが入る。本文は `h1`/`h2`/`h3`/`p`/`table` と `var(--color-primary)` などを使う）
   5. 完了報告に `.design.md` の要点（色・書体・サイズ）と、注意（推定値など）を書く。
4. 確認・レビューの手順：`read_document` で PPTX を読むと、スライドごとの文字・表・図形と、デザインの集計（書体・文字色・塗り・文字サイズ）が返る。`.design.md` の値と照合する。見た目（配置・はみ出し）は画像での確認が別途必要。

## PPTX・PDF の仕上がり（自動）
- 参考PDFに2色目があれば、差し色として表紙・章扉・本文タイトル下の線に使う（メインの色から作った淡い色は使わない）。
- 本文だけで1〜4行の短いページは、1行なら中央の大きな一文、2〜4行なら番号付きカードで組む。行が多い・表や図のあるページは従来どおり。

## デスクトップ（Claude Code）で使う
```bash
python -X utf8 design_profile.py（采来の設置フォルダ） <参考.pdf> <出力>.design.json --md <出力>.design.md --name <名前>
```
作成する Python のコードからは、次のように反映する：
```python
# 采来の設置フォルダをPythonのモジュール検索先に設定してください
import design_profile as dp
profile = dp.load('<名前>.design.json')
# Word
from docx import Document
doc = Document(); dp.apply_docx(doc, profile)            # 見出し・本文のスタイル、用紙、余白
table = doc.add_table(rows=3, cols=2); dp.style_docx_table(table, profile)
# PowerPoint：色の役割と書体
palette = dp.slide_palette(profile)   # accent, dark, text, bg, light, muted, on_accent, heading_font, body_font
# HTML・印刷用PDF
css = dp.css(profile)                 # :root の変数、@page、body/h1/h2/h3/table
```
采来の PPTX 作成と同じ見た目で作るときは、`document_media_worker.build_pptx(spec, slides, path)` の `spec['design']` に `slide_palette()` の結果を渡す。

## 守ること
- プロファイルにある色・書体だけを使う。元PDFに無い色を足さない（足すときは理由を報告する）。
- 書体は、元PDFの書体に近い、このPCにある書体に置き換えている（例：Noto Sans JP → Yu Gothic）。元の書体名は `fonts.*.original` にある。
- 余白と行間は文字の位置からの推定値。表紙だけ・画像だけのPDFは精度が落ちる（`notes` を確認する）。
- 本文と背景のコントラストが 4.5:1 未満なら `notes` に出る。そのまま使うかは利用者に確認する。
- 参考PDFは50MBまで、パスワード付きは不可。文字は先頭30ページ、配色は最大8ページから学習する。
- Excel（XLSX）、Claude の Artifact（Design・Slides）への反映は対象外。
