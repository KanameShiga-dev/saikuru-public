"""Measure, through the real CLI, what reference text and the same text as images cost (参考情報の画像化の校正).

For one adapter/model it sends three short requests with the same instruction:
  A: no reference   B: the reference as text   C: the reference as 采来's images (same preface as in use)
and takes the input-token totals the CLI reports (uncached + cache read + cache write). The differences give
tokens per character and per megapixel for that model. The answer of B and C must give the check numbers hidden
in the reference, so that image reading is confirmed, not assumed. The result goes to data/context-estimators.json;
the optimiser uses only measured, recent, readable entries.

Only fixed sample prose is sent (no project content). Usage:  python -X utf8 team_context_calibrate.py claude sonnet
"""
import base64
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from team_context_renderer import RENDERER_VERSION, render

CALIBRATION_VERSION = 'cli-usage-diff-1'
QUESTION = ('参考情報の中にある「確認番号」を、出てくる順に半角のカンマ区切りで答えてください。'
            '参考情報が無ければ「なし」とだけ答えてください。ほかの文は書かないでください。')
SAMPLE = ('この資料は、社内の作業手順を見直したときの経緯をまとめた参考情報です。以前は担当が替わるたびに、'
          '前の担当が決めたことを口頭で伝えていました。そのため、同じ確認を何度も繰り返したり、決めた理由が分からなく'
          'なったりすることがありました。見直しでは、決めたことと理由を一か所に書き残し、次の担当が読んでから作業を始める'
          'ようにしました。書き残す内容は、決めたこと、その理由、まだ決まっていないこと、確認した日付の四つです。'
          '長い説明は避け、一つの項目には一つの事実だけを書きます。読む側は、書かれていることを前提にしつつ、'
          '古くなっていそうな点は確かめてから使います。')


def sample_text(chars):
    codes = [str(secrets.randbelow(9000) + 1000) for _ in range(3)]
    body = (SAMPLE * (chars // len(SAMPLE) + 2))
    third = chars // 3
    text = (body[:third] + f'確認番号は{codes[0]}です。' + body[third:2 * third] + f'確認番号は{codes[1]}です。'
            + body[2 * third:chars] + f'確認番号は{codes[2]}です。')
    return text, codes


def _total(usage):
    parts = [usage.get(k) for k in ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')]
    if any(not isinstance(p, int) for p in parts):
        raise RuntimeError('CLIが入力トークンの内訳を返しませんでした。')
    return sum(parts)


def _claude(command, model, content, cwd):
    from team_config import claude_config_directory
    env = dict(os.environ, CLAUDE_CONFIG_DIR=str(claude_config_directory()), PYTHONIOENCODING='utf-8')
    from team_claude_auth import ENV, read_stored_token
    token = read_stored_token()   # the long-lived token, when the user stored one (same as the workers)
    if token:
        env.pop('ANTHROPIC_API_KEY', None)
        env[ENV] = token
    args = command + ['-p', '--input-format', 'stream-json', '--output-format', 'stream-json', '--verbose',
                      '--no-session-persistence', '--model', model, '--tools', '', '--strict-mcp-config',
                      '--mcp-config', json.dumps({'mcpServers': {}}), '--permission-mode', 'default']
    message = json.dumps({'type': 'user', 'message': {'role': 'user', 'content': content}, 'parent_tool_use_id': None},
                         ensure_ascii=False)
    done = subprocess.run(args, input=message + '\n', capture_output=True, text=True, encoding='utf-8', errors='replace',
                          cwd=cwd, env=env, timeout=300, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    for line in reversed(done.stdout.splitlines()):
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get('type') == 'result':
            if msg.get('is_error'):
                raise RuntimeError('Claude Code: ' + str(msg.get('result'))[:300])
            return _total(msg.get('usage') or {}), str(msg.get('result') or '').strip()
    raise RuntimeError('Claude Code の結果を受け取れませんでした: ' + done.stderr[-300:])


def calibrate(adapter, model, chars=1500, opts=None):
    from team_attachments import REFERENCE_IMAGE_RULE, image_block
    from team_config import discover
    from team_context_optimizer import DEFAULTS
    if adapter != 'claude':
        raise ValueError('校正は現在 Claude だけに対応しています（Codex はこのPCに無く、Copilot は画像を送れません）。')
    command = discover()['claude']
    if not command:
        raise RuntimeError('Claude Code CLI が見つかりません。')
    opts = dict(DEFAULTS, **(opts or {}))
    text, codes = sample_text(chars)
    pages = render(text, opts['render_width_px'], opts['render_height_px'], opts['min_font_px'], opts['max_generated_pages'])
    from team_attachments import dimensions
    area = sum(w * h for _, w, h in (dimensions(p) for p in pages))
    with tempfile.TemporaryDirectory(prefix='saikuru-calibrate-') as cwd:
        a, _ = _claude(command, model, [{'type': 'text', 'text': QUESTION}], cwd)
        b, answer_text = _claude(command, model, [{'type': 'text', 'text': QUESTION + '\n\n参考情報:\n' + text}], cwd)
        c, answer_image = _claude(command, model, [{'type': 'text', 'text': QUESTION + '\n\n参考情報は画像で渡します。'},
                                                   {'type': 'text', 'text': REFERENCE_IMAGE_RULE}]
                                  + [image_block(base64.b64encode(p).decode('ascii'), 'claude') for p in pages], cwd)
    expected = ','.join(codes)
    normalise = lambda s: s.replace(' ', '').replace('、', ',').replace('，', ',')
    per_char = (b - a) / len(text)
    preface = len('\n\n参考情報は画像で渡します。') + len(REFERENCE_IMAGE_RULE) - len('\n\n参考情報:\n')
    per_megapixel = (c - a - preface * per_char) / (area / 1_000_000)
    return {'adapter': adapter, 'model': model, 'measured_at': time.time(), 'method': CALIBRATION_VERSION,
            'renderer_version': RENDERER_VERSION, 'font_px': opts['min_font_px'], 'render_width_px': opts['render_width_px'],
            'reference_chars': len(text), 'pages': len(pages), 'image_pixels': area,
            'tokens': {'baseline': a, 'text': b, 'image': c},
            'text_tokens_per_char': round(per_char, 4), 'image_tokens_per_megapixel': round(per_megapixel, 1),
            'text_answer_ok': normalise(answer_text) == expected, 'image_answer_ok': normalise(answer_image) == expected,
            'measured_saving_ratio': round(1 - (c - a) / (b - a), 3) if b > a else None}


def save(data_dir, entry):
    path = Path(data_dir) / 'context-estimators.json'
    try:
        current = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        current = {}
    current[entry['adapter'] + ':' + entry['model']] = entry
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)
    return path


def main():
    if len(sys.argv) not in (3, 4, 5):
        print('使い方: python -X utf8 team_context_calibrate.py claude <モデル> [文字数] [文字サイズpx]')
        return 2
    opts = {'min_font_px': int(sys.argv[4])} if len(sys.argv) == 5 else None
    entry = calibrate(sys.argv[1], sys.argv[2], int(sys.argv[3]) if len(sys.argv) >= 4 else 1500, opts)
    print(json.dumps(entry, ensure_ascii=False, indent=2))
    print('保存先:', save(Path(__file__).with_name('data'), entry))
    return 0


if __name__ == '__main__':
    sys.exit(main())
