from pathlib import Path

from pyproject_api._util import ensure_empty_dir


def test_ensure_empty_dir_on_empty(tmp_path: Path) -> None:
    ensure_empty_dir(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_ensure_empty_dir_on_path_missing(tmp_path: Path) -> None:
    path = tmp_path / "a"
    ensure_empty_dir(path)
    assert list(path.iterdir()) == []


def test_ensure_empty_dir_on_path_file(tmp_path: Path) -> None:
    path = tmp_path / "a"
    path.write_text("")
    ensure_empty_dir(path)
    assert list(path.iterdir()) == []


def test_ensure_empty_dir_on_path_folder(tmp_path: Path) -> None:
    """
    ├──  a
    │  ├──  a
    │  └──  b
    │     └──  c
    └──  d
    """
    path = tmp_path / "a"
    path.mkdir()
    (path / "a").write_text("")
    sub_dir = path / "b"
    sub_dir.mkdir()
    (sub_dir / "c").write_text("")
    (tmp_path / "d").write_text("")
    ensure_empty_dir(tmp_path)
    assert list(tmp_path.iterdir()) == []
