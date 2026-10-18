"""Stop Claude before a model request if any tool result remains unsafe."""
import json
import sys


def decision(payload):
    from team_egress_guard import batch_block_reason
    reason = batch_block_reason(payload)
    if not reason:
        return None
    # PostToolBatch uses top-level decision/reason fields. Unlike PostToolUse's
    # updatedToolOutput, these control fields are not nested in hookSpecificOutput.
    return {'decision': 'block',
            'reason': '送信前ガードでツール出力を保留しました（分類: '
                      + reason + '）。結果の内容を再出力せず、利用者に確認してください。'}


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        payload = json.load(sys.stdin)
        result = decision(payload)
        if result:
            print(json.dumps(result, ensure_ascii=False))
    except Exception:
        # Hook configuration sets onFailure=block; never send unchecked output on a broken guard.
        raise SystemExit(2)


if __name__ == '__main__':
    main()
