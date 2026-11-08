"""The work folder that holds the person's projects (config.json: workspace_root).

The server fixes it once at startup with configure(). Tools run outside the server
(ledger CLI, archive restore) read the App's own config file on first use of root().
"""
import json
from pathlib import Path

from team_config import ROOT

DEFAULT = Path.home() / 'SaikuruProjects'
_root = None


def resolve(config):
    """Check workspace_root and return the work folder; without the key, the default folder."""
    value = config.get('workspace_root')
    if value is None:
        return DEFAULT
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError('作業フォルダー（workspace_root）は絶対パスで指定してください。')
    path = Path(value).resolve()
    if not path.is_dir():
        raise ValueError('作業フォルダー（workspace_root）が見つかりません。既存のフォルダーを指定してください。')
    if path.parent == path:
        raise ValueError('作業フォルダー（workspace_root）にドライブの直下は使えません。')
    if path.is_relative_to(ROOT.resolve()):
        raise ValueError('作業フォルダー（workspace_root）に采来自身のフォルダーの中は使えません。')
    return path


def configure(config):
    """Fix the work folder for this process."""
    global _root
    _root = resolve(config)
    return _root


def prepare_default():
    """Service start only: make the default work folder when no workspace_root is set."""
    if root() == DEFAULT:
        DEFAULT.mkdir(exist_ok=True)


def root():
    global _root
    if _root is None:
        file = ROOT / 'data' / 'config.json'
        _root = resolve(json.loads(file.read_text(encoding='utf-8')) if file.is_file() else {})
    return _root
