"""User-owned named topic selections, independent of the installed release."""
from __future__ import annotations

import fnmatch
import json
import os
from pathlib import Path
import re
import tempfile


BUILTIN = "Left raw + VectorNav"
PATTERNS = ("*/left_raw/*", "*/vectornav/*")
TOPIC = re.compile(r"/(?:[A-Za-z_][A-Za-z0-9_]*)(?:/[A-Za-z_][A-Za-z0-9_]*)*")


def default_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    if not base.is_absolute():
        base = Path.home() / ".config"
    return base / "neobag" / "profiles.json"


class ProfileStore:
    def __init__(self, path: Path | None = None):
        self.path = path or default_path()

    def read(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "active": BUILTIN, "profiles": {BUILTIN: {"topics": [], "patterns": list(PATTERNS)}}}
        if self.path.stat().st_size > 1024 * 1024:
            raise ValueError("Profile file exceeds 1 MiB; existing file was preserved")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1 or not isinstance(data.get("profiles"), dict):
            raise ValueError("Unsupported profile file; existing file was preserved")
        if len(data["profiles"]) > 128:
            raise ValueError("Too many profiles (maximum 128)")
        for name, profile in data["profiles"].items():
            self.validate_name(name)
            if not isinstance(profile, dict):
                raise ValueError("Invalid profile")
            self.validate_topics(profile.get("topics"))
            patterns = profile.get("patterns", [])
            if not isinstance(patterns, list) or (patterns and (name != BUILTIN or patterns != list(PATTERNS))):
                raise ValueError("Unsupported topic matching rules")
        if not isinstance(data.get("active"), str) or data["active"] not in data["profiles"]:
            raise ValueError("Active profile does not exist")
        return data

    @staticmethod
    def validate_name(name: str) -> None:
        if not isinstance(name, str) or not name.strip() or name != name.strip() or len(name) > 64 or any(ord(c) < 32 for c in name):
            raise ValueError("Use a profile name of 1–64 characters without control characters")

    @staticmethod
    def validate_topics(topics) -> None:
        if not isinstance(topics, list) or len(topics) > 10000 or any(not isinstance(t, str) or not TOPIC.fullmatch(t) for t in topics):
            raise ValueError("Profile topics must be fully qualified ROS names (maximum 10000)")

    def save(self, name: str, topics: set[str]) -> None:
        name = name.strip()
        self.validate_name(name)
        self.validate_topics(sorted(topics))
        data = self.read()  # Never replace a malformed existing file.
        if name not in data["profiles"] and len(data["profiles"]) >= 128:
            raise ValueError("Maximum 128 profiles")
        data["profiles"][name] = {"topics": sorted(topics)}
        data["active"] = name
        self._write(data)

    def activate(self, name: str) -> dict:
        data = self.read()
        profile = data["profiles"][name]
        data["active"] = name
        self._write(data)
        return profile

    def _write(self, data: dict) -> None:
        payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        if len(payload.encode("utf-8")) > 1024 * 1024:
            raise ValueError("Profile file would exceed 1 MiB")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, prefix=".profiles-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def resolve_profile(profile: dict, live: list[str]) -> set[str]:
    return set(profile["topics"]) | {name for name in live if any(fnmatch.fnmatchcase(name, rule) for rule in profile.get("patterns", []))}
