"""PostToolUse hook (Edit|Write): check the edited Python file and report problems back to Claude.

Uses pyflakes when it is installed, otherwise a syntax check with the standard library. Only the edited file
is checked (the old copies under data/backups are skipped), nothing is written, and the edit is never blocked.
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return
    path = (data.get('tool_input') or {}).get('file_path') or (data.get('tool_response') or {}).get('filePath')
    if not path or not str(path).lower().endswith('.py'):
        return
    file = Path(path)
    if not file.is_file() or ('data' in file.parts and 'backups' in file.parts):
        return
    if importlib.util.find_spec('pyflakes'):
        tool = 'pyflakes'
        run = subprocess.run([sys.executable, '-m', 'pyflakes', str(file)], capture_output=True, text=True,
                             encoding='utf-8', errors='replace', timeout=60)
        found = (run.stdout + run.stderr).strip()
    else:
        tool = '構文チェック（pyflakes は未導入）'
        try:
            compile(file.read_text(encoding='utf-8-sig'), str(file), 'exec')
            found = ''
        except SyntaxError as exc:
            found = f'{file}:{exc.lineno}: {exc.msg}'
    if found:
        print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PostToolUse',
            'additionalContext': f'{tool} の指摘（{file.name}）。修正が必要か確認してください:\n{found[:3000]}'}},
            ensure_ascii=False))


if __name__ == '__main__':
    main()
