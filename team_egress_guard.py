"""Fail-closed checks immediately before model input and on returned tool output.

Findings contain category labels only. The guard deliberately does not reject every
absolute path: ordinary project paths such as C:\\AI_Work\\demo remain usable.
"""
import re

from team_dlp import (CARD, EMAIL, INTERNAL_HOST, PHONE, PRIVATE_IP,
                      SECRET, confidential_terms, has_card_number, has_personal_number)

USER_PROFILE_PATH = re.compile(r'(?i)\b[A-Z]:[\\/]+Users[\\/]+[^\\/\s"<>]+')
HOME_PATH = re.compile(r'(?i)(?:^|(?<=[\s"\'=:]))(?:~|/home|/Users)[\\/]+[^\\/\s"<>]+')
USER_PROFILE_ENV = re.compile(r'(?i)%USERPROFILE%')
UNC_PATH = re.compile(r'''(?<![\\\w:])\\\\[^\\/\s"'<>|?*]+\\(?!\\)[^\\/\s"'<>|?*]+''')
JSON_UNC_PATH = re.compile(r'''(?<![\\\w:])(?:\\\\){2}[^\\/\s"'<>|?*]+(?:\\\\)[^\\/\s"'<>|?*]+''')
LOOPBACK_URL = re.compile(r'(?i)\bhttps?://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?(?:[/\?#]|$)')
LOOPBACK_REFERENCE = re.compile(r'(?i)\bhttps?://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?[^\s"\'<>]*')
BLOCK_MARKER = '[EGRESS_BLOCKED: protected output withheld]'


def redact_loopback_urls(text):
    """Keep local launch evidence in storage while withholding its URL from models."""
    return LOOPBACK_REFERENCE.sub('[LOCAL_LOOPBACK_URL]', text)


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def findings(value):
    """Return safe category labels only; never return matched text."""
    if not isinstance(value, (str, dict, list, tuple)):
        return ['unsupported_payload']
    text = '\n'.join(_strings(value))
    result = []
    for pattern, label in (
        (SECRET, 'secret'), (EMAIL, 'email'), (PHONE, 'phone'),
        (PRIVATE_IP, 'private_ip'),
        (INTERNAL_HOST, 'internal_host'), (USER_PROFILE_PATH, 'user_profile_path'),
        (HOME_PATH, 'home_path'), (USER_PROFILE_ENV, 'user_profile_variable'),
        (UNC_PATH, 'network_path'), (JSON_UNC_PATH, 'network_path'),
        (LOOPBACK_URL, 'loopback_url'),
    ):
        if pattern.search(text):
            result.append(label)
    if has_personal_number(text):
        result.append('personal_number')
    try:
        if has_card_number(text):
            result.append('possible_card_number')
        if any(term.casefold() in text.casefold() for term in confidential_terms()):
            result.append('configured_confidential_term')
    except (OSError, ValueError):
        result.append('policy_unavailable')
    return list(dict.fromkeys(result))


def require_safe_egress(value, route='model_input'):
    """Raise a value-only error when model-bound material needs review."""
    labels = findings(value)
    if labels:
        raise ValueError('外部送信ガードで停止しました（分類: ' + ', '.join(labels) + '）。内容を確認してください。')
    return value


def _redact_safe_user_paths(text):
    text = USER_PROFILE_PATH.sub('[LOCAL_USER_PROFILE]', text)
    text = HOME_PATH.sub('[LOCAL_USER_PROFILE]', text)
    return USER_PROFILE_ENV.sub('[USER_PROFILE]', text)


def tool_output_findings(value):
    """findings() for returned tool output. A loopback URL (127.0.0.1, localhost) in a README or source file is
    ordinary project text, not a leak, so it must not withhold the output or stop the session. Secrets, personal
    data and profile paths are still withheld. Model input keeps the stricter findings()."""
    return [label for label in findings(value) if label != 'loopback_url']


def sanitize_tool_output(value):
    """Redact profile-directory labels; withhold other detected output wholesale."""
    original_findings = tool_output_findings(value)

    def redact(item):
        if isinstance(item, str):
            return _redact_safe_user_paths(item)
        if isinstance(item, dict):
            return {key: redact(part) for key, part in item.items()}
        if isinstance(item, list):
            return [redact(part) for part in item]
        if isinstance(item, tuple):
            return tuple(redact(part) for part in item)
        return item

    redacted = redact(value)
    remaining = tool_output_findings(redacted)
    if remaining:
        labels = list(dict.fromkeys(original_findings + remaining))
        return _withhold(redacted), labels, True
    changed = redacted != value
    return redacted, original_findings, changed


def _withhold(value):
    if isinstance(value, str):
        return BLOCK_MARKER
    if isinstance(value, dict):
        return {key: _withhold(item) if isinstance(item, (str, dict, list, tuple)) else item
                for key, item in value.items()}
    if isinstance(value, list):
        return [_withhold(item) if isinstance(item, (str, dict, list, tuple)) else item for item in value]
    if isinstance(value, tuple):
        return tuple(_withhold(item) if isinstance(item, (str, dict, list, tuple)) else item for item in value)
    return value


def batch_block_reason(payload):
    """Return a safe reason when a Claude tool batch still contains protected output."""
    calls = payload.get('tool_calls') if isinstance(payload, dict) else None
    if not isinstance(calls, list):
        return 'tool_batch_unavailable'
    labels = []
    for call in calls:
        if not isinstance(call, dict) or 'tool_response' not in call:
            return 'tool_batch_unavailable'
        response = call.get('tool_response')
        if any(BLOCK_MARKER in text for text in _strings(response)):
            labels.append('protected_output_withheld')
        labels.extend(tool_output_findings(response))
    labels = list(dict.fromkeys(labels))
    return ', '.join(labels) if labels else ''
