"""Independent checks for synthetic-due-date-v1. Inspect the generated due_date.py before running.

The module is executed in a separate isolated Python process with a timeout, so an endless
loop or exception cannot stall the evaluator. Expected values are fixed here, outside the AI project.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

CASES = ['weekday_unchanged', 'closed_rolls_forward', 'consecutive_closed', 'month_year_boundary',
         'zero_days', 'negative_days_rejected', 'all_closed_rejected', 'input_unchanged']

CHILD = r'''
import copy, importlib.util, json, sys
from datetime import date
spec = importlib.util.spec_from_file_location('synthetic_due_date', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
f = module.due_date
results = {}
def run(name, fn):
    try: results[name] = bool(fn())
    except Exception: results[name] = False
def raises(fn):
    try: fn()
    except ValueError: return True
    return False
run('weekday_unchanged', lambda: f(date(2026, 10, 13), 2, {5, 6}) == date(2026, 10, 15))
run('closed_rolls_forward', lambda: f(date(2026, 10, 13), 4, {5, 6}) == date(2026, 10, 19))
run('consecutive_closed', lambda: f(date(2026, 10, 13), 4, {0, 5, 6}) == date(2026, 10, 20))
run('month_year_boundary', lambda: f(date(2026, 12, 28), 5, {5, 6}) == date(2027, 1, 4))
run('zero_days', lambda: f(date(2026, 10, 18), 0, {6}) == date(2026, 10, 19)
    and f(date(2026, 10, 13), 0, {6}) == date(2026, 10, 13))
run('negative_days_rejected', lambda: raises(lambda: f(date(2026, 10, 13), -1, {6})))
run('all_closed_rejected', lambda: raises(lambda: f(date(2026, 10, 13), 1, list(range(7)))))
def unchanged():
    closed = [5, 6]
    before = copy.deepcopy(closed)
    f(date(2026, 10, 13), 4, closed)
    return closed == before
run('input_unchanged', unchanged)
print(json.dumps(results))
'''


def check(module_path, timeout=20):
    module_path = Path(module_path).resolve()
    try:
        run = subprocess.run([sys.executable, '-I', '-B', '-X', 'utf8', '-c', CHILD, str(module_path)],
                             capture_output=True, text=True, encoding='utf-8', timeout=timeout,
                             cwd=module_path.parent)
        passed = json.loads(run.stdout.strip().splitlines()[-1]) if run.returncode == 0 and run.stdout.strip() else {}
        error = None if passed else 'module_failed'
    except subprocess.TimeoutExpired:
        passed, error = {}, 'timeout'
    checks = [{'case': name, 'passed': passed.get(name) is True} for name in CASES]
    return {'case_id': 'synthetic-due-date-v1', 'checks': checks,
            'all_passed': all(c['passed'] for c in checks), 'error': error}


def readme_status(project):
    readme = Path(project)/'README.md'
    seed = (Path(__file__).resolve().parent/'due_date_readme_seed.md').read_text(encoding='utf-8')
    text = readme.read_text(encoding='utf-8') if readme.exists() else ''
    # Presence only. Whether the example really works is a human check, never keyword-scored.
    return {'readme_changed': bool(text) and text != seed, 'readme_mentions_call': 'due_date(' in text,
            'readme_example_verified': 'human_check_required'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('base', type=Path)
    parser.add_argument('--round', type=int, required=True)
    args = parser.parse_args()
    base = args.base.resolve()
    project = base/'development'
    if args.round < 1 or not (base/'manifest.json').exists() or not project.is_dir():
        raise ValueError('Use an existing due-date evaluation base and a positive round number')
    result = dict(check(project/'due_date.py'), round=args.round, **readme_status(project))
    (base/'checks').mkdir(exist_ok=True)
    target = base/'checks'/f'round-{args.round}.json'
    if target.exists():
        raise FileExistsError('This round has already been recorded')
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    lines = ['# 独立確認（評価側）', '', f'確認回：{args.round}（制作担当の成功回数に対応）', '']
    lines += [f'- {c["case"]}: {"合格" if c["passed"] else "不合格"}' for c in result['checks']]
    lines += ['', f'全項目合格：{"はい" if result["all_passed"] else "いいえ"}',
              f'README変更：{"あり" if result["readme_changed"] else "なし"}',
              'READMEの使用例が実際に動くかは未確認（本人の確認項目）。',
              'この確認は評価側が実施した。レビュー担当は未確認を確認済みとしないこと。']
    (project/'EVALUATION_CHECKS.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(json.dumps({'round': args.round, 'all_passed': result['all_passed'], 'error': result['error']}))


if __name__ == '__main__':
    main()
