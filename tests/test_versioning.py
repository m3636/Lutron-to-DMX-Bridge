"""The version has one source (lutron_dmx.__version__) and a helper to bump it."""

import importlib.util
import re
from pathlib import Path

import pytest

import lutron_dmx

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("bump_version", ROOT / "tools" / "bump_version.py")
bump = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bump)


def test_version_is_plain_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", lutron_dmx.__version__)


def test_package_metadata_reads_the_same_version():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'dynamic = ["version"]' in pyproject and "lutron_dmx.__version__" in pyproject


@pytest.mark.parametrize("step,expected", [
    ("patch", "1.4.4"), ("minor", "1.5.0"), ("major", "2.0.0"), ("0.9.1", "0.9.1")])
def test_next_version(step, expected):
    assert bump.next_version((1, 4, 3), step) == expected


def test_next_version_rejects_junk():
    with pytest.raises(SystemExit):
        bump.next_version((1, 0, 0), "bigger")


def _run_bump(monkeypatch, tmp_path, current, arg):
    init = tmp_path / "__init__.py"
    init.write_text(f'"""doc"""\n\n__version__ = "{current}"\n', encoding="utf-8")
    monkeypatch.setattr(bump, "INIT", init)
    bump.main([arg])
    return init.read_text(encoding="utf-8")


def test_bump_rewrites_only_the_version_line(monkeypatch, tmp_path):
    assert _run_bump(monkeypatch, tmp_path, "0.2.0", "minor") == '"""doc"""\n\n__version__ = "0.3.0"\n'


@pytest.mark.parametrize("arg", ["0.2.0", "0.1.9"])
def test_bump_refuses_a_version_that_is_not_newer(monkeypatch, tmp_path, arg):
    with pytest.raises(SystemExit, match="not newer"):
        _run_bump(monkeypatch, tmp_path, "0.2.0", arg)
