import json
from pathlib import Path
import tempfile
import unittest

from connection_config import load_connection, validate_destination


class ConnectionConfigurationTests(unittest.TestCase):
    def test_valid_aliases(self):
        for alias in ("display-receiver", "receiver.local", "lab_display-2"):
            self.assertEqual(validate_destination(alias), alias)

    def test_arguments_and_shell_syntax_are_rejected(self):
        for alias in (None, True, 1, "", " ", "-oProxyCommand=bad", "host other", "host\nother",
                      "user@host", "host;command", "$(command)", "host/../../file", "a" * 256):
            with self.subTest(alias=alias), self.assertRaises(ValueError):
                validate_destination(alias)

    def test_invalid_configuration_is_rejected(self):
        payloads = [b"", b"not json", b"[]", b"null", b"{}", b'{"sshDestination":false}',
                    b'{"sshDestination":"good","extra":1}',
                    b'{"sshDestination":"good","sshDestination":"other"}', b"\xff", b" " * 16385]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "connection.json"
            for payload in payloads:
                path.write_bytes(payload)
                with self.subTest(payload=payload[:60]), self.assertRaises(ValueError):
                    load_connection(path)

    def test_valid_file_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "connection.json"
            path.write_text(json.dumps({"sshDestination": "display-receiver"}))
            self.assertEqual(load_connection(path), "display-receiver")


if __name__ == "__main__":
    unittest.main()
