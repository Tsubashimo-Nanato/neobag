import json

import pytest

from neobag.profiles import BUILTIN, ProfileStore, resolve_profile


def test_builtin_matches_only_left_raw_and_vectornav(tmp_path):
    store = ProfileStore(tmp_path / "profiles.json")
    preset = store.read()["profiles"][BUILTIN]
    assert resolve_profile(preset, ["/cam/left_raw/image_raw_color", "/sensing/vectornav/imu", "/cam/right_raw/image_raw_color", "/vectornavigation/imu"]) == {"/cam/left_raw/image_raw_color", "/sensing/vectornav/imu"}
    assert not store.path.exists()


def test_multiple_profiles_update_and_restart_preserve_offline_topics(tmp_path):
    path = tmp_path / "config" / "profiles.json"
    store = ProfileStore(path)
    store.save("Camera", {"/left/image_raw", "/offline/imu"})
    store.save("导航", {"/imu"})
    store.save("Camera", {"/left/image_raw", "/offline/imu", "/tf"})
    reopened = ProfileStore(path)
    assert reopened.read()["active"] == "Camera"
    assert resolve_profile(reopened.activate("Camera"), []) == {"/left/image_raw", "/offline/imu", "/tf"}
    assert reopened.read()["profiles"]["导航"]["topics"] == ["/imu"]
    assert not list(path.parent.glob(".profiles-*"))


@pytest.mark.parametrize("content", ["bad json", '{"version": 9}', '{"version": 1, "active": "x", "profiles": {"x": {"topics": ["../x"]}}}'])
def test_malformed_existing_file_is_never_overwritten(tmp_path, content):
    path = tmp_path / "profiles.json"
    path.write_text(content)
    with pytest.raises(ValueError):
        ProfileStore(path).save("New", {"/imu"})
    assert path.read_text() == content


def test_replace_failure_preserves_existing_file(tmp_path, monkeypatch):
    store = ProfileStore(tmp_path / "profiles.json")
    store.save("Original", {"/imu"})
    before = store.path.read_bytes()
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr("neobag.profiles.os.replace", fail)
    with pytest.raises(OSError):
        store.save("New", {"/camera"})
    assert store.path.read_bytes() == before
    assert not list(tmp_path.glob(".profiles-*"))


@pytest.mark.parametrize("name,topics", [("", {"/imu"}), ("a\nb", {"/imu"}), ("x", {"relative"})])
def test_invalid_selection_rejected(tmp_path, name, topics):
    with pytest.raises(ValueError):
        ProfileStore(tmp_path / "profiles.json").save(name, topics)
