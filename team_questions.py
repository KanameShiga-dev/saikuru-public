"""Bounded choice questions and server-side answer validation."""
import re


def legacy_questions(text):
    """Extract explicitly labelled choices, without deciding on the user's behalf."""
    headings = list(re.finditer(r'(?m)^\s*(\d+)[.．]\s+(.+)$', text))
    if not headings:
        return [{'id': 'clarification', 'text': text, 'options': [
            {'id': 'answer', 'label': '回答を入力する', 'input_required': True, 'input_label': '回答'}]}]
    result = []
    for i, heading in enumerate(headings):
        block = text[heading.end():headings[i+1].start() if i+1 < len(headings) else len(text)]
        tail = ''
        if 'あわせて確認です：' in block:
            block, tail = block.split('あわせて確認です：', 1)
        choices = list(re.finditer(r'(?m)^\s*-\s*([A-F])([^：:\n]*?)[：:]\s*', block))
        if len(choices) < 2:
            raise ValueError('質問を項目別の選択肢に整理する必要があります。')
        options = []
        for j, choice in enumerate(choices):
            label = choice.group(1) + choice.group(2) + '：' + block[choice.end():choices[j+1].start() if j+1 < len(choices) else len(block)].strip()
            options.append({'id': choice.group(1), 'label': label, 'input_required': False, 'input_label': '補足（任意）'})
        result.append({'id': 'item-' + heading.group(1), 'text': heading.group(1) + '. ' + heading.group(2), 'options': options})
        if tail:
            result.append({'id': 'additional-confirmation', 'text': tail.strip(), 'options': [
                {'id': 'yes', 'label': 'はい（質問文に記載された条件に同意する）', 'input_required': False, 'input_label': '補足（任意）'},
                {'id': 'discuss', 'label': '条件を変更したい（内容を指定する）', 'input_required': True, 'input_label': '変更したい条件'}]})
    if '計画の承認' in text[:headings[0].start()]:
        result.insert(0, {'id': 'plan-confirmation', 'text': '提示された作業計画について', 'options': [
            {'id': 'accept', 'label': '以下の回答を条件に計画を承認する', 'input_required': False, 'input_label': '補足（任意）'},
            {'id': 'revise', 'label': '計画を修正して、再提示してほしい', 'input_required': True, 'input_label': '修正してほしい点'}]})
    return validate_questions(result)


def validate_questions(questions):
    if not isinstance(questions, list) or not 1 <= len(questions) <= 8:
        raise ValueError('質問は1〜8件にしてください。')
    ids = set()
    for q in questions:
        if not isinstance(q, dict) or not isinstance(q.get('id'), str) or not q['id'] or q['id'] in ids:
            raise ValueError('質問IDが不正または重複しています。')
        ids.add(q['id'])
        if not isinstance(q.get('text'), str) or not q['text'].strip():
            raise ValueError('質問文がありません。')
        if len(re.findall(r'(?m)^\s*\d+[.．]\s+', q['text'])) > 1 or len(re.findall(r'(?m)^\s*-\s*[A-F][（(：:]', q['text'])) > 1:
            raise ValueError('複数の質問や選択肢を質問文に詰め込まず、別々の項目にしてください。')
        options = q.get('options')
        if not isinstance(options, list) or not 1 <= len(options) <= 6:
            raise ValueError('選択肢は1〜6件にしてください。')
        option_ids = set()
        for option in options:
            if not isinstance(option, dict) or not isinstance(option.get('id'), str) or not option['id'] or option['id'] in option_ids:
                raise ValueError('選択肢IDが不正または重複しています。')
            option_ids.add(option['id'])
            if not isinstance(option.get('label'), str) or not option['label'].strip():
                raise ValueError('選択肢の説明がありません。')
            if type(option.get('input_required')) is not bool or not isinstance(option.get('input_label'), str):
                raise ValueError('選択肢の入力設定が不正です。')
    return questions


def native_questions(payload):
    rows = []
    for q in payload.get('details', {}).get('questions', []):
        if q.get('isSecret'):
            raise ValueError('秘密情報の質問はダッシュボードに保存できません。')
        options = [{'id': str(i), 'label': o['label'], 'input_required': False,
                    'input_label': '補足（任意）'} for i, o in enumerate(q.get('options') or [])]
        if not options or q.get('isOther'):
            options.append({'id': 'other', 'label': '別の回答を入力', 'input_required': True, 'input_label': '回答'})
        rows.append({'id': q['id'], 'text': q['question'], 'options': options})
    return validate_questions(rows)


def answers_for(questions, answers):
    validate_questions(questions)
    if not isinstance(answers, dict) or set(answers) != {q['id'] for q in questions}:
        raise ValueError('すべての質問に回答してください。')
    clean, lines, native = {}, [], {}
    for q in questions:
        answer = answers[q['id']]
        if not isinstance(answer, dict):
            raise ValueError('回答形式が不正です。')
        option = next((o for o in q['options'] if o['id'] == answer.get('option_id')), None)
        text = answer.get('text', '')
        if option is None or not isinstance(text, str) or len(text) > 1000:
            raise ValueError('選択肢または補足が不正です。補足は1000文字以内です。')
        text = text.strip()
        if option['input_required'] and not text:
            raise ValueError('選択した項目の入力欄を記入してください。')
        clean[q['id']] = {'option_id': option['id'], 'text': text}
        value = option['label'] + (' / ' + text if text else '')
        native[q['id']] = {'answers': [value]}
        lines.append(q['text'] + '\n回答: ' + value)
    note = '\n\n'.join(lines)
    if len(note) > 4000:
        raise ValueError('回答全体を4000文字以内に短くしてください。')
    return clean, note, native
