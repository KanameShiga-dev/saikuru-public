"""Usage measurement (改修① Phase 1): raw provider values kept apart from normalized numbers.

Rules:
- A value the provider did not report is None (UNKNOWN), never 0.
- A provider's own total replaces earlier values; partial per-call values are only used when no total arrived
  (failed / cancelled runs), and are marked as partial.
- Quota (team_usage.py, % of plan windows) is a different measure and is not mixed in here.
- Only numbers and short labels are stored: no prompt, output or file content.
"""
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
_VERSION = None

EXTRA_NUMBERS = ('num_turns', 'duration_ms', 'duration_api_ms', 'total_cost_usd')


def code_version():
    """Short hash of 采来's Python sources, so measurements before and after a change can be told apart
    (采来 here is not a Git checkout, so there is no commit SHA)."""
    global _VERSION
    if _VERSION is None:
        digest = hashlib.sha256()
        for path in sorted(ROOT.glob('*.py')):
            if path.name.startswith('test_'):
                continue
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
        _VERSION = digest.hexdigest()[:12]
    return _VERSION


def allowlisted_extra(message):
    """Numbers from Claude Code's final result message (turns, durations, cost estimate, per-model tokens)."""
    out = {}
    for key in EXTRA_NUMBERS:
        value = message.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            out[key] = value
    models = message.get('modelUsage')
    if isinstance(models, dict):
        out['model_usage'] = {str(name)[:80]: {k: v for k, v in (row or {}).items()
                                               if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0}
                              for name, row in list(models.items())[:10] if isinstance(row, dict)}
    return out


def _claude(usage):
    return {'input_uncached': usage.get('input_tokens'), 'cache_read': usage.get('cache_read_input_tokens'),
            'cache_creation': usage.get('cache_creation_input_tokens'), 'output': usage.get('output_tokens')}


def _codex(usage):
    total_input, cached = usage.get('inputTokens'), usage.get('cachedInputTokens')
    uncached = total_input - cached if isinstance(total_input, int) and isinstance(cached, int) else None
    return {'input_uncached': uncached, 'cache_read': cached, 'cache_creation': None, 'output': usage.get('outputTokens')}


def normalize(provider, usage, source, partial):
    """Normalized tokens for one attempt: {'measurement': final|partial|unknown, input_uncached, cache_read,
    cache_creation, output, input_total}. Any part the provider did not report stays None."""
    if usage:
        row = _claude(usage) if provider == 'claude' else _codex(usage)
        measurement = 'final'
    elif partial:
        # Sum of per-call usage seen in the stream before the run ended without a total.
        keys = ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens', 'output_tokens')
        summed = {k: sum(p.get(k, 0) for p in partial.values()) if any(k in p for p in partial.values()) else None for k in keys}
        row = _claude(summed)
        measurement = 'partial'
    else:
        return {'measurement': 'unknown', 'input_uncached': None, 'cache_read': None, 'cache_creation': None,
                'output': None, 'input_total': None, 'source': None}
    parts = [row['input_uncached'], row['cache_read'], row['cache_creation']]
    known = [p for p in parts if isinstance(p, int)]
    row['input_total'] = sum(known) if known and (provider != 'claude' or len(known) == 3) else None
    if provider != 'claude' and isinstance(row['input_uncached'], int) and isinstance(row['cache_read'], int):
        row['input_total'] = row['input_uncached'] + row['cache_read']
    return dict(row, measurement=measurement, source=source if usage else 'stream_partial')


BUDGET_MODES = ('off', 'shadow', 'on')


def context_budget(config):
    """Context budget setting (改修③). Default SHADOW: the selection is computed and recorded, nothing is cut.
    OFF keeps the old behaviour without recording; ON applies the selection to the reuse context only."""
    value = (config or {}).get('context_budget') or {}
    mode = value.get('mode', 'shadow')
    chars = value.get('reuse_chars', 8000)
    if mode not in BUDGET_MODES or type(chars) is not int or not 1000 <= chars <= 100000:
        return {'mode': 'off', 'reuse_chars': 8000}
    return {'mode': mode, 'reuse_chars': chars}
