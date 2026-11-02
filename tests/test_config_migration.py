"""The rename from "Lutron DMX" to "Lutron To DMX Bridge" must not lose anyone's settings."""

import json
from pathlib import Path

import pytest

from lutron_to_dmx_bridge import config


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.delenv("LUTRON_TO_DMX_BRIDGE_HOME", raising=False)
    monkeypatch.delenv(config.LEGACY_HOME_ENV, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def _new_and_old(home: Path) -> tuple[Path, Path]:
    return config._default_dir("LutronToDMXBridge", "lutron-to-dmx-bridge"), config._default_dir("LutronDMX", "lutron-dmx")


def test_fresh_install_uses_new_folder(fake_home):
    new, old = _new_and_old(fake_home)
    assert config.data_dir() == new and new.is_dir() and not old.exists()
    assert new.name.lower().replace("-", "") == "lutrontodmxbridge"


def test_old_folder_is_moved_and_saved_paths_follow(fake_home):
    new, old = _new_and_old(fake_home)
    (old / "backups").mkdir(parents=True)
    (old / "backups" / "b.json").write_text("{}")
    cfg = {"driver": {"kind": "leap", "host": "10.0.0.5", "leap_cert_dir": str(old / "certs")}, "scenes": []}
    (old / "config.json").write_text(json.dumps(cfg))
    assert config.data_dir() == new
    assert not old.exists()
    assert (new / "backups" / "b.json").exists()
    saved = json.loads((new / "config.json").read_text())
    assert saved["driver"]["leap_cert_dir"] == str(new / "certs")
    assert saved["driver"]["host"] == "10.0.0.5"
    # and a second start does nothing more
    assert config.data_dir() == new


def test_existing_new_folder_wins_and_old_is_left_alone(fake_home):
    new, old = _new_and_old(fake_home)
    old.mkdir(parents=True)
    (old / "config.json").write_text('{"x": 1}')
    new.mkdir(parents=True)
    (new / "config.json").write_text('{"x": 2}')
    assert config.data_dir() == new
    assert json.loads((new / "config.json").read_text()) == {"x": 2}
    assert (old / "config.json").exists()


def test_old_environment_variable_still_works(fake_home, tmp_path, monkeypatch):
    monkeypatch.setenv(config.LEGACY_HOME_ENV, str(tmp_path / "legacy-env"))
    assert config.data_dir() == tmp_path / "legacy-env"
    monkeypatch.setenv("LUTRON_TO_DMX_BRIDGE_HOME", str(tmp_path / "new-env"))
    assert config.data_dir() == tmp_path / "new-env"
