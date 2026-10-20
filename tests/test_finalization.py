"""Verify ambiguous legacy SIGINT stops using real read-only SQLite bags."""

from pathlib import Path
import os
import sqlite3

import pytest
import yaml

from neobag.finalization import validate_sqlite_metadata


def make_database(path: Path, counts=None):
    counts = counts if counts is not None else {"/image": 2}
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE topics (id INTEGER PRIMARY KEY, name TEXT, type TEXT, serialization_format TEXT);"
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, topic_id INTEGER, timestamp INTEGER, data BLOB);"
        )
        for topic_id, (name, count) in enumerate(counts.items(), 1):
            connection.execute("INSERT INTO topics VALUES (?, ?, 'type', 'cdr')", (topic_id, name))
            connection.executemany(
                "INSERT INTO messages(topic_id, timestamp, data) VALUES (?, ?, ?)",
                ((topic_id, i, b"payload") for i in range(count)),
            )


def write_metadata(output: Path, **changes):
    information = {
        "storage_identifier": "sqlite3",
        "relative_file_paths": ["bag_0.db3"],
        "message_count": 2,
    }
    information.update(changes)
    (output / "metadata.yaml").write_text(yaml.safe_dump({"rosbag2_bagfile_information": information}))


def topic_count(name, count):
    return {"topic_metadata": {"name": name}, "message_count": count}


@pytest.mark.parametrize("count", [0, 2])
def test_matching_finalized_sqlite_bag_including_zero_messages(tmp_path, count):
    make_database(tmp_path / "bag_0.db3", {"/image": count})
    write_metadata(tmp_path, message_count=count)
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    assert validate_sqlite_metadata(tmp_path) is None
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before


def test_split_bag_and_per_topic_counts_include_silent_topics(tmp_path):
    make_database(tmp_path / "bag_0.db3", {"/image": 2, "/cmd_vel": 0})
    make_database(tmp_path / "bag_1.db3", {"/image": 3, "/cmd_vel": 1})
    write_metadata(
        tmp_path, relative_file_paths=["bag_0.db3", "bag_1.db3"], message_count=6,
        topics_with_message_count=[topic_count("/image", 5), topic_count("/cmd_vel", 1), topic_count("/silent", 0)],
    )
    assert validate_sqlite_metadata(tmp_path) is None


def test_uri_significant_characters_in_output_and_database_paths(tmp_path):
    # '#' and spaces need URI escaping on every OS; '?' is POSIX-only.
    question = "?" if os.name != "nt" else ""
    output = tmp_path / f"bag #with {question}spaces"
    output.mkdir()
    make_database(output / f"part #1{question}.db3")
    write_metadata(output, relative_file_paths=[f"part #1{question}.db3"])
    assert validate_sqlite_metadata(output) is None


@pytest.mark.parametrize("content", ["", "[", "[]", "null", "{}", "rosbag2_bagfile_information: []"])
def test_invalid_metadata(tmp_path, content):
    (tmp_path / "metadata.yaml").write_text(content)
    assert validate_sqlite_metadata(tmp_path) is not None


def test_missing_metadata(tmp_path):
    assert "metadata.yaml" in validate_sqlite_metadata(tmp_path)


def test_metadata_read_is_bounded(tmp_path):
    (tmp_path / "metadata.yaml").write_bytes(b" " * (4 * 2**20 + 1))
    assert "limit" in validate_sqlite_metadata(tmp_path)


@pytest.mark.parametrize("count", [None, True, False, -1, 2.0, "2"])
def test_metadata_count_requires_nonnegative_integer(tmp_path, count):
    write_metadata(tmp_path, message_count=count)
    assert "nonnegative integer" in validate_sqlite_metadata(tmp_path)


@pytest.mark.parametrize("storage", [None, "mcap", "sqlite", 3])
def test_only_sqlite_metadata_is_verified(tmp_path, storage):
    write_metadata(tmp_path, storage_identifier=storage)
    assert "SQLite" in validate_sqlite_metadata(tmp_path)


@pytest.mark.parametrize("paths", [[], None, "bag_0.db3", [None], [""], ["bad\0path"]])
def test_invalid_path_list(tmp_path, paths):
    write_metadata(tmp_path, relative_file_paths=paths)
    assert validate_sqlite_metadata(tmp_path) is not None


@pytest.mark.parametrize("path", ["/tmp/outside.db3", "../outside.db3", "part/../bag_0.db3", "C:\\outside.db3", "part\\..\\outside.db3"])
def test_absolute_or_traversal_paths_are_rejected(tmp_path, path):
    write_metadata(tmp_path, relative_file_paths=[path])
    assert "Unsafe" in validate_sqlite_metadata(tmp_path)


def test_symlink_database_escape_is_rejected(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    make_database(tmp_path / "outside.db3")
    make_symlink(output / "bag_0.db3", tmp_path / "outside.db3")
    write_metadata(output)
    assert "outside" in validate_sqlite_metadata(output)


def test_symlink_metadata_escape_is_rejected(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    write_metadata(tmp_path)
    make_symlink(output / "metadata.yaml", tmp_path / "metadata.yaml")
    assert "outside" in validate_sqlite_metadata(output)


def test_duplicate_database_alias_is_rejected(tmp_path):
    make_database(tmp_path / "bag_0.db3")
    make_symlink(tmp_path / "alias.db3", tmp_path / "bag_0.db3")
    write_metadata(tmp_path, relative_file_paths=["bag_0.db3", "alias.db3"], message_count=4)
    assert "repeats" in validate_sqlite_metadata(tmp_path)


def make_symlink(link, target):
    try:
        link.symlink_to(target)
    except OSError as exc:
        if os.name == "nt" and exc.winerror == 1314:
            pytest.skip("Windows symlink privilege is unavailable")
        raise


def test_missing_database_is_not_created(tmp_path):
    write_metadata(tmp_path)
    assert "Cannot validate" in validate_sqlite_metadata(tmp_path)
    assert not (tmp_path / "bag_0.db3").exists()


@pytest.mark.parametrize("kind", ["directory", "not-sqlite", "empty-sqlite", "wrong-schema"])
def test_invalid_database(tmp_path, kind):
    path = tmp_path / "bag_0.db3"
    if kind == "directory":
        path.mkdir()
    elif kind == "not-sqlite":
        path.write_bytes(b"not a SQLite database")
    else:
        with sqlite3.connect(path) as connection:
            if kind == "wrong-schema":
                connection.execute("CREATE TABLE messages (wrong_column TEXT)")
    write_metadata(tmp_path)
    assert validate_sqlite_metadata(tmp_path) is not None


def test_legacy_cache_loss_with_metadata_messages_but_empty_database_fails(tmp_path):
    make_database(tmp_path / "bag_0.db3", {})
    write_metadata(tmp_path, message_count=455)
    assert "metadata declares 455, SQLite contains 0" in validate_sqlite_metadata(tmp_path)


def test_per_topic_mismatch_fails_even_when_aggregate_matches(tmp_path):
    make_database(tmp_path / "bag_0.db3", {"/image": 2})
    write_metadata(tmp_path, topics_with_message_count=[topic_count("/cmd_vel", 2)])
    assert "per-topic" in validate_sqlite_metadata(tmp_path)


@pytest.mark.parametrize("entries", [None, {}, [{}], [topic_count("/image", True)], [topic_count("/image", 1)], [topic_count("/image", 1), topic_count("/image", 1)]])
def test_invalid_per_topic_metadata(tmp_path, entries):
    write_metadata(tmp_path, topics_with_message_count=entries)
    assert validate_sqlite_metadata(tmp_path) is not None


def test_orphan_message_topic_is_rejected(tmp_path):
    make_database(tmp_path / "bag_0.db3")
    with sqlite3.connect(tmp_path / "bag_0.db3") as connection:
        connection.execute("DELETE FROM topics")
    write_metadata(tmp_path, topics_with_message_count=[topic_count("/image", 2)])
    assert "unknown topic" in validate_sqlite_metadata(tmp_path)
