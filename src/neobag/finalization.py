"""Read-only checks of finalized SQLite recordings, including legacy SIGINT exits.

These checks confirm that SQLite contains the messages claimed by metadata.
They do not scan message payloads or certify transport-level completeness.
"""

from __future__ import annotations

from collections import Counter
from contextlib import closing
from pathlib import Path, PureWindowsPath
import sqlite3

import yaml


_MAX_METADATA_BYTES = 4 * 2**20


def _count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def validate_sqlite_metadata(output: Path) -> str | None:
    """Return an error for unverified SQLite output, or ``None`` when counts match.

    Intended for a worker after the recorder process group has exited. Reading
    ``COUNT(*)`` can take time for a large bag; this must not run on the UI thread.
    No reindex, recovery, write connection, or full payload integrity scan occurs.
    """
    try:
        root = Path(output).resolve(strict=True)
        metadata_path = root / "metadata.yaml"
        if not metadata_path.resolve(strict=True).is_relative_to(root):
            return "metadata.yaml resolves outside the bag directory"
        with metadata_path.open("rb") as stream:
            raw = stream.read(_MAX_METADATA_BYTES + 1)
        if len(raw) > _MAX_METADATA_BYTES:
            return "metadata.yaml exceeds the 4 MiB validation limit"
        metadata = yaml.safe_load(raw)
    except (OSError, ValueError, UnicodeError, yaml.YAMLError, RuntimeError) as exc:
        return f"Cannot read finalized metadata.yaml: {exc}"
    if not isinstance(metadata, dict):
        return "metadata.yaml must contain a bag information mapping"
    information = metadata.get("rosbag2_bagfile_information")
    if not isinstance(information, dict):
        return "metadata.yaml is missing rosbag2_bagfile_information"
    if information.get("storage_identifier") != "sqlite3":
        return "SQLite finalization requires sqlite3 storage metadata"
    expected = information.get("message_count")
    if not _count(expected):
        return "metadata.yaml message_count must be a nonnegative integer"
    relative_paths = information.get("relative_file_paths")
    if not isinstance(relative_paths, list) or not relative_paths:
        return "metadata.yaml relative_file_paths must be a nonempty list"

    expected_topics: dict[str, int] | None = None
    if "topics_with_message_count" in information:
        entries = information["topics_with_message_count"]
        if not isinstance(entries, list):
            return "metadata.yaml topics_with_message_count must be a list"
        expected_topics = {}
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("topic_metadata"), dict):
                return "metadata.yaml contains an invalid topic count entry"
            name = entry["topic_metadata"].get("name")
            count = entry.get("message_count")
            if not isinstance(name, str) or not name or not _count(count):
                return "metadata.yaml topic counts require names and nonnegative integers"
            if name in expected_topics:
                return f"metadata.yaml repeats topic {name}"
            expected_topics[name] = count
        if sum(expected_topics.values()) != expected:
            return "metadata.yaml topic counts do not sum to message_count"

    total = 0
    actual_topics: Counter[str] = Counter()
    seen: set[Path] = set()
    for relative in relative_paths:
        if not isinstance(relative, str) or not relative or "\0" in relative:
            return "metadata.yaml contains an invalid database path"
        path = Path(relative)
        windows_path = PureWindowsPath(relative)
        if path.is_absolute() or windows_path.drive or ".." in path.parts or ".." in windows_path.parts:
            return f"Unsafe database path in metadata.yaml: {relative}"
        try:
            database = (root / path).resolve(strict=True)
            if not database.is_relative_to(root):
                return f"Database path resolves outside the bag directory: {relative}"
            if not database.is_file():
                return f"Bag database is not a regular file: {relative}"
            if database in seen:
                return f"metadata.yaml repeats database path: {relative}"
            seen.add(database)
            # as_uri preserves spaces and URI-significant characters in paths.
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
                for table, required in (
                    ("messages", {"id", "topic_id", "timestamp", "data"}),
                    ("topics", {"id", "name", "type", "serialization_format"}),
                ):
                    columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                    if not required <= columns:
                        return f"Bag database has an invalid {table} schema: {relative}"
                total += connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
                if expected_topics is not None:
                    for name, count in connection.execute(
                        "SELECT topics.name, COUNT(*) FROM messages "
                        "LEFT JOIN topics ON messages.topic_id = topics.id "
                        "GROUP BY topics.name"
                    ):
                        if not isinstance(name, str):
                            return f"Bag database messages reference an unknown topic: {relative}"
                        actual_topics[name] += count
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            return f"Cannot validate bag database {relative}: {exc}"
    if total != expected:
        return f"Bag message count mismatch: metadata declares {expected}, SQLite contains {total}"
    if expected_topics is not None:
        positive_expected = {name: count for name, count in expected_topics.items() if count}
        if dict(actual_topics) != positive_expected:
            return "Bag per-topic message counts do not match metadata.yaml"
    return None
