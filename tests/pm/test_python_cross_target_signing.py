"""A macOS host must not codesign a Linux cross-build interpreter."""
import pytest


@pytest.mark.platforms("macos")
def test_python_stage_never_signs_a_linux_target(monkeypatch, tmp_path):
    from pm.registry import get_package
    from pm.store import Store

    staged = tmp_path / "staged"
    binary = staged / "bin/python3"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"Linux interpreter fixture")
    signed = []
    monkeypatch.setattr("hermes_cli.macos_signing.sign_managed_python", lambda path: signed.append(path))
    get_package("python").stage(Store(tmp_path / "store"), staged, "3.14.7", "linux-x64")
    assert signed == []
    assert binary.read_bytes() == b"Linux interpreter fixture"
