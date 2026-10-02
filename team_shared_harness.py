"""Metadata-only skill discovery and narrowly scoped shared-document reads."""
import json
from pathlib import Path


def skills():
    registry = Path.home()/'.codex/shared-harness.json'
    if not registry.is_file():
        return []
    data = json.loads(registry.read_text(encoding='utf-8'))
    root = Path(data['root']).resolve()
    result = []
    for item in data.get('skills', [])[:32]:
        path = Path(item['path']).resolve()
        if (path.name != 'SKILL.md' or not path.is_relative_to(root) or not path.is_file()
                or path.parent.name != item['name'] or len(item['description']) > 300):
            raise ValueError('共通Skillの登録パスまたは説明が不正です。')
        result.append(dict(name=item['name'], description=item['description'], path=str(path)))
    return result


def catalog(agent_name=None):
    available = skills()
    routed = {'common-explorer': {'project-analysis','debug'},
              'common-implementer': {'debug','test'}, 'common-reviewer': {'code-review'},
              'common-security-reviewer': {'security-review'}, 'common-harness-auditor': {'harness-audit'}}
    if agent_name in routed:
        available = [s for s in available if s['name'] in routed[agent_name]]
    if not available:
        return ''
    return ('\n共通Skill（名前・説明のみ。本文は必要な場合だけReadで読む）:\n'
            + '\n'.join(f"- {s['name']}: {s['description']} | {s['path']}" for s in available)
            + '\n該当Skillと必要なreferenceのみを読み、全部の先読みは禁止。単純作業でSkillを使う必要はありません。\n')


def shared_document(path):
    candidate = Path(path).resolve()
    if candidate.suffix.lower() != '.md' or not candidate.is_file():
        return None
    for item in skills():
        folder = Path(item['path']).parent
        if candidate == folder/'SKILL.md':
            return (item['name'], 'skill')
        if candidate.is_relative_to(folder/'references'):
            return (item['name'], 'reference')
    return None
