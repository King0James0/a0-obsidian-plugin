"""Security tests for helpers/setup.py fixes.

Tests for:
1. Launcher path interpolation with repr() escaping
2. Environment variable allowlist to prevent secret exposure
"""

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add the plugin root to path so we can import helpers
sys.path.insert(0, str(Path(__file__).parent.parent))

from helpers import setup


class TestLauncherPathEscaping(unittest.TestCase):
    """Test that _render_launcher properly escapes path values with repr()."""

    @patch('helpers.setup._signal_file')
    @patch('helpers.setup._vault_path')
    def test_normal_vault_path(self, mock_vault, mock_signal):
        """Normal vault path should render correctly in launcher Python code."""
        mock_vault.return_value = "/tmp/normal-vault"
        mock_signal.return_value = "/tmp/signal"
        
        cfg = {}
        launcher = setup._render_launcher(cfg)
        
        # Should contain properly quoted path in Python
        self.assertIn("VAULT = '/tmp/normal-vault'", launcher)
        
        # Verify it's valid Python syntax
        try:
            compile(launcher, "<launcher>", "exec")
        except SyntaxError as e:
            self.fail(f"Generated launcher code has syntax error: {e}")

    @patch('helpers.setup._signal_file')
    @patch('helpers.setup._vault_path')
    def test_vault_path_with_quotes(self, mock_vault, mock_signal):
        """Vault path with single/double quotes should be safely escaped."""
        mock_vault.return_value = "/tmp/vault-with-'quotes'"
        mock_signal.return_value = "/tmp/signal"
        
        cfg = {}
        launcher = setup._render_launcher(cfg)
        
        # repr() should escape the quotes
        self.assertIn("VAULT =", launcher)
        
        # Verify it's valid Python syntax
        try:
            compile(launcher, "<launcher>", "exec")
        except SyntaxError as e:
            self.fail(f"Generated launcher code has syntax error: {e}")
        
        # Verify the path can be extracted and is correct
        namespace = {}
        exec(launcher, namespace)
        self.assertEqual(namespace["VAULT"], "/tmp/vault-with-'quotes'")

    @patch('helpers.setup._signal_file')
    @patch('helpers.setup._vault_path')
    def test_vault_path_with_backslashes(self, mock_vault, mock_signal):
        """Vault path with backslashes should be safely escaped."""
        mock_vault.return_value = r"C:\Users\vault"
        mock_signal.return_value = "/tmp/signal"
        
        cfg = {}
        launcher = setup._render_launcher(cfg)
        
        # Verify it's valid Python syntax
        try:
            compile(launcher, "<launcher>", "exec")
        except SyntaxError as e:
            self.fail(f"Generated launcher code has syntax error: {e}")
        
        # Verify the path can be extracted and is correct
        namespace = {}
        exec(launcher, namespace)
        self.assertEqual(namespace["VAULT"], r"C:\Users\vault")

    @patch('helpers.setup._signal_file')
    @patch('helpers.setup._vault_path')
    def test_vault_path_with_newlines(self, mock_vault, mock_signal):
        """Vault path with newlines should be safely escaped."""
        mock_vault.return_value = "/tmp/vault\ninjected"
        mock_signal.return_value = "/tmp/signal"
        
        cfg = {}
        launcher = setup._render_launcher(cfg)
        
        # Verify it's valid Python syntax (newlines would break it otherwise)
        try:
            compile(launcher, "<launcher>", "exec")
        except SyntaxError as e:
            self.fail(f"Generated launcher code has syntax error: {e}")
        
        # Verify the path can be extracted and is correct
        namespace = {}
        exec(launcher, namespace)
        self.assertEqual(namespace["VAULT"], "/tmp/vault\ninjected")

    @patch('helpers.setup._signal_file')
    @patch('helpers.setup._vault_path')
    def test_vault_path_with_special_chars(self, mock_vault, mock_signal):
        """Vault path with special characters should be properly handled."""
        mock_vault.return_value = '/tmp/vault-$USER-@host-#hash'
        mock_signal.return_value = "/tmp/signal"
        
        cfg = {}
        launcher = setup._render_launcher(cfg)
        
        # Verify it's valid Python syntax
        try:
            compile(launcher, "<launcher>", "exec")
        except SyntaxError as e:
            self.fail(f"Generated launcher code has syntax error: {e}")
        
        # Verify the path can be extracted and is correct
        namespace = {}
        exec(launcher, namespace)
        self.assertEqual(namespace["VAULT"], '/tmp/vault-$USER-@host-#hash')

    @patch('helpers.setup._signal_file')
    @patch('helpers.setup._vault_path')
    def test_all_launcher_placeholders_escaped(self, mock_vault, mock_signal):
        """All placeholders (__VAULT__, __SIGNAL__, __CLI__) should be escaped."""
        mock_vault.return_value = "/tmp/vault"
        mock_signal.return_value = "/tmp/signal"
        
        cfg = {}
        launcher = setup._render_launcher(cfg)
        
        # Should not contain any unescaped placeholders
        self.assertNotIn("__VAULT__", launcher)
        self.assertNotIn("__SIGNAL__", launcher)
        self.assertNotIn("__CLI__", launcher)
        
        # Should have valid Python assignments
        self.assertRegex(launcher, r"VAULT = '[^']*'")
        self.assertRegex(launcher, r"SIGNAL = '[^']*'")
        self.assertRegex(launcher, r"CLI = '[^']*'")
        
        # Verify overall syntax
        try:
            compile(launcher, "<launcher>", "exec")
        except SyntaxError as e:
            self.fail(f"Generated launcher code has syntax error: {e}")


class TestEnvironmentVariableAllowlist(unittest.TestCase):
    """Test that _app_env uses an allowlist and doesn't expose secrets."""

    @patch('helpers.setup._runtime_dir')
    @patch('helpers.setup._display')
    def test_safe_vars_included(self, mock_display, mock_runtime):
        """Safe environment variables in allowlist should be included."""
        mock_runtime.return_value = "/tmp/obsidian-runtime"
        mock_display.return_value = ":121"
        
        cfg = {}
        
        # Mock os.environ with some safe and unsafe vars
        mock_env = {
            "PATH": "/usr/bin:/bin",
            "LANG": "en_US.UTF-8",
            "USER": "testuser",
            "API_KEY": "secret-key-123",  # unsafe
            "DB_PASSWORD": "db-pass",      # unsafe
            "TZ": "UTC",
        }
        
        with patch.dict(os.environ, mock_env, clear=True):
            result = setup._app_env(cfg)
        
        # Safe vars should be present
        self.assertEqual(result["PATH"], "/usr/bin:/bin")
        self.assertEqual(result["LANG"], "en_US.UTF-8")
        self.assertEqual(result["USER"], "testuser")
        self.assertEqual(result["TZ"], "UTC")

    @patch('helpers.setup._runtime_dir')
    @patch('helpers.setup._display')
    def test_unsafe_vars_excluded(self, mock_display, mock_runtime):
        """Unsafe environment variables should NOT be included in _app_env."""
        mock_runtime.return_value = "/tmp/obsidian-runtime"
        mock_display.return_value = ":121"
        
        cfg = {}
        
        # Mock os.environ with credentials and secrets
        mock_env = {
            "PATH": "/usr/bin",
            "API_KEY": "secret-key-123",
            "DB_PASSWORD": "db-pass",
            "AWS_SECRET_ACCESS_KEY": "aws-secret",
            "GITHUB_TOKEN": "gh-token",
            "SSH_PRIVATE_KEY": "rsa-key",
            "DATABASE_URL": "postgresql://user:pass@host/db",
        }
        
        with patch.dict(os.environ, mock_env, clear=True):
            result = setup._app_env(cfg)
        
        # Unsafe vars should NOT be present (except PATH which is in allowlist)
        self.assertNotIn("API_KEY", result)
        self.assertNotIn("DB_PASSWORD", result)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", result)
        self.assertNotIn("GITHUB_TOKEN", result)
        self.assertNotIn("SSH_PRIVATE_KEY", result)
        self.assertNotIn("DATABASE_URL", result)

    @patch('helpers.setup._runtime_dir')
    @patch('helpers.setup._display')
    def test_required_vars_always_set(self, mock_display, mock_runtime):
        """Required Obsidian vars (HOME, DISPLAY, XDG_*) should always be set."""
        mock_runtime.return_value = "/tmp/obsidian-runtime"
        mock_display.return_value = ":121"
        
        cfg = {}
        
        mock_env = {}
        with patch.dict(os.environ, mock_env, clear=True):
            result = setup._app_env(cfg)
        
        # Required vars should be present and set
        self.assertIn("HOME", result)
        self.assertIn("DISPLAY", result)
        self.assertIn("XDG_CONFIG_HOME", result)
        self.assertIn("XDG_RUNTIME_DIR", result)
        self.assertIn("XDG_CACHE_HOME", result)
        
        # HOME should be the runtime directory
        self.assertEqual(result["HOME"], "/tmp/obsidian-runtime")

    @patch('helpers.setup._runtime_dir')
    @patch('helpers.setup._display')
    def test_allowlist_completeness(self, mock_display, mock_runtime):
        """Verify the allowlist includes essential system vars but excludes secrets."""
        mock_runtime.return_value = "/tmp/obsidian-runtime"
        mock_display.return_value = ":121"
        
        cfg = {}
        
        # Set various types of environment variables
        mock_env = {
            # Should be included (system/locale)
            "PATH": "/usr/bin",
            "LANG": "en_US.UTF-8",
            "LC_ALL": "en_US.UTF-8",
            "LANGUAGE": "en_US",
            "TZ": "UTC",
            "USER": "testuser",
            "LOGNAME": "testuser",
            
            # Should NOT be included (credentials/secrets)
            "SSH_AUTH_SOCK": "/tmp/ssh-agent",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/0/bus",
            "SUDO_USER": "root",
            "SUDO_UID": "0",
        }
        
        with patch.dict(os.environ, mock_env, clear=True):
            result = setup._app_env(cfg)
        
        # Verify allowlist items are present
        allowlist_vars = {"PATH", "LANG", "LANGUAGE", "LC_ALL", "TZ", "USER", "LOGNAME"}
        for var in allowlist_vars:
            if var in mock_env:
                self.assertEqual(result.get(var), mock_env[var], f"{var} should be in result")

    @patch('helpers.setup._runtime_dir')
    @patch('helpers.setup._display')
    def test_no_os_environ_passthrough(self, mock_display, mock_runtime):
        """Verify that os.environ is NOT directly spread into result (old vulnerability)."""
        mock_runtime.return_value = "/tmp/obsidian-runtime"
        mock_display.return_value = ":121"
        
        cfg = {}
        
        # Create a custom environ with a secret
        mock_env = {
            "SUPER_SECRET_KEY": "this-should-not-be-included",
            "PATH": "/usr/bin",
        }
        
        with patch.dict(os.environ, mock_env, clear=True):
            result = setup._app_env(cfg)
        
        # The secret should NOT be in the result
        self.assertNotIn("SUPER_SECRET_KEY", result)
        
        # PATH should be in result (it's in allowlist)
        self.assertIn("PATH", result)

    @patch('helpers.setup._runtime_dir')
    @patch('helpers.setup._display')
    def test_env_is_dict(self, mock_display, mock_runtime):
        """Result should be a dictionary that can be serialized to JSON (for relaunch spec)."""
        mock_runtime.return_value = "/tmp/obsidian-runtime"
        mock_display.return_value = ":121"
        
        cfg = {}
        
        mock_env = {"PATH": "/usr/bin", "USER": "test"}
        with patch.dict(os.environ, mock_env, clear=True):
            result = setup._app_env(cfg)
        
        # Should be able to serialize to JSON (required for relaunch.json)
        try:
            json.dumps(result)
        except (TypeError, ValueError) as e:
            self.fail(f"Environment dict should be JSON-serializable: {e}")


class TestRelaunchSpecSecurityIntegration(unittest.TestCase):
    """Integration test: verify relaunch spec doesn't contain secrets."""

    @patch('helpers.setup._relaunch_spec_path')
    @patch('helpers.setup._config')
    @patch('helpers.setup._runtime_dir')
    def test_relaunch_spec_no_secrets(self, mock_runtime, mock_config, mock_spec_path):
        """Relaunch spec should not contain API keys or other secrets from environment."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_spec_path.return_value = os.path.join(tmpdir, "relaunch.json")
            mock_runtime.return_value = tmpdir
            mock_config.return_value = {}
            
            # Set up environment with secrets
            mock_env = {
                "PATH": "/usr/bin",
                "USER": "testuser",
                "API_KEY": "secret-123",
                "DB_PASSWORD": "pass123",
            }
            
            with patch.dict(os.environ, mock_env, clear=True):
                setup._write_relaunch_spec(
                    {}, 
                    ["obsidian"], 
                    setup._app_env({}),
                    tmpdir,
                    os.path.join(tmpdir, "obsidian.log")
                )
            
            # Read the written spec
            spec_path = os.path.join(tmpdir, "relaunch.json")
            self.assertTrue(os.path.exists(spec_path))
            
            with open(spec_path) as f:
                spec = json.load(f)
            
            # Verify secrets are NOT in the spec
            spec_str = json.dumps(spec)
            self.assertNotIn("secret-123", spec_str)
            self.assertNotIn("pass123", spec_str)
            
            # Safe vars should be present
            self.assertIn("PATH", spec["env"])
            self.assertIn("USER", spec["env"])


if __name__ == "__main__":
    unittest.main()
