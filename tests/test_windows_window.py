import os
import pathlib
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import agent_relay_app
import windows_window


class WindowsRuntimeTests(unittest.TestCase):
    def test_installed_webview_does_not_run_setup(self):
        with patch.object(windows_window, "runtime_installed", return_value=True), \
             patch.object(windows_window, "native_message") as message, \
             patch.object(windows_window.subprocess, "run") as install:
            windows_window.ensure_runtime(pathlib.Path("unneeded.exe"))
            message.assert_not_called()
            install.assert_not_called()

    def test_setup_is_opt_in_and_runtime_is_checked_again(self):
        with tempfile.TemporaryDirectory() as folder:
            bootstrapper = pathlib.Path(folder) / "MicrosoftEdgeWebview2Setup.exe"
            bootstrapper.touch()
            with patch.object(windows_window, "runtime_installed", side_effect=[False, True]), \
                 patch.object(windows_window, "native_message", return_value=6), \
                 patch.object(windows_window.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as install:
                windows_window.ensure_runtime(bootstrapper)
                self.assertEqual(install.call_args.args[0], [str(bootstrapper), "/install"])
            with patch.object(windows_window, "runtime_installed", return_value=False), \
                 patch.object(windows_window, "native_message", return_value=7), \
                 patch.object(windows_window.subprocess, "run") as install:
                with self.assertRaisesRegex(RuntimeError, "canceled"):
                    windows_window.ensure_runtime(bootstrapper)
                install.assert_not_called()

    def test_windows_native_failure_does_not_open_browser(self):
        server = MagicMock(server_port=8792)
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(agent_relay_app.sys, "platform", "win32"), \
             patch.object(agent_relay_app.server, "setup", return_value=(server, SimpleNamespace(host="127.0.0.1", open=False))), \
             patch.object(agent_relay_app.threading, "Thread"), \
             patch.object(agent_relay_app, "run_window", return_value=False), \
             patch("webbrowser.open") as browser:
            self.assertEqual(agent_relay_app.main(["--port", "8792"]), 1)
            browser.assert_not_called()
