from __future__ import annotations

from pathlib import Path

from src.matrix_client.sync_token import (
    is_invalid_sync_token_error,
    load_gomatrix_sync_token,
    normalize_gomatrix_sync_token,
    save_gomatrix_sync_token,
)


def test_gomatrix_sync_token_helpers(tmp_path: Path) -> None:
    assert normalize_gomatrix_sync_token("s63") == "s63"
    assert normalize_gomatrix_sync_token("42648") is None
    assert is_invalid_sync_token_error("Unknown error: invalid sync token")

    stale = tmp_path / "matrix_sync_token_cli"
    stale.write_text("42648", encoding="utf-8")
    assert load_gomatrix_sync_token(stale) is None
    assert not stale.exists()

    path = tmp_path / "token"
    save_gomatrix_sync_token(path, "s99")
    assert path.read_text(encoding="utf-8") == "s99"
    save_gomatrix_sync_token(path, "bad")
    assert path.read_text(encoding="utf-8") == "s99"
