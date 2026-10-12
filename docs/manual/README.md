# Agent Team HTML manual

- Open `agent-team-manual.html` in a browser: all 15 screenshots are embedded.
- `index.html` is the smaller version; keep `assets/` alongside it.
- Built from the local implementation on 2026-10-01. 19 Japanese chapters.
- Screenshots use a real, isolated Agent Team instance with synthetic demo data.
- No production job execution, folder move, deletion, GitHub visibility change,
  CLI update, Computer Use, or paid model inference was performed for this manual.
- The production port 8787 served another application at capture time. It was
  left untouched. Capture used loopback port 8798, without workers or monitors.
- Preview-only HTTP server uses loopback 8830 and does not start Agent Team.
- `screenshots.json` records screenshot identities and SHA-256 hashes.
- `build_manual.py` regenerates both HTML variants and the screenshot manifest.
- `capture_fixture.py` is a reproducible demo server; its state is separated into
  `capture-data/`. Run only when port 8798 is unused; Ctrl+C stops it.

## Artifact checks

Browser display, chapter navigation, keyword filtering, architecture layout, and
local image references were inspected. No application regression suite, live
development task, destructive workflow, phone device, or audible voice validation
was run. The manual states current limitations separately from implemented UI.

## Recovery

These are additive files under `docs/manual/`; application code/configuration and
production databases were not modified. To revise the manual, edit the builder
and regenerate. Retain screenshots and the manifest as documentation evidence.


## Project history documentation (2026-10-03)

The ledger chapter now includes project grouping, timeline timestamps, request origins, approval/event browsing, and unimplemented person identity features. After regenerating and applying existing manual revisions, run `python docs/manual/history_update.py` to reapply this section. This documentation update does not publish or push the manual.

## UI redesign documentation (2026-10-03)

The manual now describes the shared header (tabs, usage chips, "⋯" menu), the decision banner, repair-limit decisions, the ledger side panel and the two-column history. All 19 screenshots were retaken with the new UI using copies of `capture_fixture.py` and `capture_history_fixture.py` (synthetic data; the copy of the history fixture also served `/theme.css`). Apply after the existing revisions:

`build_manual.py` → `manual_revision.py` → `measurement_update.py` → `limited_routing_update.py` → `history_update.py` → `ui_redesign_update.py`

`ui_redesign_update.py` calls `history_update.apply`, rewrites the affected phrases, inserts the board-chapter UI section between HTML comment markers, swaps already-embedded old screenshots (originals in `data/backups/manual-ui-redesign-20261003-214203/`), and refreshes `screenshots.json`. Re-running it produces the same HTML. It does not publish or push. Record: `docs/UI_REDESIGN_20261003.md`.
