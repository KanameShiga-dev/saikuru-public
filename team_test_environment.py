"""Prepare and validate project-scoped temporary storage for approved code tests."""

import importlib.util
import os
from pathlib import Path
import shutil


class TestEnvironmentError(ValueError):
    """A required project-local test prerequisite cannot be prepared safely."""


def _is_link_or_junction(path):
    is_junction = getattr(path, 'is_junction', lambda: False)
    return path.is_symlink() or is_junction()


def _ensure_child_directory(parent, name, project_root):
    path = parent / name
    if _is_link_or_junction(path):
        raise TestEnvironmentError('テスト用一時領域にリンクまたはジャンクションがあるため、計画を開始できません。')
    created = False
    if not path.exists():
        try:
            path.mkdir()
            created = True
        except OSError:
            raise TestEnvironmentError('テスト用一時領域をプロジェクト内に準備できません。権限と保存先を確認してください。') from None
    if _is_link_or_junction(path) or not path.is_dir():
        raise TestEnvironmentError('テスト用一時領域が通常のフォルダではありません。')
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        raise TestEnvironmentError('テスト用一時領域を確認できません。') from None
    if not resolved.is_relative_to(project_root):
        raise TestEnvironmentError('テスト用一時領域が承認済みプロジェクトの外を指しています。')
    return path, created


def prepare_project_test_temp(project):
    """Create .local/test-tmp after plan approval, without following links outside the project."""
    try:
        project_root = Path(project).resolve(strict=True)
    except (OSError, TypeError, ValueError):
        raise TestEnvironmentError('承認済みプロジェクトの場所を確認できません。') from None
    if not project_root.is_dir():
        raise TestEnvironmentError('承認済みプロジェクトの場所がフォルダではありません。')

    local_dir, created_local = _ensure_child_directory(project_root, '.local', project_root)
    try:
        test_temp, created_temp = _ensure_child_directory(local_dir, 'test-tmp', project_root)
    except TestEnvironmentError:
        if created_local:
            try:
                local_dir.rmdir()
            except OSError:
                pass
        raise
    return test_temp.resolve(strict=True), created_local or created_temp


def _has_browser_install(directory):
    if _is_link_or_junction(directory):
        return False
    try:
        candidates = list(directory.iterdir())
    except OSError:
        return False
    for candidate in candidates:
        name = candidate.name.casefold()
        if (candidate.is_dir() and not _is_link_or_junction(candidate)
                and name.startswith(('chromium', 'firefox', 'webkit'))):
            executables = ('chrome-win/chrome.exe', 'chrome-win64/chrome.exe',
                           'chrome-headless-shell-win64/chrome-headless-shell.exe',
                           'firefox/firefox.exe', 'pw_run.exe')
            if any((candidate / relative).is_file()
                   and not any(_is_link_or_junction(part)
                               for part in (candidate / relative).parents if part != candidate.parent)
                   for relative in executables):
                return True
    return False


def playwright_status(project):
    """Read-only availability check; never invokes npx, installs a package or downloads a browser."""
    try:
        root = Path(project).resolve(strict=True)
    except (OSError, TypeError, ValueError):
        return {'available': False, 'driver_available': False, 'browser_available': False}
    driver = bool(shutil.which('playwright'))
    local_packages = (
        root / 'node_modules' / 'playwright' / 'cli.js',
        root / 'node_modules' / '@playwright' / 'test' / 'cli.js',
        root / 'node_modules' / '.bin' / 'playwright.cmd',
        root / 'node_modules' / '.bin' / 'playwright',
    )
    driver = driver or any(
        path.is_file() and not any(_is_link_or_junction(part) for part in path.parents if part != root)
        for path in local_packages)
    if not driver:
        try:
            driver = importlib.util.find_spec('playwright') is not None
        except (ImportError, ModuleNotFoundError, ValueError):
            driver = False

    browser_dirs = []
    configured = os.environ.get('PLAYWRIGHT_BROWSERS_PATH')
    if configured and configured != '0' and not configured.startswith(('\\\\', '//')):
        configured_path = Path(configured)
        if configured_path.is_absolute():
            browser_dirs.append(configured_path)
    local_app_data = os.environ.get('LOCALAPPDATA')
    if local_app_data:
        browser_dirs.append(Path(local_app_data) / 'ms-playwright')
    browser_dirs.extend((root / '.cache' / 'ms-playwright',
                         root / 'node_modules' / 'playwright-core' / '.local-browsers'))
    browsers = any(_has_browser_install(path) for path in browser_dirs)
    return {'available': bool(driver and browsers), 'driver_available': bool(driver),
            'browser_available': bool(browsers)}
