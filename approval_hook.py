"""Claude PreToolUse bridge. Failure always denies; token cannot approve requests."""
import json
import os
import sys
import urllib.request


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    try:
        data = json.load(sys.stdin)
        # CLI-owned JSON result formatter: no filesystem, shell, or network action.
        # Other tool names (including MCP tools) still require the normal bridge.
        if data.get('tool_name') == 'StructuredOutput':
            print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse',
                'permissionDecision': 'allow',
                'permissionDecisionReason': 'Allow the CLI structured result formatter only.'}}))
            return
        payload = json.dumps({'task_id': os.environ['AGENT_TEAM_TASK'],
                              'tool': data.get('tool_name', ''), 'input': data.get('tool_input', {}),
                              'cwd': data.get('cwd', '')}).encode()
        request = urllib.request.Request(os.environ['AGENT_TEAM_ENDPOINT'] + '/worker/tool', data=payload,
                    headers={'Content-Type': 'application/json',
                             'Authorization': 'Bearer ' + os.environ['AGENT_TEAM_RUN_TOKEN']})
        with urllib.request.urlopen(request, timeout=930) as response:
            answer = json.load(response)
        decision = 'allow' if answer.get('allow') is True else 'deny'
        reason = answer.get('note') or '采来 — サイクル —の承認結果'
    except Exception:
        decision, reason = 'deny', '統括サービスに接続できないため実行を拒否しました。'
    print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse',
                      'permissionDecision': decision, 'permissionDecisionReason': reason}}, ensure_ascii=False))


if __name__ == '__main__':
    main()
