"""Tests for atomic filesystem helpers."""

from unittest.mock import patch

import pytest

from gate.io import atomic_write, atomic_write_bytes


@pytest.mark.parametrize(
    ("writer", "content"),
    [(atomic_write, "hello"), (atomic_write_bytes, b"hello")],
)
def test_atomic_writes_replace_destination(tmp_path, writer, content):
    destination = tmp_path / "nested" / "value.bin"
    writer(destination, content)
    assert destination.read_bytes() == b"hello"
    assert not destination.with_suffix(".bin.tmp").exists()


@pytest.mark.parametrize(
    ("writer", "content"),
    [(atomic_write, "hello"), (atomic_write_bytes, b"hello")],
)
def test_atomic_writes_remove_temporary_file_on_failure(tmp_path, writer, content):
    destination = tmp_path / "value.bin"
    with patch("gate.io.os.replace", side_effect=OSError("replace failed")):
        with pytest.raises(OSError, match="replace failed"):
            writer(destination, content)
    assert not destination.exists()
    assert not destination.with_suffix(".bin.tmp").exists()
