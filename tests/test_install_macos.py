import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import install_macos


class InstallationTests(unittest.TestCase):
    def fixture(self, directory):
        root, home = Path(directory) / "source", Path(directory) / "user home"
        for path in (root / "build/Wireless Display.app", root / "build/Wireless Display Host.app",
                     root / "menu", root / "config"):
            path.mkdir(parents=True)
        for name in ("h264-control.py", "connection_config.py"):
            (root / "menu" / name).write_text("# fixture\n")
        for name in ("h264-settings", "display-settings"):
            (root / "config" / (name + ".example.json")).write_text("{}\n")
        return root, home

    def test_wrapper_quotes_paths_and_forwards_arguments(self):
        with tempfile.TemporaryDirectory(prefix="display's test ") as directory:
            root = Path(directory)
            script = root / "helper's code.py"
            script.write_text("import json,sys; print(json.dumps(sys.argv[1:]))")
            wrapper = root / "run"
            wrapper.write_text(install_macos.helper_wrapper(sys.executable, script))
            result = subprocess.run(["/bin/sh", str(wrapper), "a b", "$(false)", "'quoted'"],
                                    capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout), ["a b", "$(false)", "'quoted'"])

    def test_existing_settings_preserved_and_binaries_backed_up(self):
        with tempfile.TemporaryDirectory() as directory:
            root, home = self.fixture(directory)
            base = home / ".config/mac-wireless-display";base.mkdir(parents=True)
            configuration = '{"sshDestination":"existing-alias"}\n'
            (base / "connection.json").write_text(configuration)
            (base / "h264-settings.json").write_text('{"bitrate":1234567}\n')
            app = home / "Applications/Wireless Display.app";app.mkdir(parents=True)
            (app / "old-marker").write_text("preserve me")
            with patch.object(install_macos, "active_apps", return_value=[]), \
                 patch.object(install_macos, "require_python", return_value=sys.executable):
                backup = install_macos.install(root, home)
            self.assertEqual((base / "connection.json").read_text(), configuration)
            self.assertEqual((base / "h264-settings.json").read_text(), '{"bitrate":1234567}\n')
            self.assertTrue(any(backup.glob("*-Wireless Display.app/old-marker")))
            self.assertTrue((base / "h264-control").is_file())

    def test_active_app_refuses_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root, home = self.fixture(directory)
            with patch.object(install_macos, "active_apps", return_value=["WirelessDisplayHost"]), \
                 patch.object(install_macos, "require_python", return_value=sys.executable), self.assertRaises(ValueError):
                install_macos.install(root, home, ssh_host="receiver")
            self.assertFalse(home.exists())

    def test_invalid_alias_refuses_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root, home = self.fixture(directory)
            with patch.object(install_macos, "active_apps", return_value=[]), \
                 patch.object(install_macos, "require_python", return_value=sys.executable), self.assertRaises(ValueError):
                install_macos.install(root, home, ssh_host="-oProxyCommand=bad")
            self.assertFalse(home.exists())

    def test_existing_alias_is_not_silently_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            root, home = self.fixture(directory)
            base = home / ".config/mac-wireless-display";base.mkdir(parents=True)
            path = base / "connection.json";path.write_text('{"sshDestination":"original"}')
            with patch.object(install_macos, "active_apps", return_value=[]), \
                 patch.object(install_macos, "require_python", return_value=sys.executable), self.assertRaises(ValueError):
                install_macos.install(root, home, ssh_host="different")
            self.assertEqual(json.loads(path.read_text())["sshDestination"], "original")

    def test_invalid_default_settings_fail_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root, home = self.fixture(directory)
            (root / "config/display-settings.example.json").write_text("invalid JSON")
            with patch.object(install_macos, "active_apps", return_value=[]), \
                 patch.object(install_macos, "require_python", return_value=sys.executable), self.assertRaises(ValueError):
                install_macos.install(root, home, ssh_host="receiver")
            self.assertFalse(home.exists())

    def test_failed_replacement_rolls_back_existing_apps(self):
        with tempfile.TemporaryDirectory() as directory:
            root, home = self.fixture(directory)
            app = home / "Applications/Wireless Display.app"
            host = home / ".config/mac-wireless-display/build/Wireless Display Host.app"
            for target in (app, host):
                target.mkdir(parents=True)
                (target / "original").write_text("original app")
            real_move = install_macos.shutil.move
            injected = False

            def fail_once(source, destination, *args, **kwargs):
                nonlocal injected
                if Path(destination) == host and Path(source).name == "1" and not injected:
                    injected = True
                    raise OSError("Generated failure while committing staged host")
                return real_move(source, destination, *args, **kwargs)

            with patch.object(install_macos, "active_apps", return_value=[]), \
                 patch.object(install_macos, "require_python", return_value=sys.executable), \
                 patch.object(install_macos.shutil, "move", side_effect=fail_once), self.assertRaises(OSError):
                install_macos.install(root, home, ssh_host="receiver")
            self.assertTrue(injected)
            for target in (app, host):
                self.assertEqual((target / "original").read_text(), "original app")
            self.assertFalse((home / ".config/mac-wireless-display/connection.json").exists())

    def test_first_install_creates_private_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root, home = self.fixture(directory)
            with patch.object(install_macos, "active_apps", return_value=[]), \
                 patch.object(install_macos, "require_python", return_value=sys.executable):
                install_macos.install(root, home, ssh_host="receiver")
            base = home / ".config/mac-wireless-display"
            self.assertEqual(json.loads((base / "connection.json").read_text()), {"sshDestination": "receiver"})
            for name in ("connection", "h264-settings", "display-settings"):
                self.assertEqual((base / (name + ".json")).stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
