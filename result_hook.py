"""Claude PostToolUse / PostToolUseFailure bridge for Bash: send the command's result to 采来 (2026-10-09).

采来 records the exit code and a masked output tail as evidence for the reviewers, so a review does not rely on the
assignee's own report of a run. Best effort: any failure is ignored and never changes the tool result.

What Claude Code sends (checked with CLI 2.1.289): a command that exits 0 fires PostToolUse with tool_response
{stdout, stderr, interrupted, ...} and no exit code; any other exit fires PostToolUseFailure (not PostToolUse) with
error "Exit code N\\n<output>" and is_interrupt. So success means exit code 0, and a failure's code is read from error.
"""
import json
import os
import re
import sys
import urllib.request

FAILED = re.compile(r'^(?:Error:\s*)?Exit code (-?\d+)\s*\n?', re.I)


def output_of(response):
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        parts = [str(response.get('stdout') or ''), str(response.get('stderr') or '')]
        return parts[0] + ('\n[stderr]\n' + parts[1] if parts[1].strip() else '')
    return ''


def exit_code_of(response):
    if isinstance(response, dict):
        for key in ('exit_code', 'exitCode', 'returncode', 'returnCode', 'code'):
            if isinstance(response.get(key), int):
                return response[key]
    return None


def result_of(data):
    """(exit_code, output, timed_out) from one hook payload."""
    if data.get('hook_event_name') == 'PostToolUseFailure':
        error = str(data.get('error') or '')
        match = FAILED.match(error)
        return (int(match.group(1)) if match else None, error[match.end():] if match else error,
                data.get('is_interrupt') is True)
    response = data.get('tool_response')
    interrupted = isinstance(response, dict) and response.get('interrupted') is True
    code = exit_code_of(response)
    # PostToolUse fires only when the command exited 0 (other codes go to PostToolUseFailure).
    return (code if code is not None else (None if interrupted else 0)), output_of(response), interrupted


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        data = json.load(sys.stdin)
        if data.get('tool_name') != 'Bash':
            return
        exit_code, output, timed_out = result_of(data)
        from team_security_audit import mask
        output = mask(output)
        payload = json.dumps({'task_id': os.environ['AGENT_TEAM_TASK'], 'tool': 'Bash',
                              'command': (data.get('tool_input') or {}).get('command', ''),
                              'exit_code': exit_code, 'output': output[-20000:], 'timed_out': timed_out}).encode()
        request = urllib.request.Request(os.environ['AGENT_TEAM_ENDPOINT'] + '/worker/tool-result', data=payload,
                                         headers={'Content-Type': 'application/json',
                                                  'Authorization': 'Bearer ' + os.environ['AGENT_TEAM_RUN_TOKEN']})
        with urllib.request.urlopen(request, timeout=30):
            pass
    except Exception:
        pass


if __name__ == '__main__':
    main()
