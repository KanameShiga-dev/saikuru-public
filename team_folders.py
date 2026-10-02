"""Folder navigation confined to the existing local project boundary."""
import re
from pathlib import Path
from team_config import ROOT

BASE = Path('C:/Projects').resolve()


def folder(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('フォルダを指定してください。')
    path = Path(value)
    if not path.is_absolute():
        raise ValueError('フォルダは絶対パスで指定してください。')
    path = path.resolve()
    if not path.is_relative_to(BASE) or not path.is_dir():
        raise ValueError('C:\\Projects内の既存フォルダを指定してください。')
    if path.is_relative_to(ROOT) and not path.is_relative_to(ROOT / 'sample-project'):
        raise ValueError('統括サービスの内部フォルダは選択できません。')
    return path


def project_folder(value):
    path = folder(value)
    if path == BASE or ROOT.is_relative_to(path):
        raise ValueError('個別のプロジェクトフォルダを選択してください。')
    return path


def listing(value):
    path = folder(value or str(BASE))
    entries = []
    try:
        for item in path.iterdir():
            if item.name.startswith('.'):
                continue
            try:
                resolved = folder(str(item))
                entries.append({'name': item.name, 'path': str(resolved)})
            except (OSError, ValueError, RuntimeError):
                continue
    except OSError:
        raise ValueError('このフォルダの一覧を取得できません。') from None
    try:
        project_folder(str(path))
        selectable = True
    except ValueError:
        selectable = False
    return {'path': str(path), 'parent': str(path.parent) if path != BASE else None,
            'entries': sorted(entries, key=lambda e: e['name'].casefold()), 'selectable': selectable}


def create(parent, name):
    parent = folder(parent)
    if not isinstance(name, str) or not name or len(name) > 100:
        raise ValueError('新しいフォルダ名を1〜100文字で入力してください。')
    if (name != name.strip() or name.endswith('.') or name.startswith('.') or
            re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or
            re.fullmatch(r'(?i:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?', name)):
        raise ValueError('フォルダ名に使用できない文字・予約名が含まれています。')
    target = parent / name
    if not target.resolve().is_relative_to(BASE):
        raise ValueError('作成先が作業フォルダの外です。')
    try:
        target.mkdir()  # No overwrite, no recursive parent creation.
    except FileExistsError:
        raise ValueError('同じ名前が既に存在します。別の名前にするか、既存フォルダを選択してください。') from None
    except OSError:
        raise ValueError('フォルダを作成できません。名前とアクセス権を確認してください。') from None
    return str(target)
