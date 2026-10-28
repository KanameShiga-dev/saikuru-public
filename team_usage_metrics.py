"""Usage measurement (改修① Phase 1): raw provider values kept apart from normalized numbers.

Rules:
- A value the provider did not report is None (UNKNOWN), never 0.
- A provider's own total replaces earlier values; partial per-call values are only used when no total arrived
  (failed / cancelled runs), and are marked as partial.
- Quota (team_usage.py, % of plan windows) is a different measure and is not mixed in here.
- Only numbers and short labels are stored: no prompt, output or file content.
"""
import hashlib
import math
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
_VERSION = None

EXTRA_NUMBERS = ('num_turns', 'duration_ms', 'duration_api_ms', 'total_cost_usd')


def known_number(value):
    try: return type(value) in (int,float) and math.isfinite(value) and value >= 0
    except OverflowError: return False


def token_count(value):
    return value if type(value) is int and 0 <= value <= 2**63-1 else None


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
        if known_number(value):
            out[key] = value
    models = message.get('modelUsage')
    if isinstance(models, dict):
        out['model_usage'] = {str(name)[:80]: {k: v for k, v in (row or {}).items()
                                               if known_number(v)}
                              for name, row in list(models.items())[:10] if isinstance(row, dict)}
    return out


def _claude(usage):
    return {'input_uncached': token_count(usage.get('input_tokens')), 'cache_read': token_count(usage.get('cache_read_input_tokens')),
            'cache_creation': token_count(usage.get('cache_creation_input_tokens')), 'output': token_count(usage.get('output_tokens'))}


def _codex(usage):
    total_input, cached, written = (token_count(usage.get(k)) for k in ('inputTokens','cachedInputTokens','cacheWriteInputTokens'))
    # Preserve reported cache writes, but do not guess how nonzero writes overlap
    # with inputTokens. The reported total is kept separately in normalize().
    uncached = total_input-cached if total_input is not None and cached is not None and total_input >= cached and written in (None,0) else None
    return {'input_uncached': uncached, 'cache_read': cached, 'cache_creation': written,
            'output': token_count(usage.get('outputTokens')),'reasoning':token_count(usage.get('reasoningOutputTokens'))}


def copilot_normalized(tokens):
    """Normalized tokens from the Copilot CLI usage file totals (agent_run.copilot_tokens, team_adapters.copilot_tokens).
    Copilot's inputTokens already include the cache reads and writes (checked on 445 recorded runs, 2026-10-10)."""
    if not isinstance(tokens, dict) or not any(token_count(v) is not None for v in tokens.values()):
        return {'measurement': 'unknown', 'input_uncached': None, 'cache_read': None, 'cache_creation': None,
                'output': None, 'input_total': None, 'source': None}
    total, read, write = (token_count(tokens.get(k)) for k in ('inputTokens', 'cacheReadTokens', 'cacheWriteTokens'))
    uncached = total-read-write if all(v is not None for v in (total,read,write)) and total >= read+write else None
    complete = uncached is not None and all(type(tokens.get(k)) is int and tokens[k] >= 0
        for k in ('inputTokens', 'cacheReadTokens', 'cacheWriteTokens', 'outputTokens'))
    return {'measurement': 'final' if complete else 'partial', 'input_uncached': uncached, 'cache_read': read, 'cache_creation': write,
            'output': token_count(tokens.get('outputTokens')), 'input_total': total, 'reasoning': token_count(tokens.get('reasoningTokens')),
            'source': 'copilot_usage_file'}


# Historical comparison coefficients, not current prices, money or plan-limit pressure.
# Do not derive USD costs from this score.
WEIGHTS = {'input_uncached': 1.0, 'cache_read': 0.1, 'cache_creation': 1.25, 'output': 5.0}


def weighted(row):
    """Relative weighted tokens, not money. Every component must be reported; unknown is never zero."""
    if any(token_count(row.get(k)) is None for k in WEIGHTS):
        return None
    return round(sum(WEIGHTS[k] * row[k] for k in WEIGHTS))


def normalize(provider, usage, source, partial):
    """Normalized tokens for one attempt: {'measurement': final|partial|unknown, input_uncached, cache_read,
    cache_creation, output, input_total}. Any part the provider did not report stays None."""
    usage=usage if isinstance(usage,dict) and provider in ('claude','codex') else None
    partial=partial if isinstance(partial,dict) and provider=='claude' else {}
    if usage:
        row = _claude(usage) if provider == 'claude' else _codex(usage)
        measurement = 'final'
    elif partial:
        # Sum of per-call usage seen in the stream before the run ended without a total.
        keys = ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens', 'output_tokens')
        calls = [p for p in partial.values() if isinstance(p,dict)]
        summed = {k:sum(p[k] for p in calls) if calls and all(token_count(p.get(k)) is not None for p in calls) else None for k in keys}
        row = _claude(summed)
        measurement = 'partial'
    else:
        return {'measurement': 'unknown', 'input_uncached': None, 'cache_read': None, 'cache_creation': None,
                'output': None, 'input_total': None, 'source': None}
    parts = [row['input_uncached'], row['cache_read'], row['cache_creation']]
    known = [p for p in parts if isinstance(p, int)]
    row['input_total'] = sum(known) if known and (provider != 'claude' or len(known) == 3) else None
    if provider != 'claude' and usage:
        row['input_total'] = token_count(usage.get('inputTokens'))
    return dict(row, measurement=measurement, source=source if usage else 'stream_partial')


BUDGET_MODES = ('off', 'shadow', 'on')


def observe_prompt(ctx, text, *, includes_system=False, kind='initial', stage='adapter_payload'):
    """Optional adapter hook; only numeric preparation metadata, no prompt storage."""
    record = getattr(ctx,'record_prompt_measurement',None)
    if callable(record): record(text,stage=stage,includes_system=includes_system,kind=kind)


def _identifier(value):
    return value if isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}',value) else None


def _label(value):
    return value if isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_./:+ -]{1,80}',value) else None


def history_complete(task):
    """A retained history must cover every known attempt, including held/no-call attempts."""
    count=token_count(task.get('attempt'))
    if count is None or task.get('agent_run_history_truncated'): return False
    runs=[r for r in (task.get('agent_run_history') or []) if isinstance(r,dict)]
    if isinstance(task.get('agent_run'),dict): runs.append(task['agent_run'])
    ordinals=[token_count(r.get('attempt')) for r in runs]
    if any(v is None or not 0 < v <= count for v in ordinals): return False
    return len(set(ordinals))==count


def _coverage(values, *, final=None):
    known = [v for v in values if known_number(v)]
    complete = bool(values) and len(known) == len(values) and (final is None or all(final))
    return {'total':sum(known) if complete else None,'known_sum':sum(known) if known else None,
            'known':len(known),'unknown':len(values)-len(known),'lower_bound':bool(known) and not complete}


def _intervals(rows, *, start='started_at', end='finished_at', at=None):
    spans=[]; missing=0; pending=0
    for row in rows:
        left,right=row.get(start),row.get(end)
        if not known_number(left): missing+=1; continue
        if right is None and row.get('pending') is True and at is not None and known_number(at): right=at; pending+=1
        if not known_number(right) or right < left: missing+=1; continue
        spans.append((left,right))
    merged=[]
    for left,right in sorted(spans):
        if merged and left <= merged[-1][1]: merged[-1]=(merged[-1][0],max(right,merged[-1][1]))
        else: merged.append((left,right))
    duration=sum(right-left for left,right in merged)
    return {'seconds':duration if rows and not missing and not pending else None,
            'observed_seconds':duration if spans else None,'unknown_intervals':missing,
            'pending_intervals':pending,'overlap_removed':True,'actual_human_work_seconds':None}


def workflow_measurements(jobs, tasks, approvals=(), *, checkpoints=(), at=None):
    """Read-only metadata report. No Store, native probes, prices, prompts or quota access.

    Run IDs deduplicate snapshots globally; conflicting ownership is unknown rather
    than billed twice. Totals require complete coverage; known subtotals are labelled.
    Native session IDs are represented only by provider-scoped SHA-256 fingerprints.
    """
    tasks=list(tasks); approvals=list(approvals)
    jobs={j['id']:j for j in jobs if isinstance(j,dict) and _identifier(j.get('id'))}
    checkpoints={c['id']:c for c in checkpoints if isinstance(c,dict) and _identifier(c.get('id'))}
    candidates={}; anonymous=[]; duplicate=0; conflicts=set()
    for task in tasks:
        if not isinstance(task,dict) or task.get('job_id') not in jobs: continue
        history=[r for r in (task.get('agent_run_history') or []) if isinstance(r,dict)]
        if isinstance(task.get('agent_run'),dict): history.append(task['agent_run'])
        if not history and token_count(task.get('attempt')) not in (None,0):
            anonymous.append((task,{'attempt':task['attempt'],'attempt_id':(task.get('current_attempt') or {}).get('id'),'status':'unknown'}))
        for run in history:
            if run.get('status') == 'held': continue  # no invocation was made
            identity=_identifier(run.get('id'))
            if identity is None: anonymous.append((task,run)); continue
            if identity in candidates:
                duplicate+=1
                old_task,_=candidates[identity]
                if (old_task.get('id'),old_task['job_id']) != (task.get('id'),task['job_id']): conflicts.add(identity)
            candidates[identity]=(task,run)  # history first, current snapshot last; never sum cumulative usage
    rows=[]
    for identity,(task,run) in [(key,value) for key,value in candidates.items()]+[(None,v) for v in anonymous]:
        ids=run.get('measurement_ids') if isinstance(run.get('measurement_ids'),dict) else {}
        unknown=[]
        invalid = (identity is None or identity in conflicts or _identifier(task.get('id')) is None
                   or ids.get('job_id',task['job_id']) != task['job_id'] or ids.get('task_id',task.get('id')) != task.get('id'))
        if invalid: unknown.append('run_identity_missing_or_conflicting')
        if task.get('agent_run_history_truncated'): unknown.append('history_truncated')
        if not history_complete(task): unknown.append('attempt_history_incomplete')
        normalized=run.get('usage_normalized')
        if isinstance(run.get('usage'),dict) and run.get('provider') in ('claude','codex'):
            normalized=normalize(run.get('provider'),run['usage'],run.get('usage_source'),{})
        if not isinstance(normalized,dict):
            normalized=(copilot_normalized(run.get('copilot_tokens')) if run.get('provider')=='copilot'
                        else normalize(run.get('provider'),run.get('usage'),run.get('usage_source'),{}))
        valid_measurement=normalized.get('measurement') in ('final','partial')
        counts={k:token_count(normalized.get(k)) if not invalid and valid_measurement else None for k in
                ('input_uncached','cache_read','cache_creation','output','input_total','reasoning')}
        unknown.extend('not_reported:'+k for k,v in counts.items() if v is None)
        if run.get('provider')=='codex' and (counts.get('cache_creation') or 0)>0 and counts.get('input_uncached') is None:
            unknown.append('cache_write_overlap_unverified')
        cost=(run.get('usage_raw') or {}).get('total_cost_usd')
        if invalid or not known_number(cost): cost=None; unknown.append('price_unknown')
        session=run.get('session_id')
        fingerprint=(hashlib.sha256((str(run.get('provider'))+'\0'+session).encode()).hexdigest()
                     if isinstance(session,str) and session and len(session)<=160 else None)
        if fingerprint is None: unknown.append('session_not_recorded')
        attempt_id=_identifier(run.get('attempt_id') or ids.get('attempt_id'))
        if attempt_id is None: unknown.append('attempt_id_not_recorded')
        checkpoint_id=_identifier(ids.get('checkpoint_id'))
        cp=checkpoints.get(checkpoint_id)
        if checkpoint_id and (cp is None or cp.get('job_id') != task['job_id']): unknown.append('checkpoint_relation_unverified')
        prompts=run.get('prompt_measurements') or []
        final=[p for p in prompts if isinstance(p,dict) and p.get('stage')=='adapter_payload'
               and token_count(p.get('characters')) is not None]
        initial=next((p for p in final if p.get('kind')=='initial'),None)
        if initial is None: unknown.append('final_prompt_not_recorded')
        if run.get('prompt_measurements_truncated'): unknown.append('prompt_preparations_truncated')
        inherited=(run.get('inherited_context') if isinstance(run.get('inherited_context'),dict) else {}).get('characters')
        rows.append({'job_id':task['job_id'],'task_id':_identifier(task.get('id')),'work_package_id':_identifier(task.get('work_package_id')),
            'run_id':identity,'attempt_id':attempt_id,'attempt_ordinal':token_count(run.get('attempt')),
            'session_fingerprint':fingerprint,'checkpoint_id':checkpoint_id,'checkpoint_relation_verified':bool(cp and cp.get('job_id')==task['job_id']),
            'provider':run.get('provider') if run.get('provider') in ('codex','claude','copilot','gemini') else None,
            'model':_label(run.get('model')),'effort':_label(run.get('effort')),
            'status':run.get('status') if run.get('status') in ('starting','running','succeeded','failed','cancelled','interrupted','unknown') else 'unknown',
            'started_at':run.get('started_at') if known_number(run.get('started_at')) else None,
            'finished_at':run.get('finished_at') if known_number(run.get('finished_at')) else None,
            'measurement':normalized.get('measurement') if not invalid and normalized.get('measurement') in ('final','partial') else 'unknown',**counts,
            'history_truncated':bool(task.get('agent_run_history_truncated')),
            'history_incomplete':not history_complete(task),
            'weighted':weighted(counts),'cost_usd_reported':cost,'cost_source':'cli_reported_usd' if cost is not None else None,
            'initial_payload_characters':initial['characters'] if initial else None,
            'payload_includes_system':initial.get('includes_system') if initial else None,
            'prepared_payloads':len(final) if prompts else None,
            'prepared_payloads_lower_bound':bool(run.get('prompt_measurements_truncated')),
            'format_repair_preparations':sum(p.get('kind')=='format_repair' for p in final) if prompts else None,
            'inherited_context_characters':token_count(inherited),'unknown_reasons':sorted(set(unknown))})
    summaries={}
    for job_id,job in jobs.items():
        selected=[r for r in rows if r['job_id']==job_id]
        truncated=any(t.get('agent_run_history_truncated') for t in tasks if t.get('job_id')==job_id)
        incomplete=any(not history_complete(t) for t in tasks if t.get('job_id')==job_id)
        decisions=[a for a in approvals if a.get('job_id')==job_id and a.get('intended_role') in ('end_user','authorized_person')]
        held=[a for a in approvals if a.get('job_id')==job_id and a.get('intended_role')=='maintainer']
        # Pending intervals can be observed until the caller's explicit timestamp;
        # missing decision timestamps never become zero-length waits.
        def waits(records):
            spans=[{'created_at':a.get('created_at'),'decided_at':a.get('decided_at'),'pending':a.get('status')=='pending'}
                   for a in records]
            return _intervals(spans,start='created_at',end='decided_at',at=at)
        unclassified=sum(a.get('job_id')==job_id and a.get('intended_role') not in ('end_user','authorized_person','maintainer') for a in approvals)
        human=waits(decisions)
        if unclassified: human['seconds']=None
        summaries[job_id]={'runs':len(selected),'retries_known':sum((r['attempt_ordinal'] or 0)>1 for r in selected),
            'attempts_with_ids':len({r['attempt_id'] for r in selected if r['attempt_id']}),'history_truncated':bool(truncated),
            'history_incomplete':bool(incomplete),
            'tokens':{k:_coverage([r[k] for r in selected],final=[r['measurement']=='final' and not incomplete for r in selected])
                      for k in ('input_total','input_uncached','cache_read','cache_creation','output','weighted')},
            'cost_usd':_coverage([r['cost_usd_reported'] for r in selected],final=[not incomplete for r in selected]),
            'run_wall_seconds_union':_intervals(selected),'human_decision_wait':human,'maintenance_wait':waits(held),
            'formal_receipt_claimed':False,'actual_human_work_seconds':None,
            'unclassified_approvals':unclassified,
            'inherited_context_characters':_coverage([r['inherited_context_characters'] for r in selected])}
    def groups(field):
        grouped={}
        for row in rows:
            if row.get(field) is not None: grouped.setdefault(row[field],[]).append(row)
        return {'unattributed_runs':sum(r.get(field) is None for r in rows),'groups':{
            key:{'runs':len(items),'run_ids':[r['run_id'] for r in items],
                 'tokens':{part:_coverage([r[part] for r in items],final=[r['measurement']=='final' and not r['history_incomplete'] for r in items])
                           for part in ('input_total','input_uncached','cache_read','cache_creation','output')},
                 'cost_usd':_coverage([r['cost_usd_reported'] for r in items],final=[not r['history_incomplete'] for r in items])}
            for key,items in grouped.items()}}
    return {'schema_version':1,'rows':rows,'jobs':summaries,
            'by_task':groups('task_id'),'by_attempt':groups('attempt_id'),
            'by_session':groups('session_fingerprint'),'by_checkpoint':groups('checkpoint_id'),
            'deduplicated_snapshots':duplicate,
            'conflicting_run_ids':len(conflicts),'unidentified_snapshots':len(anonymous),
            'local_price_estimate_used':False,'cost_is_billing_invoice':False,'quota_included':False,'execution_permission':'none',
            'limitations':['Character counts are not tokens or remaining native context.',
                'Prepared payloads are not proof of Provider/model invocation.',
                'Human approval wait is not actual human work; overlapping intervals are unioned.',
                'Run wall time includes tool/approval waits; it is not CPU/model-compute time and must not be added to overlapping human waits.',
                'Missing/partial prices and usage remain unknown; known_sum is not a complete total.',
                'USD amounts are CLI-reported estimates, not invoices or subscription billing.',
                'Group dimensions describe the same runs and must not be added to one another.',
                'No artifact success denominator or actual personal receipt is inferred.']}


def context_budget(config):
    """Context budget setting (改修③). Default SHADOW: the selection is computed and recorded, nothing is cut.
    OFF keeps the old behaviour without recording; ON applies the selection to the reuse context only."""
    value = (config or {}).get('context_budget') or {}
    mode = value.get('mode', 'shadow')
    chars = value.get('reuse_chars', 8000)
    if mode not in BUDGET_MODES or type(chars) is not int or not 1000 <= chars <= 100000:
        return {'mode': 'off', 'reuse_chars': 8000}
    return {'mode': mode, 'reuse_chars': chars}


def codex_context_observation(token_usage, *, received_at, compacted=False):
    """Local receipt of Codex's LAST TURN INPUT proxy, never live occupancy.

    Cumulative totals and character counts cannot fill missing context fields.
    No prompt, native session identifier, token/cost estimate or default 0 stored.
    """
    import math
    if isinstance(received_at, bool) or not isinstance(received_at, (int, float)) or not math.isfinite(received_at):
        raise ValueError('文脈観測の時刻が不正です。')
    source = token_usage if isinstance(token_usage, dict) else {}
    last = source.get('last') if isinstance(source.get('last'), dict) else {}
    capacity = source.get('modelContextWindow')
    capacity = capacity if type(capacity) is int and 0 < capacity <= 2**63 - 1 else None
    used = last.get('inputTokens')
    used = used if type(used) is int and 0 <= used <= 2**63 - 1 else None
    known = not compacted and capacity is not None and used is not None and used <= capacity
    return {'schema_version': 1, 'provider': 'codex',
            'measurement': 'proxy' if known else 'unknown', 'metric': 'last_turn_input_tokens',
            'capacity_tokens': capacity, 'used_tokens': used if known else None,
            'observed_at': float(received_at), 'time_source': 'local_receipt',
            'invalidated_by': 'compaction' if compacted else None,
            'reason': 'last_turn_is_not_occupancy' if known else ('compaction' if compacted else 'unavailable_or_invalid')}
