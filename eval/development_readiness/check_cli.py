"""Synthetic CLI checks. Inspect the generated CLI before running this local evaluator."""
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path


def check(project):
    project = Path(project).resolve()
    cases = [('boundary', 'item,quantity\na,5\n', '5', 0, 'a\n'),
             ('duplicates', 'item,quantity\na,3\na,4\n', '5', 0, ''),
             ('japanese', 'item,quantity\n箱,0\n本,1\n', '1', 0, '本\n箱\n'),
             ('header_only', 'item,quantity\n', '0', 0, ''),
             ('negative', 'item,quantity\na,-1\n', '5', 2, ''),
             ('invalid_integer', 'item,quantity\na,abc\n', '5', 2, ''),
             ('missing_column', 'item\na\n', '5', 2, ''),
             ('invalid_threshold', 'item,quantity\na,1\n', 'abc', 2, '')]
    results = []
    with tempfile.TemporaryDirectory(prefix='sairai-stock-check-') as tmp:
        for name, content, threshold, code, output in cases:
            source = Path(tmp)/'sample.csv'
            source.write_text(content, encoding='utf-8')
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            run = subprocess.run([sys.executable, '-B', '-X', 'utf8', str(project/'stock_cli.py'), str(source), threshold],
                                 cwd=tmp, capture_output=True, text=True, encoding='utf-8', timeout=10)
            passed = (run.returncode == code and run.stdout == output
                      and (code == 0 or bool(run.stderr.strip()))
                      and hashlib.sha256(source.read_bytes()).hexdigest() == before)
            results.append({'case': name, 'passed': passed})
        run = subprocess.run([sys.executable, '-B', '-X', 'utf8', str(project/'stock_cli.py'), str(Path(tmp)/'missing.csv'), '1'],
                             cwd=tmp, capture_output=True, text=True, encoding='utf-8', timeout=10)
        results.append({'case': 'missing_file', 'passed': run.returncode != 0 and bool(run.stderr.strip())})
    return {'checks': results, 'all_passed': all(r['passed'] for r in results),
            'readme_present': (project/'README.md').exists(), 'human_acceptance': 'not_performed'}


if __name__ == '__main__':
    print(json.dumps(check(sys.argv[1]), ensure_ascii=False, indent=2))
