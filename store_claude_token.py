"""Store the Claude Code long-lived token for 采来's workers in the Windows Credential Manager (2026-10-12).

The user runs this in their own terminal after `claude setup-token`. The token is typed or pasted at a hidden
prompt (nothing is echoed) and written to the generic credential that team_claude_auth reads. It is never printed,
logged or written to a file.

    python store_claude_token.py            # store or replace
    python store_claude_token.py --delete   # remove it (采来 then uses the regular login again)
"""
import ctypes
import getpass
import sys
import warnings
from ctypes import wintypes

from team_claude_auth import TARGET, _Credential, read_stored_token, stored_token_state

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2


def store(token):
    blob = token.encode('utf-16-le')
    if not blob or len(blob) > 5120:
        raise ValueError('資格情報の長さが上限を超えています。保存していません。')
    buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    credential = _Credential()
    credential.Type = CRED_TYPE_GENERIC
    credential.TargetName = TARGET
    credential.CredentialBlobSize = len(blob)
    credential.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
    credential.Persist = CRED_PERSIST_LOCAL_MACHINE
    credential.UserName = 'saikuru'
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    advapi.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
    advapi.CredWriteW.restype = wintypes.BOOL
    if not advapi.CredWriteW(ctypes.byref(credential), 0):
        raise OSError(ctypes.get_last_error(), '資格情報を保存できませんでした。')


def delete():
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    advapi.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    advapi.CredDeleteW.restype = wintypes.BOOL
    if not advapi.CredDeleteW(TARGET, CRED_TYPE_GENERIC, 0) and ctypes.get_last_error() != 1168:   # 1168: not found
        raise OSError(ctypes.get_last_error(), '資格情報を削除できませんでした。')


def main():
    if '--delete' in sys.argv[1:]:
        delete()
        print('采来用の長期の鍵を削除しました。采来は通常のログインを使います。')
        return 0
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            token = getpass.getpass('claude setup-token で表示された鍵を貼り付けて Enter（画面には表示されません）: ').strip()
    except getpass.GetPassWarning:
        print('非表示入力を利用できません。通常の対話ターミナルで実行してください。保存していません。')
        return 1
    if not token.startswith('sk-ant-') or not token.isascii() or any(c.isspace() for c in token) or not 40 <= len(token) <= 2560:
        print('鍵の形が正しくありません（sk-ant- で始まる長い文字列）。保存していません。')
        return 1
    if token.count('sk-ant-') > 1:
        # Seen 2026-10-12: the key was pasted twice into the hidden prompt (nothing is shown, so it is easy to miss).
        print('鍵が2回貼り付けられているようです（sk-ant- が2か所）。1回だけ貼り付けて、もう一度実行してください。保存していません。')
        return 1
    store(token)
    stored = read_stored_token()
    # 2026-10-15: a copy that roams with the domain profile was replaced by an old empty one. Accept only this PC's copy.
    local = stored_token_state().get('persist') == CRED_PERSIST_LOCAL_MACHINE
    if stored == token and local:
        print('保存しました（この PC だけに保存、保存先 ' + TARGET + '）。')
        return 0
    print('保存の確認に失敗しました。もう一度実行してください。' if stored != token else
          '保存しましたが、会社のネットワークで同期される形になっています。管理者にご相談ください。')
    return 1


if __name__ == '__main__':
    sys.exit(main())
