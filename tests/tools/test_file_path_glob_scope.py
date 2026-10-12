"""Directory components remain part of file-name glob scope (#66636)."""
import pytest

from tools.file_operations import ShellFileOperations
from tests.tools.test_file_operations import make_real_subprocess_env


@pytest.mark.parametrize("order", ["discovery", "modified"])
def test_directory_glob_excludes_same_basename_in_other_directory(tmp_path, order):
    wanted = tmp_path / "reference" / "report.txt"
    other = tmp_path / "other" / "report.txt"
    for file in (wanted, other):
        file.parent.mkdir()
        file.write_text("fixture", encoding="utf-8")
    ops = ShellFileOperations(make_real_subprocess_env(str(tmp_path)))
    result = ops._search_files("**/reference/**", str(tmp_path), limit=50, offset=0, order=order)
    assert result.error is None
    assert result.files == [str(wanted)]
    basename = ops._search_files("*.txt", str(tmp_path), limit=50, offset=0, order=order)
    assert basename.error is None
    assert set(basename.files) == {str(wanted), str(other)}


def test_path_glob_without_ripgrep_fails_explicitly(tmp_path, monkeypatch):
    ops = ShellFileOperations(make_real_subprocess_env(str(tmp_path)))
    monkeypatch.setattr(ops, "_has_command", lambda command: command == "find")
    result = ops._search_files("**/reference/**", str(tmp_path), limit=50, offset=0)
    assert result.error is not None
    assert "Path-aware file globs require ripgrep" in result.error
