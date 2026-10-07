import hashlib
import os
import pathlib
import sqlite3
from datetime import datetime, timezone

import pytest
from click.testing import CliRunner

from sqlite_utils import Database, cli, sync_files


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_tree(root: pathlib.Path, files: dict[str, str]) -> None:
    for path, content in files.items():
        full = root / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, "utf-8")


def history_rows(db: Database, table: str = "files"):
    return list(
        db.execute(
            f"SELECT event, path, old_hash, new_hash, size FROM "
            f"{table}_history ORDER BY id"
        )
    )


def current_paths(db: Database, table: str = "files") -> set[str]:
    return {row[0] for row in db.execute(f"SELECT path FROM {table}")}


def content_hashes(db: Database, table: str = "files") -> set[str]:
    return {row[0] for row in db.execute(f"SELECT content_hash FROM {table}_content")}


def content_for(db: Database, path: str, table: str = "files") -> bytes:
    row = db.execute(
        f"SELECT c.content FROM {table} f "
        f"JOIN {table}_content c ON f.content_hash = c.content_hash "
        "WHERE f.path = ?",
        [path],
    ).fetchone()
    assert row is not None
    return row[0]


def add_manual_row(db: Database, path: str, data: bytes, table: str = "files") -> str:
    """Insert a row exactly as a user working outside the sync would."""
    digest = sha256(data)
    db.execute(
        f"INSERT OR IGNORE INTO {table}_content VALUES (?, ?, ?)",
        [digest, data, len(data)],
    )
    db.execute(
        f"INSERT INTO {table} (path, content_hash, size, mtime, managed_by, "
        "synced_at) VALUES (?, ?, ?, ?, NULL, ?)",
        [path, digest, len(data), None, "2026-01-01T00:00:00+00:00"],
    )
    return digest


def test_first_sync_creates_schema_and_rows(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    write_tree(root, {"a.txt": "alpha", "nested/b.txt": "bravo"})
    db = Database(memory=True)

    result = sync_files(db, root)

    assert (result.added, result.updated, result.deleted) == (2, 0, 0)
    assert result.unchanged == 0
    assert result.contents_added == 2
    assert result.contents_deleted == 0
    assert {"files", "files_content", "files_history"} <= set(db.table_names())
    assert current_paths(db) == {"a.txt", "nested/b.txt"}
    assert content_for(db, "a.txt") == b"alpha"
    # Rows are tagged as managed by the resolved source directory
    tags = {row[0] for row in db.execute("SELECT managed_by FROM files")}
    assert tags == {str(root.resolve())}
    assert [(row[0], row[1], row[2], row[3]) for row in history_rows(db)] == [
        ("insert", "a.txt", None, sha256(b"alpha")),
        ("insert", "nested/b.txt", None, sha256(b"bravo")),
    ]


def test_identical_content_is_stored_once(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    write_tree(root, {"a.txt": "same", "other/b.txt": "same", "c.txt": "different"})
    db = Database(memory=True)

    result = sync_files(db, root)

    assert result.added == 3
    assert result.contents_added == 2
    assert content_hashes(db) == {sha256(b"same"), sha256(b"different")}
    hashes_by_path = dict(db.execute("SELECT path, content_hash FROM files").fetchall())
    # The two "same" files share one stored BLOB
    assert hashes_by_path["a.txt"] == hashes_by_path["other/b.txt"]
    assert hashes_by_path["c.txt"] != hashes_by_path["a.txt"]


def test_update_records_history_and_garbage_collects_old_blob(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    write_tree(root, {"a.txt": "one"})
    db = Database(memory=True)
    sync_files(db, root)
    old_hash = sha256(b"one")
    assert content_for(db, "a.txt") == b"one"

    (root / "a.txt").write_text("two", "utf-8")
    result = sync_files(db, root)

    assert (result.added, result.updated, result.deleted) == (0, 1, 0)
    assert result.unchanged == 0  # the only file changed
    assert result.contents_added == 1
    assert result.contents_deleted == 1
    assert content_for(db, "a.txt") == b"two"
    assert content_hashes(db) == {sha256(b"two")}
    assert history_rows(db)[-1] == (
        "update",
        "a.txt",
        old_hash,
        sha256(b"two"),
        3,
    )


def test_delete_records_history_and_removes_blob(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    write_tree(root, {"a.txt": "one", "b.txt": "two"})
    db = Database(memory=True)
    sync_files(db, root)
    old_hash = sha256(b"one")

    (root / "a.txt").unlink()
    result = sync_files(db, root)

    assert (result.added, result.updated, result.deleted) == (0, 0, 1)
    assert result.unchanged == 1
    assert result.contents_deleted == 1
    assert current_paths(db) == {"b.txt"}
    assert content_hashes(db) == {sha256(b"two")}
    # The final state of the deleted file survives in history
    assert history_rows(db)[-1] == ("delete", "a.txt", old_hash, None, 3)


def test_content_kept_until_last_reference_disappears(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    write_tree(root, {"a.txt": "shared", "b.txt": "shared"})
    db = Database(memory=True)
    sync_files(db, root)
    shared_hash = sha256(b"shared")
    assert content_hashes(db) == {shared_hash}

    # First reference disappears: the BLOB must stay
    (root / "a.txt").unlink()
    result = sync_files(db, root)
    assert result.deleted == 1
    assert result.contents_deleted == 0
    assert content_hashes(db) == {shared_hash}
    assert content_for(db, "b.txt") == b"shared"

    # Last reference disappears: now the BLOB is collected
    (root / "b.txt").unlink()
    result = sync_files(db, root)
    assert result.deleted == 1
    assert result.contents_deleted == 1
    assert content_hashes(db) == set()
    delete_events = [r for r in history_rows(db) if r[0] == "delete"]
    assert [r[1] for r in delete_events] == ["a.txt", "b.txt"]
    assert all(r[2] == shared_hash for r in delete_events)


def test_update_into_existing_content_reuses_blob(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    write_tree(root, {"a.txt": "xxx", "b.txt": "yyy"})
    db = Database(memory=True)
    sync_files(db, root)

    # b.txt changes to content a.txt already stores
    (root / "b.txt").write_text("xxx", "utf-8")
    result = sync_files(db, root)

    assert result.updated == 1
    assert result.contents_added == 0  # no new BLOB
    assert result.contents_deleted == 1  # orphaned "yyy" BLOB collected
    assert content_hashes(db) == {sha256(b"xxx")}


def test_unchanged_rerun_is_a_noop(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    write_tree(root, {"a.txt": "one"})
    db = Database(memory=True)
    first = sync_files(db, root)
    assert first.changed

    statements = []
    db._tracer = lambda sql, params: statements.append(sql.lstrip().upper())

    second = sync_files(db, root)

    assert (second.added, second.updated, second.deleted) == (0, 0, 0)
    assert second.unchanged == 1
    assert (second.contents_added, second.contents_deleted) == (0, 0)
    assert not second.changed
    # No new history rows and no writes on an unchanged run
    assert len(history_rows(db)) == 1
    assert not any(s.startswith(("INSERT", "UPDATE", "DELETE")) for s in statements)


def test_mtime_only_change_is_not_a_change_event(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    target = root / "a.txt"
    target.write_text("one", "utf-8")
    db = Database(memory=True)
    sync_files(db, root)
    before = db.execute("SELECT mtime, synced_at FROM files").fetchone()

    # Same bytes, different mtime
    os.utime(target, (1_000_000_000, 1_000_000_000))
    result = sync_files(db, root)

    assert (result.added, result.updated, result.deleted) == (0, 0, 0)
    assert len(history_rows(db)) == 1  # the original insert only
    after = db.execute("SELECT mtime, synced_at FROM files").fetchone()
    assert after == before  # untouched rows are not rewritten


def test_full_lifecycle_history_is_queryable(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    target = root / "a.txt"
    target.write_text("one", "utf-8")
    db = Database(memory=True)
    clock = iter(
        [
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 2, 1, tzinfo=timezone.utc),
            datetime(2026, 3, 1, tzinfo=timezone.utc),
        ]
    )

    sync_files(db, root, now=lambda: next(clock))
    target.write_text("two", "utf-8")
    sync_files(db, root, now=lambda: next(clock))
    target.unlink()
    sync_files(db, root, now=lambda: next(clock))

    events = db.execute(
        "SELECT event, old_hash, new_hash, changed_at FROM files_history " "ORDER BY id"
    ).fetchall()
    assert [e[0] for e in events] == ["insert", "update", "delete"]
    assert events[0][1] is None and events[0][2] == sha256(b"one")
    assert events[1] == (
        "update",
        sha256(b"one"),
        sha256(b"two"),
        "2026-02-01T00:00:00+00:00",
    )
    assert events[2][1] == sha256(b"two") and events[2][2] is None


def test_manual_rows_survive_deletes_and_pin_content(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    write_tree(root, {"a.txt": "alpha"})
    db = Database(memory=True)
    sync_files(db, root)
    # A user-created row with its own BLOB
    manual_hash = add_manual_row(db, "manual.txt", b"hand made")

    (root / "a.txt").unlink()
    sync_files(db, root)

    assert current_paths(db) == {"manual.txt"}
    # The managed BLOB was collected, the manual BLOB is pinned
    assert content_hashes(db) == {manual_hash}
    # No history was ever written for the manual row
    assert [r[1] for r in history_rows(db)] == ["a.txt", "a.txt"]


def test_manual_row_at_synced_path_is_conflict_and_left_alone(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    db = Database(memory=True)
    # Create the schema by syncing the empty directory once
    sync_files(db, root)
    manual_hash = add_manual_row(db, "a.txt", b"manual wins")
    write_tree(root, {"a.txt": "from disk"})

    result = sync_files(db, root)

    assert result.added == 0
    assert result.conflicts == ["a.txt"]
    row = db.execute(
        "SELECT content_hash, managed_by FROM files WHERE path = 'a.txt'"
    ).fetchone()
    assert row == (manual_hash, None)  # untouched
    assert content_hashes(db) == {manual_hash}
    # The conflict is stable across reruns
    assert sync_files(db, root).conflicts == ["a.txt"]


def test_multiple_roots_have_independent_ownership(tmp_path):
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    write_tree(root_a, {"a.txt": "alpha", "x.txt": "shared"})
    write_tree(root_b, {"b.txt": "bravo", "y.txt": "shared"})
    db = Database(memory=True)

    sync_files(db, [root_a, root_b])

    assert current_paths(db) == {"a.txt", "b.txt", "x.txt", "y.txt"}
    assert content_hashes(db) == {sha256(b"alpha"), sha256(b"bravo"), sha256(b"shared")}
    tags = dict(db.execute("SELECT path, managed_by FROM files").fetchall())
    assert tags["a.txt"] == str(root_a.resolve())
    assert tags["b.txt"] == str(root_b.resolve())

    # A file vanishing from root A must not affect root B
    (root_a / "a.txt").unlink()
    result = sync_files(db, [root_a, root_b])
    assert result.deleted == 1
    assert current_paths(db) == {"b.txt", "x.txt", "y.txt"}
    assert content_for(db, "b.txt") == b"bravo"

    # The shared BLOB goes only when both references are gone
    assert result.contents_deleted == 1
    (root_a / "x.txt").unlink()
    (root_b / "y.txt").unlink()
    result = sync_files(db, [root_a, root_b])
    assert result.contents_deleted == 1
    assert sha256(b"shared") not in content_hashes(db)


def test_managed_by_tag_groups_sources(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    write_tree(root, {"a.txt": "alpha"})
    db = Database(memory=True)

    sync_files(db, root, managed_by="import-1")
    assert db.execute("SELECT managed_by FROM files").fetchone() == ("import-1",)

    (root / "a.txt").unlink()
    result = sync_files(db, root, managed_by="import-1")
    assert result.deleted == 1
    assert current_paths(db) == set()


def test_missing_directory_raises(tmp_path):
    db = Database(memory=True)
    with pytest.raises(Exception, match="Directory does not exist"):
        sync_files(db, tmp_path / "nope")
    assert db.table_names() == []


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root bypasses file permissions",
)
def test_read_failure_leaves_no_state(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    write_tree(root, {"a.txt": "alpha"})
    secret = root / "secret.txt"
    secret.write_text("secret", "utf-8")
    secret.chmod(0o000)
    db = Database(memory=True)

    with pytest.raises(PermissionError):
        sync_files(db, root)

    # Reads precede every write: no schema, no rows, nothing to roll back
    assert db.table_names() == []
    secret.chmod(0o644)


def test_history_write_failure_rolls_back_everything(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    write_tree(root, {"a.txt": "one"})
    db = Database(memory=True)
    sync_files(db, root)
    old_hash = sha256(b"one")

    # Sabotage history writes
    db.execute(
        "CREATE TRIGGER block_history BEFORE INSERT ON files_history "
        "BEGIN SELECT RAISE(ABORT, 'history broken'); END"
    )
    (root / "a.txt").write_text("two", "utf-8")

    with pytest.raises(sqlite3.DatabaseError, match="history broken"):
        sync_files(db, root)

    # The current table and BLOBs are unchanged
    assert content_for(db, "a.txt") == b"one"
    assert content_hashes(db) == {old_hash}
    assert len(history_rows(db)) == 1  # only the original insert


class _InterruptedDatabase(Database):
    """Fails during the DELETE FROM files phase, simulating an interruption."""

    def execute(self, sql, parameters=None):  # type: ignore[override]
        if sql.lstrip().lower().startswith('delete from "files" where'):
            raise KeyboardInterrupt
        return super().execute(sql, parameters)


def test_interrupted_rerun_converges(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    write_tree(root, {"a.txt": "one", "b.txt": "two"})
    db_path = tmp_path / "test.db"

    db = Database(db_path)
    sync_files(db, root)
    db.close()

    (root / "a.txt").unlink()

    interrupted = _InterruptedDatabase(db_path)
    with pytest.raises(KeyboardInterrupt):
        sync_files(interrupted, root)
    interrupted.close()

    # No partial result survived the rolled-back run
    db = Database(db_path)
    assert current_paths(db) == {"a.txt", "b.txt"}
    delete_events = [r for r in history_rows(db) if r[0] == "delete"]
    assert delete_events == []
    assert content_hashes(db) == {sha256(b"one"), sha256(b"two")}

    # The rerun converges: exactly one delete, one garbage-collected BLOB
    result = sync_files(db, root)
    assert (result.deleted, result.contents_deleted) == (1, 1)
    assert current_paths(db) == {"b.txt"}
    delete_events = [r for r in history_rows(db) if r[0] == "delete"]
    assert len(delete_events) == 1
    assert delete_events[0][1] == "a.txt"
    # A further rerun is a no-op
    assert not sync_files(db, root).changed


def test_cli_sync_files(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem():
        pathlib.Path("docs").mkdir()
        pathlib.Path("docs/a.txt").write_text("alpha", "utf-8")
        pathlib.Path("docs/b.txt").write_text("beta", "utf-8")

        result = runner.invoke(
            cli.cli,
            ["sync-files", "docs.db", "files", "docs"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert "2 added, 0 updated, 0 deleted, 0 unchanged" in result.output

        # Rerun is a stable no-op
        result = runner.invoke(
            cli.cli,
            ["sync-files", "docs.db", "files", "docs"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "0 added, 0 updated, 0 deleted, 2 unchanged" in result.output

        db = Database("docs.db")
        assert current_paths(db) == {"a.txt", "b.txt"}
        assert content_for(db, "a.txt") == b"alpha"

        pathlib.Path("docs/a.txt").unlink()
        result = runner.invoke(
            cli.cli,
            ["sync-files", "docs.db", "files", "docs"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "0 added, 0 updated, 1 deleted" in result.output
        assert current_paths(db) == {"b.txt"}
