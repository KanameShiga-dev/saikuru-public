"""Load the user's shared role instructions for each 采来 — サイクル — task."""
import hashlib
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from team_adapters import ProviderError


ROLE_AGENTS = {
    'planner': ('common-explorer',),
    'researcher': ('common-explorer',),
    'builder': ('common-implementer',),
    'reviewer': ('common-reviewer',),
}
MAX_AGENT_FILE_BYTES = 64_000


@dataclass(frozen=True)
class AgentDefinition:
    name: str
    description: str
    instructions: str
    digest: str
    sandbox: str
    tools: tuple


def _definition(name, description, instructions, digest, sandbox, tools):
    expected = 'workspace-write' if name == 'common-implementer' else 'read-only'
    if sandbox != expected or not isinstance(description, str) or not description.strip():
        raise ProviderError(f'共通Agentの説明または権限が不正です: {name}')
    allowed = {'Read', 'Glob', 'Grep', 'Edit', 'Write', 'Bash'} if expected == 'workspace-write' else {'Read', 'Glob', 'Grep'}
    if not tools or set(tools) - allowed:
        raise ProviderError(f'共通Agentのツール権限が担当範囲を超えています: {name}')
    return AgentDefinition(name, description.strip(), instructions, digest, sandbox, tuple(tools))


def names_for_role(role):
    return ROLE_AGENTS.get(role, ())


def _load_codex(name):
    path = Path.home() / '.codex' / 'agents' / f'{name}.toml'
    if not path.is_file():
        raise ProviderError(f'共通Agent定義がありません: {name} (Codex)')
    raw = path.read_bytes()
    if len(raw) > MAX_AGENT_FILE_BYTES:
        raise ProviderError(f'共通Agent定義が大きすぎます: {name}')
    try:
        data = tomllib.loads(raw.decode('utf-8-sig'))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ProviderError(f'共通Agent定義を読み取れません: {name} (Codex TOML)') from exc
    if data.get('name') != name or not isinstance(data.get('developer_instructions'), str) or not data['developer_instructions'].strip():
        raise ProviderError(f'共通Agent定義のnameまたはdeveloper_instructionsが不正です: {name}')
    sandbox = data.get('sandbox_mode', 'read-only')
    tools = ('Read', 'Glob', 'Grep', 'Edit', 'Write', 'Bash') if sandbox == 'workspace-write' else ('Read', 'Glob', 'Grep')
    return _definition(name, data.get('description'), data['developer_instructions'].strip(),
                       hashlib.sha256(raw).hexdigest(), sandbox, tools)


def _load_claude(name):
    path = Path.home() / '.claude' / 'agents' / f'{name}.md'
    if not path.is_file():
        raise ProviderError(f'共通Agent定義がありません: {name} (Claude Code)')
    raw = path.read_bytes()
    if len(raw) > MAX_AGENT_FILE_BYTES:
        raise ProviderError(f'共通Agent定義が大きすぎます: {name}')
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeError as exc:
        raise ProviderError(f'共通Agent定義を読み取れません: {name} (Claude Markdown)') from exc
    match = re.match(r'\A---\s*\r?\n(.*?)\r?\n---\s*\r?\n(.*)\Z', text, re.S)
    if not match or not match.group(2).strip():
        raise ProviderError(f'共通Agent定義のfrontmatterまたは本文が不正です: {name}')
    fields = dict(re.findall(r'^([A-Za-z]+):[ \t]*(.*)$', match.group(1), re.M))
    if fields.get('name', '').strip().strip('"\'') != name:
        raise ProviderError(f'共通Agent定義のnameが不正です: {name}')
    tools = tuple(t.strip().strip('"\'') for t in fields.get('tools', '').strip(' []\r').split(',') if t.strip())
    sandbox = 'workspace-write' if name == 'common-implementer' else 'read-only'
    return _definition(name, fields.get('description'), match.group(2).strip(),
                       hashlib.sha256(raw).hexdigest(), sandbox, tools)


def load_agent(role, provider, selected_name=None):
    names = names_for_role(role)
    if not names:
        raise ProviderError(f'共通Agentの割り当てがありません: {role}')
    name = selected_name or names[0]
    allowed = names + (('common-security-reviewer',) if role == 'reviewer' else ()) + (('common-harness-auditor',) if role == 'researcher' else ())
    if name not in allowed:
        raise ProviderError(f'この担当には指定の共通Agentを割り当てられません: {name}')
    loader = _load_codex if provider == 'codex' else _load_claude if provider == 'claude' else None
    if loader is None:
        raise ProviderError(f'共通Agentの定義形式がありません: {provider}')
    return loader(name)


def load_for_task(role, provider):
    definition = load_agent(role, provider)
    return (definition.name,), {definition.name: definition.digest}, definition.instructions
