"""SQLite payload comparison tests; no ROS runtime or publishers required."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "compare_bags.py"
SPEC = importlib.util.spec_from_file_location("compare_bags_script", SCRIPT)
compare_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(compare_module)
compare_bags = compare_module.compare_bags
BagComparisonError = compare_module.BagComparisonError


def write_database(path, topics, messages):
    """topics=(id,name,type), messages=(topic_id,timestamp,bytes)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as database:
        database.execute("CREATE TABLE topics (id INTEGER PRIMARY KEY, name TEXT, type TEXT, serialization_format TEXT)")
        database.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, topic_id INTEGER, timestamp INTEGER, data BLOB)")
        database.executemany("INSERT INTO topics VALUES (?, ?, ?, 'cdr')", topics)
        database.executemany("INSERT INTO messages (topic_id, timestamp, data) VALUES (?, ?, ?)", messages)


def make_bag(path, payloads, topic="/image", message_type="sensor_msgs/msg/Image"):
    write_database(path / "bag_0.db3", [(1, topic, message_type)], [(1, index, payload) for index, payload in enumerate(payloads)])
    return path


def test_receipt_timestamps_and_message_order_do_not_change_payload_match(tmp_path):
    source = make_bag(tmp_path / "source", [b"one", b"two", b"one"])
    destination = make_bag(tmp_path / "destination", [b"one", b"one", b"two"])
    with sqlite3.connect(destination / "bag_0.db3") as database:
        database.execute("UPDATE messages SET timestamp = timestamp + 123456789")
    before = {path: (path.stat().st_size, path.stat().st_mtime_ns) for path in tmp_path.rglob("*") if path.is_file()}
    report = compare_bags(source, destination)
    assert report["exact_payload_match"] is True
    assert report["source_count"] == report["destination_count"] == 3
    assert report["missing_count"] == report["extra_count"] == 0
    assert before == {path: (path.stat().st_size, path.stat().st_mtime_ns) for path in tmp_path.rglob("*") if path.is_file()}


def test_duplicate_multiplicity_detects_missing_message(tmp_path):
    source = make_bag(tmp_path / "source", [b"same", b"same", b"same"])
    destination = make_bag(tmp_path / "destination", [b"same"])
    report = compare_bags(source, destination)
    assert not report["exact_payload_match"]
    assert report["missing_count"] == 2
    assert report["extra_count"] == 0
    details = report["topics"]["/image"]
    assert details["distinct_missing_payloads"] == 1
    assert details["types"]["sensor_msgs/msg/Image"]["missing_payloads_sample"][0]["count"] == 2


def test_same_counts_with_corrupted_payload_fail(tmp_path):
    source = make_bag(tmp_path / "source", [b"correct", b"duplicate", b"duplicate"])
    destination = make_bag(tmp_path / "destination", [b"wrong", b"wrong", b"duplicate"])
    report = compare_bags(source, destination)
    assert report["source_count"] == report["destination_count"]
    assert not report["exact_payload_match"]
    assert report["missing_count"] == report["extra_count"] == 2
    assert report["topics"]["/image"]["distinct_missing_payloads"] == 2
    assert report["topics"]["/image"]["distinct_extra_payloads"] == 1


def test_identical_payload_wrong_type_fails(tmp_path):
    source = make_bag(tmp_path / "source", [b"same"])
    destination = make_bag(tmp_path / "destination", [b"same"], message_type="std_msgs/msg/String")
    report = compare_bags(source, destination)
    assert not report["exact_payload_match"]
    assert report["missing_count"] == report["extra_count"] == 1
    assert not report["topics"]["/image"]["type_match"]


def test_split_database_metadata_order_and_topic_ids_do_not_matter(tmp_path):
    source = tmp_path / "source"
    write_database(source / "second.db3", [(8, "/image", "sensor_msgs/msg/Image")], [(8, 10, b"two")])
    write_database(source / "first.db3", [(3, "/image", "sensor_msgs/msg/Image")], [(3, 20, b"one")])
    # This unrelated database must be excluded by metadata enumeration.
    write_database(source / "leftover.db3", [(1, "/unrelated", "std_msgs/msg/String")], [(1, 0, b"ignore")])
    (source / "metadata.yaml").write_text(
        "rosbag2_bagfile_information:\n  relative_file_paths:\n    - 'second.db3'\n    - \"first.db3\"\n  duration:\n    nanoseconds: 100\n"
    )
    destination = make_bag(tmp_path / "destination", [b"one", b"two"])
    report = compare_bags(source, destination)
    assert report["exact_payload_match"]
    assert report["source"]["databases"] == ["second.db3", "first.db3"]
    assert report["source"]["database_enumeration"] == "metadata.relative_file_paths"
    assert report["destination"]["database_enumeration"] == "sorted_db3_glob"


def test_selected_topics_report_generated_system_topics_but_exclude_from_match(tmp_path):
    source = make_bag(tmp_path / "source", [b"frame"])
    destination = tmp_path / "destination"
    write_database(destination / "bag.db3", [
        (1, "/image", "sensor_msgs/msg/Image"),
        (2, "/rosout", "rcl_interfaces/msg/Log"),
        (3, "/parameter_events", "rcl_interfaces/msg/ParameterEvent"),
    ], [(1, 10, b"frame"), (2, 20, b"recorder log")])
    assert not compare_bags(source, destination)["exact_payload_match"]
    report = compare_bags(source, destination, ["/image"])
    assert report["exact_payload_match"]
    assert report["destination"]["excluded_topics"] == {"/parameter_events": 0, "/rosout": 1}
    assert report["destination"]["metadata_only_topics"] == ["/parameter_events"]
    assert report["destination_count"] == 1
    assert report["destination"]["message_count_all"] == 2


def test_unknown_requested_topic_does_not_vacuously_pass(tmp_path):
    source = make_bag(tmp_path / "source", [b"frame"])
    destination = make_bag(tmp_path / "destination", [b"frame"])
    report = compare_bags(source, destination, ["/not_present"])
    assert not report["exact_payload_match"]
    assert not report["topics"]["/not_present"]["type_match"]


def test_metadata_only_matching_topic_is_compared_as_zero_messages(tmp_path):
    source = make_bag(tmp_path / "source", [])
    destination = make_bag(tmp_path / "destination", [])
    report = compare_bags(source, destination)
    assert report["exact_payload_match"]
    assert report["source_count"] == 0
    assert report["source"]["metadata_only_topics"] == ["/image"]


@pytest.mark.parametrize("entry", ["missing.db3", "../outside.db3", "bag.mcap"])
def test_invalid_metadata_database_reference_fails(tmp_path, entry):
    source = make_bag(tmp_path / "source", [])
    (source / "metadata.yaml").write_text(f"rosbag2_bagfile_information:\n  relative_file_paths:\n    - {entry}\n")
    with pytest.raises(BagComparisonError):
        compare_bags(source, source)


def test_nonempty_wal_is_rejected_instead_of_ignored(tmp_path):
    source = make_bag(tmp_path / "source", [b"frame"])
    (source / "bag_0.db3-wal").write_bytes(b"pending writes")
    with pytest.raises(BagComparisonError, match="Uncheckpointed WAL"):
        compare_bags(source, source)


def test_orphan_message_topic_id_is_error(tmp_path):
    source = make_bag(tmp_path / "source", [b"frame"])
    with sqlite3.connect(source / "bag_0.db3") as database:
        database.execute("UPDATE messages SET topic_id = 100")
    with pytest.raises(BagComparisonError, match="unknown topic ID"):
        compare_bags(source, source)


def test_cli_json_exit_status_and_external_report_file(tmp_path):
    source = make_bag(tmp_path / "source", [b"frame", b"frame"])
    destination = make_bag(tmp_path / "destination", [b"frame"])
    output = tmp_path / "reports" / "comparison.json"
    result = subprocess.run([sys.executable, str(SCRIPT), str(source), str(destination), "--output", str(output), "--topics", "/image"], capture_output=True, text=True)
    assert result.returncode == 1
    assert json.loads(result.stdout)["missing_count"] == 1
    assert json.loads(output.read_text(encoding="utf-8")) == json.loads(result.stdout)
    result = subprocess.run([sys.executable, str(SCRIPT), str(source), str(source)], capture_output=True, text=True)
    assert result.returncode == 0
    assert json.loads(result.stdout)["exact_payload_match"]


def test_cli_report_cannot_modify_bag_files(tmp_path, capsys):
    source = make_bag(tmp_path / "source", [b"frame"])
    database = source / "bag_0.db3"
    before = database.read_bytes()
    assert compare_module.main([str(source), str(source), "--output", str(database)]) == 2
    assert "outside both bag directories" in capsys.readouterr().out
    assert database.read_bytes() == before


def test_sqlite_to_mcap_duplicates_and_corruption(tmp_path):
    from mcap.writer import Writer
    source = make_bag(tmp_path / "source", [b"same", b"same", b"other"])
    dest = tmp_path / "dest"
    dest.mkdir()
    path = dest / "recorded.mcap"
    with path.open("wb") as stream:
        writer = Writer(stream)
        writer.start(profile="ros2")
        schema = writer.register_schema("sensor_msgs/msg/Image", "ros2msg", b"uint8[] data")
        channel = writer.register_channel("/image", "cdr", schema)
        for index, payload in enumerate([b"other", b"same", b"same"]):
            writer.add_message(channel, index, payload, index)
        writer.finish()
    assert compare_bags(source, dest)["exact_payload_match"]
    with path.open("r+b") as stream:
        stream.truncate(30)
    with pytest.raises(BagComparisonError):
        compare_bags(source, dest)
