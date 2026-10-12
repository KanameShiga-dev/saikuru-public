"""Reference-context image optimisation (改修指示書 ECR-2026-10-10-01).

Modes: OFF (default, nothing recorded), SHADOW (the choice is computed and recorded; the input is unchanged) and
AUTO (the reference is sent as images only when every check passes; otherwise text, as before).

Only explicitly reference-only fields are candidates. The request text, task instructions, user decisions,
approvals, command evidence and policies are never passed here (they carry user-written commands and contracts
that must reach the model as exact text — REQUEST_COMMANDS_RULE).

Cost: a subscription CLI does not publish how it counts image tokens, so nothing is assumed. The estimate comes only
from a measurement through the same CLI and model (team_context_calibrate.py → data/context-estimators.json) that
also confirmed the model read the image correctly. Without such a recent measurement the reference stays text.
"""
import base64
import hashlib
import json
import re
import time
from pathlib import Path

from team_context_renderer import RENDERER_VERSION, RenderError, page_count, page_sizes, render

MODES = ('off', 'shadow', 'auto')
DEFAULTS = {'mode': 'off', 'codex_min_chars': 1200, 'claude_min_chars': 1400, 'large_reference_chars': 4000,
            'minimum_estimated_saving_ratio': 0.30, 'max_generated_pages': 2, 'render_width_px': 1024,
            'render_height_px': 1024, 'min_font_px': 20, 'allow_reference_only': True, 'store_generated_images': False}
LIMITS = {'codex_min_chars': (200, 20000), 'claude_min_chars': (200, 20000), 'large_reference_chars': (1000, 20000),
          'max_generated_pages': (1, 2), 'render_width_px': (512, 1568), 'render_height_px': (512, 1568),
          'min_font_px': (16, 32)}
# Minimum estimated saving to send images, chosen on the settings page (2026-10-10 user decision: at most 30%).
RATIO_RANGE = (0.10, 0.30)
ESTIMATOR_FILE = 'context-estimators.json'
ESTIMATOR_MAX_AGE = 30 * 24 * 3600   # re-measure monthly: CLI and model behaviour change

# Image input per provider as 采来 sends it. Copilot's adapter refuses images; unknown providers are text.
CAPABILITIES = {'claude': {'image_input': True}, 'codex': {'image_input': True}, 'copilot': {'image_input': False}}

# Execution details (exact text needed): code, commands, paths. A reference containing them stays text.
EXECUTION_DETAIL = re.compile(r'```|`[^`\n]+`|^\s*(?:\$|>|PS [A-Z]:\\)\s|^\s*(?:python|py|git|pip|npm|node|powershell|pwsh|cmd)(?:\.exe)?\s'
                              r'|[A-Za-z]:\\|\\\\[^\\\s]+\\|(?:^|\s)/(?:[\w.-]+/)+[\w.-]*', re.M | re.I)
TABLE_DETAIL = re.compile(r'\t|^[^\n]*\|[^\n]*\||<\s*/?\s*(?:table|tr|td|th)\b', re.M | re.I)


def settings(config):
    """The effective setting. A missing section is OFF; validate() rejects a malformed one when saving/loading."""
    value = (config or {}).get('context_optimization')
    if value is None:
        return dict(DEFAULTS)
    validate(value)
    return dict(DEFAULTS, **value)


def validate(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError('参考情報の画像化の設定に未知の項目があります。')
    if value.get('mode', 'off') not in MODES:
        raise ValueError('参考情報の画像化の mode は off・shadow・auto のいずれかを指定してください。')
    for key, (low, high) in LIMITS.items():
        if key in value and (type(value[key]) is not int or not low <= value[key] <= high):
            raise ValueError(f'参考情報の画像化の {key} は {low}〜{high} の整数で指定してください。')
    ratio = value.get('minimum_estimated_saving_ratio', 0.30)
    if type(ratio) not in (int, float) or not RATIO_RANGE[0] <= ratio <= RATIO_RANGE[1]:
        raise ValueError('画像にする基準（推定の削減率）は 10〜30% で指定してください。')
    if value.get('allow_reference_only', True) is not True:
        raise ValueError('参考情報の画像化は参考情報だけが対象です（allow_reference_only は true のみ）。')
    if value.get('store_generated_images', False) is not False:
        raise ValueError('生成画像は保存しません（store_generated_images は false のみ）。')


def for_job(config, job):
    """The config for one request: a request with its own token reduction (ON/OFF, set when it was created) changes
    only the mode — ON is AUTO, OFF is SHADOW so the run is still recorded for the comparison. Threshold, font size and
    the other values stay those of the settings page / config.json."""
    side = ((job or {}).get('token_reduction') or {}).get('side')
    if side not in ('on', 'off'):
        return config
    section = dict((config or {}).get('context_optimization') or {}, mode='auto' if side == 'on' else 'shadow')
    return dict(config or {}, context_optimization=section)


def load_estimators(data_dir):
    try:
        value = json.loads((Path(data_dir) / ESTIMATOR_FILE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def usable_estimator(estimators, adapter, model, opts=None, now=None):
    """The measured estimator for this adapter/model, or None (unmeasured, old, other renderer or page layout,
    not readable). Readability was confirmed at one font size and width only, so those must match the setting."""
    opts = opts or DEFAULTS
    entry = (estimators or {}).get(f'{adapter}:{model}')
    if not isinstance(entry, dict):
        return None
    per_char, per_mp = entry.get('text_tokens_per_char'), entry.get('image_tokens_per_megapixel')
    age = (time.time() if now is None else now) - entry.get('measured_at', 0) if type(entry.get('measured_at')) in (int, float) else None
    fresh = age is not None and 0 <= age <= ESTIMATOR_MAX_AGE
    if (fresh and entry.get('renderer_version') == RENDERER_VERSION and entry.get('text_answer_ok') is True
            and entry.get('font_px') == opts['min_font_px'] and entry.get('render_width_px') == opts['render_width_px']
            and entry.get('image_answer_ok') is True and isinstance(per_char, (int, float)) and per_char > 0
            and isinstance(per_mp, (int, float)) and per_mp > 0):
        return entry
    return None


def marker(text, pages, partial=False):
    digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
    if partial:
        return (f'[参考情報の一部（この位置にあった{len(text)}文字、SHA-256 {digest[:16]}）は、'
                f'この依頼文の末尾にある参考画像{pages}枚に、省略せずそのまま描いてあります。そちらを読んでください。'
                f'この目印の前後の文は、画像の部分の前後にそのまま続きます]')
    return (f'[参考情報（HANDOFF.md の控え、{len(text)}文字、SHA-256 {digest[:16]}）は、'
            f'この依頼文の末尾にある参考画像{pages}枚に、省略せずそのまま描いてあります。そちらを読んでください]')


INDENTED_CODE = re.compile(r'^(?: {4}|\t)\S', re.M)
FENCE = re.compile(r'^\s*(?:```|~~~)')
MAX_EXTRACTION_CHARS = 60000   # HANDOFF.md is at most 100 KB; beyond this the reference stays text


def blocks(text):
    """Paragraphs of the text as [(start, end, plain)], offsets into the text without the trailing line break.
    A fenced code block is one block even with blank lines inside, and is never plain."""
    found, start, end, fenced, in_fence, pos = [], None, None, False, False, 0
    for line in text.splitlines(keepends=True):
        content_end = pos + len(line.rstrip('\r\n'))
        if FENCE.match(line):
            start = pos if start is None else start
            in_fence, fenced, end = not in_fence, True, content_end
        elif in_fence or line.strip():
            start = pos if start is None else start
            end = content_end
        elif start is not None:
            found.append((start, end, fenced))
            start, fenced = None, False
        pos += len(line)
    if start is not None:
        found.append((start, end, fenced or in_fence))
    from team_context_renderer import unsupported_characters
    return [(s, e, not code and not EXECUTION_DETAIL.search(text[s:e]) and not TABLE_DETAIL.search(text[s:e]) and not INDENTED_CODE.search(text[s:e])
             and not unsupported_characters(text[s:e])) for s, e, code in found]


def extract(text, opts):
    """The longest run of consecutive plain paragraphs that fits in the page limit, as (start, end, count), or None.
    Only the choice of WHICH part goes into images is made here: nothing is removed from what the model receives."""
    from team_context_renderer import layout
    width, height, font_px, limit = (opts['render_width_px'], opts['render_height_px'], opts['min_font_px'],
                                     opts['max_generated_pages'])
    parts, lines, per_page = [], [], None
    for s, e, plain in blocks(text):
        count = 0
        if plain:
            try:
                _, wrapped, per_page = layout(text[s:e], width, height, font_px)   # also rejects missing glyphs
                count = len(wrapped)
                plain = count <= limit * per_page
            except RenderError:
                plain = False
        parts.append((s, e, plain))
        lines.append(count)
    # Each line break starts a new wrapped line, so a window's line count is the sum of its paragraphs' lines plus
    # the blank lines between them (the same count as laying out the window text itself).
    gaps = [text.count('\n', parts[k][1], parts[k + 1][0]) - 1 for k in range(len(parts) - 1)]
    fits = lambda i, j: sum(lines[i:j + 1]) + sum(gaps[i:j]) <= limit * per_page
    best, index = None, 0
    while index < len(parts):
        if not parts[index][2]:
            index += 1
            continue
        run = index
        while run < len(parts) and parts[run][2]:
            run += 1
        # Two pointers over the plain run [index, run): the line count only grows as the window grows.
        i, j = index, index
        while i < run:
            j = max(j, i)
            while j + 1 < run and fits(i, j + 1):
                j += 1
            chars = parts[j][1] - parts[i][0]
            if best is None or chars > best[1] - best[0]:
                best = (parts[i][0], parts[j][1], j - i + 1)
            i += 1
        index = run
    return best


def _assess_one(field, text, adapter, model, opts, estimators):
    from team_attachments import REFERENCE_IMAGE_RULE
    record = {'field': field, 'reference_chars': len(text),
              'reference_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
              'threshold_chars': opts['codex_min_chars'] if adapter == 'codex' else opts['claude_min_chars'],
              'rendered_page_count': None, 'image_pixels': None, 'text_tokens_estimated': None,
              'image_tokens_estimated': None, 'estimated_saving_ratio': None, 'estimate_confidence': 'unknown',
              'estimator_version': None, 'renderer_version': RENDERER_VERSION, 'candidate_transport': 'text',
              'partial': False, 'extracted_range': None, 'extracted_chars': None}
    from team_dlp import scan_input
    if scan_input(text)['block']:
        # Turning text into an image never hides it from the model; the existing guards decide on sending.
        return dict(record, decision_reason='policy_blocked')
    capability = CAPABILITIES.get(adapter)
    if len(text) >= opts['large_reference_chars']:
        # Large reference: only its longest plain-prose part goes into images; the rest stays text in place.
        if len(text) > MAX_EXTRACTION_CHARS:
            return dict(record, decision_reason='reference_too_large')
        if not capability or not capability['image_input']:
            return dict(record, decision_reason='unsupported_or_unknown')
        window = extract(text, opts)
        if not window or window[1] - window[0] < record['threshold_chars']:
            return dict(record, decision_reason='no_extractable_part')
        part = text[window[0]:window[1]]
        record.update(partial=True, extracted_range=[window[0], window[1]], extracted_chars=len(part),
                      extracted_blocks=window[2], total_blocks=len(blocks(text)))
    else:
        if EXECUTION_DETAIL.search(text) or TABLE_DETAIL.search(text):
            return dict(record, decision_reason='contains_execution_details')
        if len(text) < record['threshold_chars']:
            return dict(record, decision_reason='below_threshold')
        if not capability or not capability['image_input']:
            return dict(record, decision_reason='unsupported_or_unknown')
        part = text
    try:
        sizes = page_sizes(part, opts['render_width_px'], opts['render_height_px'], opts['min_font_px'])
    except RenderError as exc:
        return dict(record, decision_reason='render_fail:' + str(exc))
    record.update(rendered_page_count=len(sizes), image_pixels=sum(w * h for w, h in sizes))
    if len(sizes) > opts['max_generated_pages']:
        return dict(record, decision_reason='render_or_readability_fail')
    estimator = usable_estimator(estimators, adapter, model, opts)
    if not estimator:
        return dict(record, decision_reason='uncertain_cost')
    per_char = estimator['text_tokens_per_char']
    text_cost = len(part) * per_char
    image_cost = (record['image_pixels'] / 1_000_000 * estimator['image_tokens_per_megapixel']
                  + (len(REFERENCE_IMAGE_RULE) + len(marker(part, len(sizes), record['partial']))) * per_char)
    saving = 1 - image_cost / text_cost
    record.update(text_tokens_estimated=round(text_cost), image_tokens_estimated=round(image_cost),
                  estimated_saving_ratio=round(saving, 3), estimate_confidence='measured_cli',
                  estimator_version=estimator.get('method'), estimator_measured_at=estimator.get('measured_at'))
    if saving < opts['minimum_estimated_saving_ratio']:
        return dict(record, decision_reason='insufficient_savings')
    return dict(record, decision_reason='image_candidate', candidate_transport='image')


def decide(references, adapter, model, config, estimators=None, attempt=1):
    """Return (record, selection). record is None when OFF. selection is None unless AUTO chose images for one
    reference: {'field', 'text' (original), 'replacement' (the text with the imaged part replaced by its marker),
    'images' (base64 PNG)}. Never raises: any failure keeps text."""
    try:
        opts = settings(config)
    except ValueError:
        return None, None
    if opts['mode'] == 'off':
        return None, None
    started = time.perf_counter()
    selection = None
    try:
        items = [_assess_one(field, text, adapter, model, opts, estimators) for field, text in references
                 if isinstance(text, str) and text]
        chosen = next((item for item in items if item['candidate_transport'] == 'image'), None)
        if chosen and opts['mode'] == 'auto':
            if attempt > 1:
                # A retried run goes back to the plain text input, so a failure with images cannot repeat.
                chosen['decision_reason'] = 'retry_uses_text'
            else:
                text = next(t for f, t in references if f == chosen['field'])
                start, end = chosen['extracted_range'] or (0, len(text))
                part = text[start:end]
                try:
                    pages = render(part, opts['render_width_px'], opts['render_height_px'], opts['min_font_px'],
                                   opts['max_generated_pages'])
                    selection = {'field': chosen['field'], 'text': text,
                                 'replacement': text[:start] + marker(part, len(pages), chosen['partial']) + text[end:],
                                 'images': [base64.b64encode(page).decode('ascii') for page in pages]}
                except RenderError as exc:
                    chosen['decision_reason'] = 'render_fail:' + str(exc)
    except Exception as exc:   # the optimisation must never stop a task
        items, selection = [{'decision_reason': 'optimizer_error', 'error': type(exc).__name__}], None
    record = {'optimization_mode': opts['mode'], 'adapter': adapter, 'model': model,
              'selected_transport': 'image' if selection else 'text', 'references': items,
              'elapsed_ms': round((time.perf_counter() - started) * 1000, 1)}
    return record, selection


def _saving_by_length(adapter, model, opts, estimators):
    """[(characters, estimated saving)] for plain prose of each length, by the same calculation as a real run."""
    from team_context_calibrate import SAMPLE
    prose = SAMPLE * (opts['large_reference_chars'] // len(SAMPLE) + 1)
    start = opts['codex_min_chars'] if adapter == 'codex' else opts['claude_min_chars']
    result = []
    for chars in list(range(start, opts['large_reference_chars'], 100)) + [opts['large_reference_chars'] - 1]:
        item = _assess_one('sample', prose[:chars], adapter, model, opts, estimators)
        if item['estimated_saving_ratio'] is not None:
            result.append([chars, item['estimated_saving_ratio']])
    return result


def overview(config, estimators, now=None):
    """Data for the settings page: the setting, each measurement and what it means for each role (no text)."""
    opts = settings(config)
    measured = []
    for key, entry in sorted((estimators or {}).items()):
        if not isinstance(entry, dict) or ':' not in key:
            continue
        adapter, model = key.split(':', 1)
        usable = usable_estimator(estimators, adapter, model, opts, now) is not None
        why = None
        if not usable:
            fresh = (now or time.time()) - (entry.get('measured_at') or 0) <= ESTIMATOR_MAX_AGE
            why = ('読み取りを確認できていない' if not (entry.get('text_answer_ok') and entry.get('image_answer_ok'))
                   else '30日を超えた（測り直しが必要）' if not fresh
                   else '文字サイズ・幅が設定と違う' if (entry.get('font_px'), entry.get('render_width_px')) != (opts['min_font_px'], opts['render_width_px'])
                   else '描画の版が違う（測り直しが必要）')
        measured.append({'adapter': adapter, 'model': model, 'font_px': entry.get('font_px'), 'measured_at': entry.get('measured_at'),
                         'reference_chars': entry.get('reference_chars'), 'measured_saving_ratio': entry.get('measured_saving_ratio'),
                         'readable': bool(entry.get('text_answer_ok') and entry.get('image_answer_ok')), 'usable': usable, 'unusable_reason': why,
                         'saving_by_length': _saving_by_length(adapter, model, opts, estimators) if usable else []})
    roles = []
    for role in ('planner', 'builder', 'researcher', 'reviewer'):
        profile = (config.get('profiles') or {}).get((config.get('roles') or {}).get(role)) or {}
        adapter, model = profile.get('adapter'), profile.get('model')
        entry = next((m for m in measured if (m['adapter'], m['model']) == (adapter, model)), None)
        roles.append({'role': role, 'adapter': adapter, 'model': model,
                      'image_input': bool((CAPABILITIES.get(adapter) or {}).get('image_input')),
                      'measured': entry})
    return {'settings': opts, 'ratio_range': list(RATIO_RANGE), 'modes': list(MODES), 'measurements': measured, 'roles': roles}


def update_settings(config, body):
    """New config with the page's choices (mode and saving threshold only); the other items are kept."""
    if not isinstance(body, dict) or set(body) != {'mode', 'minimum_estimated_saving_ratio'}:
        raise ValueError('動作と基準の2項目を指定してください。')
    section = dict(config.get('context_optimization') or {}, mode=body['mode'],
                   minimum_estimated_saving_ratio=body['minimum_estimated_saving_ratio'])
    validate(section)
    return dict(config, context_optimization=section)


def apply(prompt, selection):
    """Replace the one JSON-escaped occurrence of the reference in the prompt by its replacement (the marker in
    place of the imaged part). Returns the new prompt, or None when the reference is not found exactly once."""
    needle = json.dumps(selection['text'], ensure_ascii=False)[1:-1]
    if prompt.count(needle) != 1:
        return None
    return prompt.replace(needle, json.dumps(selection['replacement'], ensure_ascii=False)[1:-1])
