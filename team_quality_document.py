"""Deterministic Markdown / HTML checks for document deliverables (SQAH Phase 2, record only).

Nothing here decides acceptance or changes a job. It reads a file, runs fixed rules, and returns
findings that carry line numbers and kinds but never the matched text, so a result file cannot copy a
secret it detected. No AI, process, network or conversion tool is used.

States per check: passed / failed / inconclusive. Anything not Markdown or HTML is inconclusive.
Accuracy of content, sources and layout is NOT judged here; those stay with reviewers and people.
"""
import hashlib
import json
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlparse

from team_dlp import EMAIL, MY_NUMBER, PHONE, SECRET, confidential_terms, has_card_number

MAX_BYTES = 2_000_000
EXTENSIONS = {'.md': 'markdown', '.markdown': 'markdown', '.html': 'html', '.htm': 'html'}
PLACEHOLDER = re.compile(r'\b(TODO|TBD|FIXME|XXX)\b|未記入|要確認|ここに記入|〇〇|○○|Lorem ipsum', re.I)
VOID_TAGS = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'track', 'wbr'}
HEADING = re.compile(r'^(#{1,6})\s+(.*?)\s*#*\s*$')
LINK = re.compile(r'(?<!\!)\[[^\]]*\]\(([^)\s]+)(?:\s+"[^"]*")?\)')
FENCE = re.compile(r'^\s*(```|~~~)')


def _check(check_id, state, detail='', lines=None):
    item = {'id': check_id, 'state': state, 'detail': detail}
    if lines:
        item['lines'] = sorted(set(lines))[:20]
    return item


def _markdown(text):
    headings, links, placeholder_lines, empty = [], [], [], []
    in_fence = False
    lines = text.splitlines()
    for number, line in enumerate(lines, 1):
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = HEADING.match(line)
        if match:
            title = match.group(2).strip()
            headings.append((number, len(match.group(1)), title))
            if not title:
                empty.append(number)
        if PLACEHOLDER.search(line):
            placeholder_lines.append(number)
        for target in LINK.findall(line):
            links.append((number, target))
    unfenced_open = in_fence
    return {'headings': headings, 'links': links, 'placeholder': placeholder_lines, 'empty': empty,
            'anchors': {_slug(title) for _, _, title in headings}, 'unbalanced': unfenced_open}


def _slug(title):
    cleaned = re.sub(r'[^\w\- ]', '', title.lower(), flags=re.UNICODE).strip()
    return re.sub(r'\s+', '-', cleaned)


class _Html(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.headings, self.links, self.ids, self.stack, self.errors = [], [], set(), [], []
        self.placeholder, self.empty = [], []
        self._heading = None
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get('id'):
            self.ids.add(attrs['id'])
        if tag in ('script', 'style'):
            self._skip += 1
        if tag in ('a', 'img', 'link', 'script') and (attrs.get('href') or attrs.get('src')):
            self.links.append((self.getpos()[0], attrs.get('href') or attrs.get('src')))
        if re.fullmatch(r'h[1-6]', tag):
            self._heading = [self.getpos()[0], int(tag[1]), '']
        if tag not in VOID_TAGS:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        # Self-closing form (<br/>, <img ... />): nothing is pushed on the open-tag stack.
        attrs = dict(attrs)
        if attrs.get('id'):
            self.ids.add(attrs['id'])
        if tag in ('a', 'img', 'link') and (attrs.get('href') or attrs.get('src')):
            self.links.append((self.getpos()[0], attrs.get('href') or attrs.get('src')))

    def handle_endtag(self, tag):
        if tag in ('script', 'style') and self._skip:
            self._skip -= 1
        if re.fullmatch(r'h[1-6]', tag) and self._heading:
            line, level, title = self._heading
            self.headings.append((line, level, title.strip()))
            if not title.strip():
                self.empty.append(line)
            self._heading = None
        if tag in VOID_TAGS:
            return
        if tag in self.stack:
            while self.stack and self.stack[-1] != tag:
                self.errors.append(self.getpos()[0])
                self.stack.pop()
            self.stack.pop()
        else:
            self.errors.append(self.getpos()[0])

    def handle_data(self, data):
        if self._skip:
            return
        if self._heading is not None:
            self._heading[2] += data
        if PLACEHOLDER.search(data):
            self.placeholder.append(self.getpos()[0])


def _html(text):
    parser = _Html()
    parser.feed(text)
    parser.close()
    errors = list(parser.errors) + [0] * len(parser.stack)
    return {'headings': parser.headings, 'links': parser.links, 'placeholder': parser.placeholder,
            'empty': parser.empty, 'anchors': parser.ids, 'unbalanced': bool(errors), 'error_lines': [e for e in errors if e]}


def _link_problems(links, anchors, base):
    """Relative file links that do not resolve and in-page anchors that do not exist. Remote URLs are not fetched."""
    broken = []
    for line, target in links:
        parsed = urlparse(target)
        if parsed.scheme or target.startswith(('//', 'mailto:', 'tel:', 'data:', 'javascript:')):
            continue
        if not parsed.path and parsed.fragment:
            if unquote(parsed.fragment) not in anchors and _slug(unquote(parsed.fragment)) not in anchors:
                broken.append(line)
            continue
        path = Path(unquote(parsed.path))
        if path.is_absolute() or '..' in path.parts or path.drive:
            broken.append(line)
        elif not (base / path).exists():
            broken.append(line)
    return broken


def inspect_text(name, text, base, required_headings=None):
    """Checks for one document. `base` resolves relative links. Returns {'format', 'checks'}."""
    kind = EXTENSIONS.get(Path(name).suffix.lower())
    if kind is None:
        return {'format': None, 'checks': [_check('DQA-01', 'inconclusive', 'Markdown・HTML 以外は機械検査の対象外')]}
    parsed = _markdown(text) if kind == 'markdown' else _html(text)
    checks = []
    if parsed['unbalanced']:
        detail = 'コードブロックが閉じていません' if kind == 'markdown' else 'タグの対応が崩れています'
        checks.append(_check('DQA-01a', 'failed', detail, parsed.get('error_lines')))
    else:
        checks.append(_check('DQA-01a', 'passed', '構造（' + ('コードブロック' if kind == 'markdown' else 'タグ') + '）が整っている'))
    if parsed['empty']:
        checks.append(_check('DQA-01b', 'failed', '見出しが空です', parsed['empty']))
    elif not parsed['headings']:
        checks.append(_check('DQA-01b', 'inconclusive', '見出しがありません'))
    else:
        checks.append(_check('DQA-01b', 'passed', f"見出し{len(parsed['headings'])}件、空の見出しなし"))
    if required_headings:
        have = {title.strip().casefold() for _, _, title in parsed['headings']}
        missing = [h for h in required_headings if h.strip().casefold() not in have]
        checks.append(_check('DQA-01c', 'failed' if missing else 'passed',
                             f'必須の見出しが{len(missing)}件ありません' if missing else '必須の見出しがそろっている'))
    broken = _link_problems(parsed['links'], parsed['anchors'], Path(base))
    checks.append(_check('DQA-01d', 'failed' if broken else 'passed',
                         '参照先のないリンクがあります（外部URLは確認していません）' if broken
                         else '相対リンクと内部アンカーの参照先がある（外部URLは確認していません）', broken))
    checks.append(_check('DQA-01e', 'failed' if parsed['placeholder'] else 'passed',
                         '未記入の印が残っています' if parsed['placeholder'] else '未記入の印なし', parsed['placeholder']))
    checks.extend(_sensitive(text))
    return {'format': kind, 'checks': checks}


def _sensitive(text):
    """Secrets, e-mail and phone numbers fail. Digit runs that look like a personal or card number only warn:
    in reports they are mostly dates, folder names and token counts (115 real documents: 11 of 11 hits)."""
    strict, weak = [], []
    if SECRET.search(text):
        strict.append('認証情報・秘密情報らしき文字列')
    if EMAIL.search(text):
        strict.append('メールアドレス')
    if PHONE.search(text):
        strict.append('電話番号')
    if MY_NUMBER.search(text):
        weak.append('12桁の番号らしき数字')
    if has_card_number(text):
        weak.append('カード番号らしき数字')
    if any(t.casefold() in text.casefold() for t in confidential_terms()):
        strict.append('登録された社内・顧客の固有名')
    if strict:
        return [_check('DQA-05', 'failed', '検出: ' + '、'.join(strict + weak) + '（値は記録しません）')]
    if weak:
        return [_check('DQA-05', 'inconclusive',
                       '注意: ' + '、'.join(weak) + '（日付・名前・件数の可能性が高いため不合格にはしていません。人が確認してください）')]
    return [_check('DQA-05', 'passed', '検出器で該当なし（登録外の固有名は検出できません）')]


def inspect_file(project, relative, required_headings=None):
    """Read one deliverable under the project and return its checks with the SHA-256 that was inspected."""
    root = Path(project).resolve()
    relative = Path(relative)
    if relative.is_absolute() or relative.drive or '..' in relative.parts or not relative.parts:
        raise ValueError('成果物は対象プロジェクト内の相対パスで指定してください。')
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file() or (root / relative).is_symlink():
        raise ValueError('成果物が存在しない、対象外、またはリンクです。')
    if path.stat().st_size > MAX_BYTES:
        return {'file': relative.as_posix(), 'sha256': None, 'format': None,
                'checks': [_check('DQA-01', 'inconclusive', '大きすぎるため検査していません')]}
    raw = path.read_bytes()
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        return {'file': relative.as_posix(), 'sha256': hashlib.sha256(raw).hexdigest(), 'format': None,
                'checks': [_check('DQA-01', 'inconclusive', 'UTF-8 として読めないため検査していません')]}
    result = inspect_text(relative.name, text, path.parent, required_headings)
    return {'file': relative.as_posix(), 'sha256': hashlib.sha256(raw).hexdigest(), **result}


def overall(checks):
    states = [c['state'] for c in checks]
    if 'failed' in states:
        return 'failed'
    return 'passed' if states and all(s == 'passed' for s in states) else 'inconclusive'


def record(store_dir, job_id, result):
    """Append the result as one JSON line next to (not inside) the operational DB. Never changes a job."""
    folder = Path(store_dir)
    folder.mkdir(parents=True, exist_ok=True)
    line = {'at': time.time(), 'job_id': job_id, 'file': result['file'], 'sha256': result['sha256'],
            'overall': overall(result['checks']), 'checks': result['checks']}
    with (folder / 'document-qa.jsonl').open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(line, ensure_ascii=False) + '\n')
    return line
