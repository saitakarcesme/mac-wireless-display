"""Exercise Linux installer policy with a temporary home and mocked systemd."""
import contextlib
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import install_receiver


class ReceiverInstallationTests(unittest.TestCase):
    def test_missing_wayland_session_refuses_before_commands(self):
        with patch.object(install_receiver.sys, "platform", "linux"), \
             patch.dict(install_receiver.os.environ, {}, clear=True), \
             patch.object(install_receiver.subprocess, "run") as run, self.assertRaises(SystemExit):
            install_receiver.main()
        run.assert_not_called()

    def test_active_service_refuses_before_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            with patch.object(install_receiver.sys, "platform", "linux"), \
                 patch.dict(install_receiver.os.environ, {"WAYLAND_DISPLAY": "wayland-test"}, clear=True), \
                 patch.object(install_receiver.shutil, "which", return_value="/mock/command"), \
                 patch.object(install_receiver.Path, "home", return_value=home), \
                 patch.object(install_receiver.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)), \
                 self.assertRaises(SystemExit):
                install_receiver.main()
            self.assertFalse(home.exists())

    def test_inactive_install_keeps_driver_and_only_imports_session_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home with spaces"
            environment = {"WAYLAND_DISPLAY": "custom-wayland", "DISPLAY": ":42",
                           "LIBVA_DRIVER_NAME": "user-driver", "PRIVATE_TOKEN": "generated-test-value"}

            def run(arguments, **kwargs):
                return subprocess.CompletedProcess(arguments, 3 if "is-active" in arguments else 0)

            with patch.object(install_receiver.sys, "platform", "linux"), \
                 patch.dict(install_receiver.os.environ, environment, clear=True), \
                 patch.object(install_receiver.shutil, "which", return_value="/mock/command"), \
                 patch.object(install_receiver.Path, "home", return_value=home), \
                 patch.object(install_receiver.subprocess, "run", side_effect=run) as commands, \
                 contextlib.redirect_stdout(io.StringIO()):
                install_receiver.main()
            calls = [entry.args[0] for entry in commands.call_args_list]
            self.assertIn(["systemctl", "--user", "import-environment", "WAYLAND_DISPLAY", "DISPLAY"], calls)
            self.assertFalse(any("start" in call or "enable" in call for call in calls))
            self.assertFalse(any("PRIVATE_TOKEN" in call or "LIBVA_DRIVER_NAME" in call for call in calls))
            service = (home / ".config/systemd/user/mac-h264-display.service").read_text()
            self.assertIn("--refinement-port 16011", service)
            self.assertNotIn("Environment=", service)
            self.assertTrue((home / ".local/lib/mac-h264-display/run-receiver").stat().st_mode & 0o100)


if __name__ == "__main__":
    unittest.main()
