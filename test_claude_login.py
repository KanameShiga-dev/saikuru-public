"""2026-10-09: 采来 opens the Claude sign-in URL from the CLI output with this PC's browser; nothing else is opened
and the URL is not kept in the state shown to the screen."""
import io
import json
import unittest

from team_claude_login import ClaudeLogin, login_url


class LoginUrlTest(unittest.TestCase):
    def test_sign_in_hosts_only(self):
        good = 'https://claude.com/cai/oauth/authorize?code=true&client_id=x&state=y'
        self.assertEqual(login_url("If the browser didn't open, visit: " + good), good)
        self.assertEqual(login_url('https://claude.ai/oauth/authorize?x=1'), 'https://claude.ai/oauth/authorize?x=1')
        self.assertTrue(login_url('https://console.anthropic.com/oauth/authorize?x=1'))
        for bad in ('http://claude.com/oauth', 'https://evil-claude.com/x', 'https://claude.com.evil.test/x',
                    'https://user:pw@claude.com/x', 'https://claude.com:8443/x', 'file:///C:/x', 'Paste code here if prompted >'):
            with self.subTest(bad=bad):
                self.assertIsNone(login_url(bad))


class WatchOutputTest(unittest.TestCase):
    def test_first_url_is_opened_once_and_not_kept(self):
        opened = []
        login = ClaudeLogin(app=None, opener=opened.append)
        url = 'https://claude.com/cai/oauth/authorize?state=secret-state'
        output = io.BytesIO(('Opening browser to sign in…\nIf the browser didn\'t open, visit: ' + url + '\n'
                             'https://claude.com/other\nPaste code here if prompted >').encode('utf-8'))
        login._watch_output(output)
        self.assertEqual(opened, [url])
        snapshot = login.snapshot()
        self.assertTrue(snapshot['browser_opened'])
        self.assertNotIn('secret-state', json.dumps(snapshot))
        self.assertNotIn('secret-state', json.dumps(vars(login), default=str))

    def test_open_failure_is_reported(self):
        def fail(url):
            raise OSError('no browser')
        login = ClaudeLogin(app=None, opener=fail)
        login._watch_output(io.BytesIO(b'visit: https://claude.com/oauth?x=1\n'))
        self.assertFalse(login.snapshot()['browser_opened'])

    def test_other_output_opens_nothing(self):
        opened = []
        login = ClaudeLogin(app=None, opener=opened.append)
        login._watch_output(io.BytesIO(b'visit: https://example.com/oauth\nerror\n'))
        self.assertEqual(opened, [])
        self.assertIsNone(login.snapshot()['browser_opened'])


if __name__ == '__main__':
    unittest.main()
