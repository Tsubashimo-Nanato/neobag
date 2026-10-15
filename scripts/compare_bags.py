#!/usr/bin/env python3
"""Compare finalized SQLite/MCAP ROS 2 bags without importing ROS or modifying bags.

Payload multiplicity is compared per topic name and message type. Receipt
timestamps, message ordering, QoS and recording frequency are intentionally not
compared: replay records new receipt timestamps and may reorder callbacks.
Only one serialized message is held at a time; counters retain its SHA256 hash.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any


class BagComparisonError(RuntimeError):
    """A bag cannot be read reliably as finalized storage."""


def _yaml_scalar(value: str) -> str:
    """Read the simple filename scalars emitted by rosbag metadata writers."""
    value = value.strip()
    if value.startswith('"'):
        try:
            result = json.loads(value)
        except json.JSONDecodeError as exc:
            raise BagComparisonError("Unsupported quoted filename in metadata.yaml") from exc
        if not isinstance(result, str):
            raise BagComparisonError("Metadata filename must be a string")
        return result
    if value.startswith("'"):
        if not value.endswith("'"):
            raise BagComparisonError("Unterminated quoted filename in metadata.yaml")
        return value[1:-1].replace("''", "'")
    value = value.split(" #", 1)[0].rstrip()
    if not value or value[0] in "&*!|>{[":
        raise BagComparisonError("Unsupported filename syntax in metadata.yaml")
    return value


def _metadata_paths(metadata: Path) -> list[str] | None:
    """Parse ROS's block list, without a general YAML dependency.

    A missing key returns None for the documented *.db3 fallback. A present but
    unsupported list fails closed so split files cannot be silently omitted.
    """
    lines = metadata.read_text(encoding="utf-8-sig").splitlines()
    for index, line in enumerate(lines):
        match = re.fullmatch(r"( *)relative_file_paths:\s*(.*?)\s*", line)
        if match is None:
            continue
        indent, tail = len(match[1]), match[2]
        if tail == "[]":
            return []
        if tail and not tail.startswith("#"):
            # JSON-style arrays are valid YAML, and occasionally hand-written.
            try:
                value = json.loads(tail)
            except json.JSONDecodeError as exc:
                raise BagComparisonError("Use ROS block-list relative_file_paths metadata") from exc
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise BagComparisonError("relative_file_paths must be a list of filenames")
            return value
        result: list[str] = []
        for item in lines[index + 1:]:
            if not item.strip() or item.lstrip().startswith("#"):
                continue
            item_indent = len(item) - len(item.lstrip(" "))
            entry = re.fullmatch(r" *-\s+(.*)", item)
            if item_indent >= indent and entry:
                result.append(_yaml_scalar(entry[1]))
            elif item_indent <= indent:
                break
            else:
                raise BagComparisonError("Unsupported relative_file_paths metadata entry")
        return result
    return None


def _database_files(directory: Path) -> tuple[Path, list[Path], str]:
    directory = Path(directory).expanduser().resolve()
    if not directory.is_dir():
        raise BagComparisonError(f"Bag directory does not exist: {directory}")
    metadata = directory / "metadata.yaml"
    filenames = _metadata_paths(metadata) if metadata.is_file() else None
    method = "metadata.relative_file_paths" if filenames is not None else "sorted_db3_glob"
    candidates = [directory / name for name in filenames] if filenames is not None else sorted([*directory.glob("*.db3"), *directory.glob("*.mcap")])
    if not candidates:
        raise BagComparisonError(f"No SQLite .db3 files found in bag: {directory}")
    files: list[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve()
        if not resolved.is_relative_to(directory):
            raise BagComparisonError(f"Metadata path leaves the bag directory: {candidate}")
        if resolved.suffix.lower() not in (".db3", ".mcap"):
            raise BagComparisonError(f"Only SQLite .db3 and MCAP storage are supported: {candidate.name}")
        if not resolved.is_file():
            raise BagComparisonError(f"A bag database listed in metadata is missing: {candidate}")
        if resolved in files:
            raise BagComparisonError(f"Database appears twice in metadata: {candidate.name}")
        wal = Path(str(resolved) + "-wal")
        if wal.exists() and wal.stat().st_size:
            raise BagComparisonError(
                f"Uncheckpointed WAL found: {wal}. Stop/finalize the writer before comparing."
            )
        files.append(resolved)
    return directory, files, method


def _read_mcap(path, selected, counts, formats, hashes):
    try:
        from mcap.reader import NonSeekingReader
        from mcap.exceptions import McapError
    except ImportError as exc:
        raise BagComparisonError("MCAP comparison requires pip install 'mcap>=1.2,<2'") from exc
    try:
        with path.open("rb") as stream:
            # Streaming order avoids retaining the whole bag for timestamp sorting.
            reader = NonSeekingReader(stream, validate_crcs=True)
            for schema, channel, message in reader.iter_messages(log_time_order=False):
                if schema is None or not schema.name or not channel.topic or channel.message_encoding != "cdr":
                    raise BagComparisonError(f"Invalid ROS MCAP schema/channel in {path}")
                name, message_type = channel.topic, schema.name
                counts[name][message_type] += 1
                formats[name][message_type].add(channel.message_encoding)
                if selected is None or name in selected:
                    hashes[name][message_type][hashlib.sha256(message.data).hexdigest()] += 1
    except (McapError, EOFError) as exc:
        raise BagComparisonError(f"Cannot read finalized MCAP {path}: {exc}") from exc


def _read_bag(directory: Path, selected: set[str] | None) -> dict[str, Any]:
    directory, files, enumeration = _database_files(directory)
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    formats: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    hashes: dict[str, dict[str, Counter[str]]] = defaultdict(lambda: defaultdict(Counter))
    for database in files:
        before = database.stat()
        if database.suffix.lower() == ".mcap":
            _read_mcap(database, selected, counts, formats, hashes)
            after = database.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise BagComparisonError(f"Bag changed during comparison: {database}")
            continue
        try:
            # immutable=1 prevents SQLite from creating journal/shared-memory
            # sidecars. Nonempty WAL is rejected above rather than ignored.
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
                connection.execute("PRAGMA query_only = ON")
                entries = connection.execute(
                    "SELECT id, name, type, serialization_format FROM topics"
                ).fetchall()
                identities: dict[int, tuple[str, str]] = {}
                for topic_id, name, message_type, serialization in entries:
                    if not all(isinstance(v, str) and v for v in (name, message_type, serialization)):
                        raise BagComparisonError(f"Invalid topic metadata in {database}")
                    identities[topic_id] = (name, message_type)
                    counts[name][message_type] += 0
                    formats[name][message_type].add(serialization)
                for topic_id, count in connection.execute(
                    "SELECT topic_id, COUNT(*) FROM messages GROUP BY topic_id"
                ):
                    if topic_id not in identities:
                        raise BagComparisonError(f"Messages refer to unknown topic ID {topic_id} in {database}")
                    name, message_type = identities[topic_id]
                    counts[name][message_type] += count
                wanted_ids = [key for key, (name, _) in identities.items() if selected is None or name in selected]
                # Keep bind counts below old SQLite builds' 999-variable limit.
                for offset in range(0, len(wanted_ids), 900):
                    batch = wanted_ids[offset:offset + 900]
                    placeholders = ",".join("?" for _ in batch)
                    cursor = connection.execute(
                        f"SELECT topic_id, data FROM messages WHERE topic_id IN ({placeholders})", batch
                    )
                    for topic_id, payload in cursor:
                        if not isinstance(payload, bytes):
                            raise BagComparisonError(f"Non-BLOB message payload in {database}")
                        name, message_type = identities[topic_id]
                        hashes[name][message_type][hashlib.sha256(payload).hexdigest()] += 1
        except sqlite3.Error as exc:
            raise BagComparisonError(f"Cannot read ROS SQLite database {database}: {exc}") from exc
        after = database.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise BagComparisonError(f"Bag database changed during comparison: {database}")
    return {
        "path": str(directory),
        "databases": [str(path.relative_to(directory)) for path in files],
        "database_enumeration": enumeration,
        "counts": counts,
        "formats": formats,
        "hashes": hashes,
    }


def _bag_summary(bag: dict[str, Any], selected: set[str]) -> dict[str, Any]:
    totals = {name: sum(types.values()) for name, types in bag["counts"].items()}
    return {
        key: bag[key] for key in ("path", "databases", "database_enumeration")
    } | {
        "message_count_all": sum(totals.values()),
        "topic_count_all": len(totals),
        "metadata_only_topics": sorted(name for name, count in totals.items() if count == 0),
        "excluded_topics": {name: count for name, count in sorted(totals.items()) if name not in selected},
    }


def _samples(counter: Counter[str], limit: int = 20) -> list[dict[str, Any]]:
    # A report remains manageable even if every frame differs.
    return [{"sha256": digest, "count": counter[digest]} for digest in sorted(counter)[:limit]]


def compare_bags(source: Path, destination: Path, topics: list[str] | None = None) -> dict[str, Any]:
    """Return payload/count differences; optional topics scope pass/fail exactly.

    No topics are implicitly ignored, including /rosout and /parameter_events.
    Selected topics absent from either bag fail even when both counts are zero.
    Duplicate serialized messages retain their full multiplicity.
    """
    if topics is not None and (not topics or any(not isinstance(t, str) or not t for t in topics)):
        raise ValueError("topics must be a nonempty list of topic names, or None")
    selected = set(topics) if topics is not None else None
    source_bag = _read_bag(source, selected)
    destination_bag = _read_bag(destination, selected)
    if selected is None:
        selected = set(source_bag["counts"]) | set(destination_bag["counts"])
    topic_reports: dict[str, Any] = {}
    for name in sorted(selected):
        source_types = source_bag["counts"].get(name, {})
        destination_types = destination_bag["counts"].get(name, {})
        type_reports: dict[str, Any] = {}
        for message_type in sorted(source_types.keys() | destination_types.keys()):
            source_hashes = source_bag["hashes"].get(name, {}).get(message_type, Counter())
            destination_hashes = destination_bag["hashes"].get(name, {}).get(message_type, Counter())
            missing = source_hashes - destination_hashes
            extra = destination_hashes - source_hashes
            source_formats = source_bag["formats"].get(name, {}).get(message_type, set())
            destination_formats = destination_bag["formats"].get(name, {}).get(message_type, set())
            type_reports[message_type] = {
                "source_count": source_types.get(message_type, 0),
                "destination_count": destination_types.get(message_type, 0),
                "source_serialization_formats": sorted(source_formats),
                "destination_serialization_formats": sorted(destination_formats),
                "source_distinct_payloads": len(source_hashes),
                "destination_distinct_payloads": len(destination_hashes),
                "missing_count": sum(missing.values()),
                "extra_count": sum(extra.values()),
                "distinct_missing_payloads": len(missing),
                "distinct_extra_payloads": len(extra),
                "missing_payloads_sample": _samples(missing),
                "extra_payloads_sample": _samples(extra),
                "exact_payload_match": not missing and not extra and source_formats == destination_formats,
            }
        type_match = bool(source_types) and source_types.keys() == destination_types.keys()
        topic_reports[name] = {
            "source_types": sorted(source_types),
            "destination_types": sorted(destination_types),
            "type_match": type_match,
            "source_count": sum(source_types.values()),
            "destination_count": sum(destination_types.values()),
            "missing_count": sum(report["missing_count"] for report in type_reports.values()),
            "extra_count": sum(report["extra_count"] for report in type_reports.values()),
            "distinct_missing_payloads": sum(report["distinct_missing_payloads"] for report in type_reports.values()),
            "distinct_extra_payloads": sum(report["distinct_extra_payloads"] for report in type_reports.values()),
            "exact_payload_match": type_match and all(report["exact_payload_match"] for report in type_reports.values()),
            "types": type_reports,
        }
    return {
        "schema_version": 1,
        "comparison": "sha256_serialized_payload_multiset_per_topic_and_type",
        "source": _bag_summary(source_bag, selected),
        "destination": _bag_summary(destination_bag, selected),
        "selected_topics": sorted(selected),
        "scope": "explicit_topics" if topics is not None else "all_topics",
        "source_count": sum(report["source_count"] for report in topic_reports.values()),
        "destination_count": sum(report["destination_count"] for report in topic_reports.values()),
        "missing_count": sum(report["missing_count"] for report in topic_reports.values()),
        "extra_count": sum(report["extra_count"] for report in topic_reports.values()),
        "exact_payload_match": bool(topic_reports) and all(report["exact_payload_match"] for report in topic_reports.values()),
        "topics": topic_reports,
        "limitations": [
            "Receipt timestamps and message order are not compared.",
            "Payload equality does not establish live recording rate, latency, QoS equality or hardware acceptance.",
            "Finalized SQLite .db3 and ROS MCAP bags are supported; nonempty WAL sidecars are rejected.",
            "Missing/extra SHA256 samples are limited to 20 distinct hashes per topic and type; counts are complete.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--output", type=Path, help="Write the JSON report to this file as well as stdout")
    parser.add_argument("--topics", nargs="+", help="Compare only these exact topic names; report all excluded topic counts")
    args = parser.parse_args(argv)
    try:
        # Never permit a report path to overwrite anything inside either bag.
        if args.output is not None:
            report_path = args.output.expanduser().resolve()
            if any(report_path.is_relative_to(path.expanduser().resolve()) for path in (args.source, args.destination)):
                raise BagComparisonError("Report output must be outside both bag directories")
        report = compare_bags(args.source, args.destination, args.topics)
        text = json.dumps(report, indent=2, ensure_ascii=False)
        if args.output is not None:
            args.output.expanduser().parent.mkdir(parents=True, exist_ok=True)
            args.output.expanduser().write_text(text + "\n", encoding="utf-8")
        print(text)
        return 0 if report["exact_payload_match"] else 1
    except (BagComparisonError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc), "exact_payload_match": False}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
