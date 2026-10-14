import subprocess
import unittest
from unittest.mock import patch

from provider_auth import AuthService, failure_kind


class AuthTests(unittest.TestCase):
    def test_status_and_redaction(self):
        cases = [
            ('Codex', 0, '', 'Logged in using ChatGPT', 'ready'),
            ('Codex', 1, '', 'Not logged in', 'sign_in_required'),
            ('Claude', 1, '{"loggedIn":false}', '', 'sign_in_required'),
            ('Claude', 0, '{"loggedIn":true,"email":"private@example.com","token":"secret"}', '', 'ready'),
            ('Codex', 1, '', 'Could not find home directory', 'environment'),
            ('Claude', 1, '', 'Network connection failed', 'error'),
        ]
        for provider, code, stdout, stderr, expected in cases:
            with self.subTest(provider=provider,expected=expected), patch('provider_auth.command_for', return_value='cli'), patch(
                'provider_auth.subprocess.run', return_value=subprocess.CompletedProcess([],code,stdout,stderr)
            ):
                state=AuthService().status(provider)
                self.assertEqual(state['state'],expected)
                self.assertNotIn('private@example.com',str(state))
                self.assertNotIn('secret',str(state))

    def test_missing_cli_and_timeout_are_not_login_errors(self):
        for error, expected in [(FileNotFoundError(), 'not_installed'),
                                (subprocess.TimeoutExpired('cli',15), 'error')]:
            with patch('provider_auth.subprocess.run', side_effect=error):
                self.assertEqual(AuthService().status('Codex')['state'],expected)

    def test_error_classification(self):
        for text in ('not logged in', 'authentication_error', 'refresh_token_reused', 'HTTP 401', 'Access token has expired'):
            self.assertEqual(failure_kind(text),'sign_in_required')
        for text in ('DNS resolution failed', 'Permission denied', '403 Forbidden'):
            self.assertEqual(failure_kind(text),'error')
        self.assertEqual(failure_kind('Usage limit exceeded'),'usage_limit')
        for message in ('Reconnecting... 5/5 (unexpected status 503 Service Unavailable)',
                        'HTTP 502', 'stream disconnected before completion', 'request timed out'):
            self.assertEqual(failure_kind(message), 'provider_transient')
        self.assertEqual(failure_kind('Reconnecting... 401 Unauthorized'), 'sign_in_required')
        self.assertEqual(failure_kind('Reconnecting... 429 rate limit'), 'usage_limit')
        self.assertEqual(failure_kind('Test assertion: 503 != 200'), 'error')

    def test_login_is_explicit_single_and_output_is_not_exposed(self):
        service=AuthService()
        with patch.object(service,'status',return_value={'provider':'Codex','state':'sign_in_required'}), patch(
            'provider_auth.LoginRunner'
        ) as runner, patch('provider_auth.threading.Timer'):
            service.login('Codex')
            service.login('Codex')
            runner.assert_called_once()
            runner.return_value.start.assert_called_once()
            service.cancel('Codex')
            runner.return_value.stop.assert_called_once()
        service.finished('Codex',1)
        self.assertEqual(service.status('Codex')['state'],'sign_in_required')
        self.assertFalse(service.logins)


if __name__ == '__main__':
    unittest.main()
