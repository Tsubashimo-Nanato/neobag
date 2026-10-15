from types import SimpleNamespace

import pytest

from neobag import settings


def test_defaults_prefer_installed_plugins_and_never_mutate_environment(monkeypatch):
    environment = {"ROS_DOMAIN_ID": "219", "ROS_LOCALHOST_ONLY": "1"}
    monkeypatch.setattr(settings, "os", SimpleNamespace(name="posix", environ=environment))
    monkeypatch.setattr(settings, "package_available", lambda cmd, package: True)
    monkeypatch.setattr(settings.psutil, "virtual_memory", lambda: SimpleNamespace(total=16 * 2**30, available=8 * 2**30))
    storage, child, cache = settings.resolve_settings(("ros2",), "auto", "auto", None)
    assert storage == "mcap"
    assert child == environment | {"RMW_IMPLEMENTATION": "rmw_cyclonedds_cpp", "CYCLONEDDS_URI": settings.CYCLONE_RECEIVER_CONFIG}
    assert "RMW_IMPLEMENTATION" not in environment
    assert cache == 512 * 2**20


@pytest.mark.parametrize("explicit", [{"RMW_IMPLEMENTATION": "rmw_fastrtps_cpp"}, {"FASTDDS_DEFAULT_PROFILES_FILE": "/config.xml"}])
def test_auto_honors_explicit_dds_configuration(monkeypatch, explicit):
    monkeypatch.setattr(settings, "os", SimpleNamespace(name="posix", environ=explicit))
    def unexpected_probe(*args):
        raise AssertionError("No RMW probe should be made")
    monkeypatch.setattr(settings, "package_available", unexpected_probe)
    storage, child, cache = settings.resolve_settings(("ros2",), "sqlite3", "auto", 64)
    assert child == explicit
    assert cache == 64 * 2**20


def test_unavailable_plugins_keep_ros_environment(monkeypatch):
    monkeypatch.setattr(settings, "os", SimpleNamespace(name="posix", environ={}))
    monkeypatch.setattr(settings, "package_available", lambda *args: False)
    storage, child, _ = settings.resolve_settings(("ros2",), "auto", "auto", None)
    assert storage == "sqlite3"
    assert "RMW_IMPLEMENTATION" not in child


def test_cache_respects_available_and_total_ram(monkeypatch):
    monkeypatch.setattr(settings.psutil, "virtual_memory", lambda: SimpleNamespace(total=4 * 2**30, available=128 * 2**20))
    assert settings.cache_bytes() == 32 * 2**20


@pytest.mark.parametrize("value", [0, -1, 8193, True, 1.5])
def test_invalid_cache_size(value):
    with pytest.raises(ValueError):
        settings.cache_bytes(value)


@pytest.mark.parametrize("choice,expected", [("cyclonedds", "rmw_cyclonedds_cpp"), ("fastrtps", "rmw_fastrtps_cpp"), ("inherit", "rmw_custom_cpp")])
def test_rmw_explicit_choice(monkeypatch, choice, expected):
    monkeypatch.setenv("RMW_IMPLEMENTATION", "rmw_custom_cpp")
    _, env, _ = settings.resolve_settings(("ros2",), "sqlite3", choice, 128)
    assert env["RMW_IMPLEMENTATION"] == expected


def test_user_cyclone_configuration_is_never_replaced(monkeypatch):
    monkeypatch.setenv("CYCLONEDDS_URI", "/my/cyclone.xml")
    _, env, _ = settings.resolve_settings(("ros2",), "mcap", "cyclonedds", 128)
    assert env["CYCLONEDDS_URI"] == "/my/cyclone.xml"
