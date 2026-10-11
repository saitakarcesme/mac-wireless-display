"""Validate the local SSH alias without interpreting it as command-line options."""
import json
import re
from pathlib import Path

_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}\Z", re.ASCII)
MAX_CONFIG_BYTES = 16 * 1024


def validate_destination(value):
    if not isinstance(value, str) or not _ALIAS.fullmatch(value):
        raise ValueError("sshDestination must be a nonempty SSH alias using letters, digits, dots, underscores or hyphens")
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate connection setting: " + key)
        result[key] = value
    return result


def load_connection(path):
    try:
        with Path(path).open("rb") as stream:
            data = stream.read(MAX_CONFIG_BYTES + 1)
        if len(data) > MAX_CONFIG_BYTES:
            raise ValueError("connection.json exceeds 16 KiB")
        settings = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Cannot read a valid connection.json: " + str(exc)) from exc
    if not isinstance(settings, dict) or set(settings) != {"sshDestination"}:
        raise ValueError("connection.json must contain only sshDestination")
    return validate_destination(settings["sshDestination"])
