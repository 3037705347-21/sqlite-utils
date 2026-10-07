import json
import sqlite3
from datetime import datetime, timezone

import pytest
from click.testing import CliRunner

from sqlite_utils import ColumnAnalysisError, Database, cli, sync_column_analysis

STATS = "t_column_stats"
HISTORY = "t_column_stats_history"
ARCHIVE = "t_column_stats_archive"


def make_db(values=("a", "a"), column="cat", path=None):
    db = Database(memory=True) if path is None else Database(path)
    db["t"].insert_all([{column: value} for value in values])
    return db


def stats_rows(db, table=STATS):
    return {
        row[0]: dict(
            total_rows=row[1],
            num_null=row[2],
            num_blank=row[3],
            num_distinct=row[4],
            most_common=json.loads(row[5]) if row[5] else None,
            least_common=json.loads(row[6]) if row[6] else None,
            fingerprint=row[7],
            note=row[8],
            analyzed_at=row[9],
        )
        for row in db.execute(
            f'SELECT "column", total_rows, num_null, num_blank, num_distinct, '
            f"most_common, least_common, fingerprint, note, analyzed_at "
            f'FROM {table} ORDER BY "column"'
        ).fetchall()
    }


def history_for(db, column, table=HISTORY):
    return db.execute(
        f"SELECT id, num_distinct, fingerprint, analyzed_at FROM {table} "
        'WHERE tbl = ? AND "column" = ? ORDER BY id',
        ["t", column],
    ).fetchall()


def archive_for(db, column, table=ARCHIVE):
    return db.execute(
        f"SELECT num_distinct, fingerprint, note, archived_at FROM {table} "
        'WHERE tbl = ? AND "column" = ? ORDER BY id',
        ["t", column],
    ).fetchall()


def set_note(db, column, note, table=STATS):
    db.execute(
        f'UPDATE {table} SET note = ? WHERE tbl = ? AND "column" = ?',
        [note, "t", column],
    )


def drop_column(db, column, table="t"):
    db.execute(f'ALTER TABLE {table} DROP COLUMN "{column}"')


_counter = [0]


@pytest.fixture(autouse=True)
def reset_clock():
    _counter[0] = 0
    yield


def tick():
    # Deterministic UTC timestamps, reset for every test
    _counter[0] += 1
    month = (_counter[0] - 1) % 12 + 1
    return datetime(2026, month, 1, tzinfo=timezone.utc)


def test_first_run_creates_schema_and_rows():
    db = make_db(["a", "a", "b"])
    result = sync_column_analysis(db, "t", now=tick)

    assert (result.added, result.updated, result.unchanged) == (1, 0, 0)
    assert result.snapshots_added == 1
    assert (result.snapshots_pruned, result.archived, result.deleted) == (0, 0, 0)
    assert result.columns == ("cat",)
    assert result.changed
    assert {STATS, HISTORY, ARCHIVE} <= set(db.table_names())

    rows = stats_rows(db)
    assert set(rows) == {"cat"}
    cat = rows["cat"]
    assert (cat["total_rows"], cat["num_null"], cat["num_distinct"]) == (3, 0, 2)
    assert cat["most_common"] == [["a", 2], ["b", 1]]
    assert cat["note"] is None
    assert cat["analyzed_at"] == "2026-01-01T00:00:00+00:00"

    history = history_for(db, "cat")
    assert len(history) == 1
    assert history[0][1] == 2
    assert history[0][2] == cat["fingerprint"]
    # The archive stays empty while every column is alive
    assert db.execute(f"SELECT COUNT(*) FROM {ARCHIVE}").fetchone()[0] == 0


def test_unchanged_rerun_is_a_noop():
    db = make_db(["a", "a", "b"])
    first = sync_column_analysis(db, "t", now=tick)
    assert first.changed
    before = stats_rows(db)["cat"]

    statements = []
    db._tracer = lambda sql, params: statements.append(sql.lstrip().upper())
    second = sync_column_analysis(db, "t", now=tick)

    assert (second.added, second.updated, second.unchanged) == (0, 0, 1)
    assert (second.snapshots_added, second.snapshots_pruned) == (0, 0)
    assert not second.changed
    assert len(history_for(db, "cat")) == 1
    assert stats_rows(db)["cat"] == before  # nothing was rewritten
    assert not any(s.startswith(("INSERT", "UPDATE", "DELETE")) for s in statements)


def test_data_change_updates_current_and_appends_deduplicated_snapshot():
    db = make_db(["a", "a"])
    sync_column_analysis(db, "t", now=tick)
    first_fp = stats_rows(db)["cat"]["fingerprint"]

    db["t"].insert({"cat": "b"})
    result = sync_column_analysis(db, "t", now=tick)

    assert (result.added, result.updated, result.unchanged) == (0, 1, 0)
    assert result.snapshots_added == 1
    cat = stats_rows(db)["cat"]
    assert cat["num_distinct"] == 2
    assert cat["fingerprint"] != first_fp

    history = history_for(db, "cat")
    assert [row[1] for row in history] == [1, 2]
    assert [row[2] for row in history] == [first_fp, cat["fingerprint"]]

    # Rerunning converges: identical statistics add no snapshot and no write
    converged = sync_column_analysis(db, "t", now=tick)
    assert not converged.changed
    assert len(history_for(db, "cat")) == 2


def test_new_column_is_added_removed_column_is_archived_with_note():
    db = make_db(["a", "a"])
    sync_column_analysis(db, "t", now=tick)
    db["t"].add_column("extra", str)
    db.execute("UPDATE \"t\" SET extra = 'x'")
    sync_column_analysis(db, "t", now=tick)
    set_note(db, "extra", "manually reviewed")
    original_history = history_for(db, "extra")
    assert len(original_history) == 1

    drop_column(db, "extra")
    result = sync_column_analysis(db, "t", now=tick)

    assert result.archived == 1
    assert (result.added, result.updated, result.deleted) == (0, 0, 0)
    assert set(stats_rows(db)) == {"cat"}

    archived = archive_for(db, "extra")
    assert len(archived) == 1
    assert archived[0][0] == 1  # num_distinct at the time of the last analysis
    assert archived[0][2] == "manually reviewed"  # note carried over
    assert archived[0][3] == "2026-03-01T00:00:00+00:00"
    # The dropped column's history snapshots remain trackable
    assert history_for(db, "extra") == original_history


def test_readded_column_with_same_stats_gets_no_duplicate_snapshot():
    db = make_db(["a", "a"])
    sync_column_analysis(db, "t", now=tick)
    db["t"].add_column("extra", str)
    db.execute("UPDATE \"t\" SET extra = 'x'")
    sync_column_analysis(db, "t", now=tick)
    drop_column(db, "extra")
    sync_column_analysis(db, "t", now=tick)
    assert len(history_for(db, "extra")) == 1

    db["t"].add_column("extra", str)
    db.execute("UPDATE \"t\" SET extra = 'x'")
    result = sync_column_analysis(db, "t", now=tick)

    # The current row is new, but the latest historical snapshot already
    # describes exactly these statistics - no duplicate is appended.
    assert result.added == 1
    assert result.snapshots_added == 0
    assert len(history_for(db, "extra")) == 1
    assert stats_rows(db)["extra"]["note"] is None


def test_on_stale_delete_without_note_deletes_with_note_archives():
    db = make_db(["a", "a"])
    sync_column_analysis(db, "t", now=tick)
    db["t"].add_column("noted", str)
    db["t"].add_column("plain", str)
    db.execute("UPDATE \"t\" SET noted = 'y', plain = 'z'")
    sync_column_analysis(db, "t", now=tick)
    set_note(db, "noted", "keep me")
    drop_column(db, "noted")
    drop_column(db, "plain")

    result = sync_column_analysis(db, "t", on_stale="delete", now=tick)

    assert (result.deleted, result.archived) == (1, 1)
    assert set(stats_rows(db)) == {"cat"}
    assert archive_for(db, "plain") == []
    assert len(archive_for(db, "noted")) == 1
    assert archive_for(db, "noted")[0][2] == "keep me"
    # History of both dropped columns is retained
    assert len(history_for(db, "plain")) == 1
    assert len(history_for(db, "noted")) == 1


def test_manual_note_survives_statistics_updates_and_reruns():
    db = make_db(["a", "a"])
    sync_column_analysis(db, "t", now=tick)
    set_note(db, "cat", "important column")

    db["t"].insert({"cat": "b"})
    sync_column_analysis(db, "t", now=tick)
    assert stats_rows(db)["cat"]["note"] == "important column"
    sync_column_analysis(db, "t", now=tick)
    assert stats_rows(db)["cat"]["note"] == "important column"


def test_retention_prunes_oldest_snapshots():
    db = make_db(["a", "a"])
    sync_column_analysis(db, "t", retention=3, now=tick)
    for value in ("b", "c", "d", "e"):
        db["t"].insert({"cat": value})
        sync_column_analysis(db, "t", retention=3, now=tick)

    history = history_for(db, "cat")
    assert len(history) == 3
    # The newest three snapshots survive, in change order
    assert [row[1] for row in history] == [3, 4, 5]
    assert history[-1][2] == stats_rows(db)["cat"]["fingerprint"]


def test_lowering_retention_prunes_on_next_run_then_converges():
    db = make_db(["a", "a"])
    sync_column_analysis(db, "t", retention=10, now=tick)
    for value in ("b", "c", "d"):
        db["t"].insert({"cat": value})
        sync_column_analysis(db, "t", retention=10, now=tick)
    assert len(history_for(db, "cat")) == 4

    reduced = sync_column_analysis(db, "t", retention=2, now=tick)
    assert reduced.snapshots_pruned == 2
    assert [row[1] for row in history_for(db, "cat")] == [3, 4]

    again = sync_column_analysis(db, "t", retention=2, now=tick)
    assert again.snapshots_pruned == 0
    assert not again.changed
    assert len(history_for(db, "cat")) == 2


def test_retention_none_keeps_every_snapshot():
    db = make_db(["a", "a"])
    sync_column_analysis(db, "t", retention=None, now=tick)
    for value in ("b", "c", "d"):
        db["t"].insert({"cat": value})
        sync_column_analysis(db, "t", retention=None, now=tick)
    assert len(history_for(db, "cat")) == 4


def test_column_subset_leaves_other_stats_untouched():
    db = Database(memory=True)
    db["t"].insert_all([{"cat": "a", "other": "x"}, {"cat": "a", "other": "y"}])
    sync_column_analysis(db, "t", columns=["cat"], now=tick)
    assert set(stats_rows(db)) == {"cat"}

    result = sync_column_analysis(db, "t", columns=["cat"], now=tick)
    assert not result.changed
    assert set(stats_rows(db)) == {"cat"}


class _FailingAnalysisDatabase(Database):
    """Raises when the 'broken' column is analyzed, like a real query error."""

    def execute(self, sql, parameters=None):  # type: ignore[override]
        lowered = sql.lower()
        if lowered.startswith("select") and '"broken"' in lowered:
            raise sqlite3.OperationalError("analysis of broken failed")
        return super().execute(sql, parameters)


def test_analysis_failure_before_first_run_creates_nothing():
    db = _FailingAnalysisDatabase(memory=True)
    db["t"].insert_all([{"cat": "a", "broken": "x"}])

    with pytest.raises(sqlite3.OperationalError, match="analysis of broken"):
        sync_column_analysis(db, "t", now=tick)

    # All analysis precedes every write: no schema, no rows
    assert db.table_names() == ["t"]


def test_analysis_failure_after_first_run_leaves_state_intact():
    db = _FailingAnalysisDatabase(memory=True)
    db["t"].insert_all([{"cat": "a"}, {"cat": "a"}])
    sync_column_analysis(db, "t", now=tick)
    db["t"].add_column("broken", str)
    db.execute("UPDATE \"t\" SET broken = 'x'")
    first_history = history_for(db, "cat")
    first_stats = stats_rows(db)["cat"]

    with pytest.raises(sqlite3.OperationalError, match="analysis of broken"):
        sync_column_analysis(db, "t", now=tick)

    # The transaction rolled back: no archive row, no new snapshot
    assert stats_rows(db)["cat"] == first_stats
    assert history_for(db, "cat") == first_history
    assert "broken" not in stats_rows(db)
    assert db.execute(f"SELECT COUNT(*) FROM {ARCHIVE}").fetchone()[0] == 0


class _InterruptedDatabase(Database):
    """Fails while the current-statistics row is written, simulating an interrupt."""

    def execute(self, sql, parameters=None):  # type: ignore[override]
        lowered = sql.lstrip().lower()
        if lowered.startswith(f'insert into "{STATS}" (') or lowered.startswith(
            f'update "{STATS}" set'
        ):
            raise KeyboardInterrupt
        return super().execute(sql, parameters)


def test_interrupted_write_rolls_back_and_rerun_converges(tmp_path):
    path = str(tmp_path / "test.db")
    db = Database(path)
    db["t"].insert_all([{"cat": "a"}, {"cat": "a"}])
    sync_column_analysis(db, "t", now=tick)
    db["t"].insert({"cat": "b"})
    db.close()

    interrupted = _InterruptedDatabase(path)
    with pytest.raises(KeyboardInterrupt):
        sync_column_analysis(interrupted, "t", now=tick)
    interrupted.close()

    db = Database(path)
    # The rolled-back run changed nothing: still the first snapshot
    assert stats_rows(db)["cat"]["num_distinct"] == 1
    assert len(history_for(db, "cat")) == 1

    # The rerun converges to exactly one update + one new snapshot
    result = sync_column_analysis(db, "t", now=tick)
    assert (result.added, result.updated) == (0, 1)
    assert result.snapshots_added == 1
    assert stats_rows(db)["cat"]["num_distinct"] == 2
    assert len(history_for(db, "cat")) == 2
    assert not sync_column_analysis(db, "t", now=tick).changed


def test_invalid_arguments_raise_before_any_write():
    db = make_db(["a"])
    with pytest.raises(ColumnAnalysisError, match="Table does not exist"):
        sync_column_analysis(db, "missing")
    with pytest.raises(ColumnAnalysisError, match="columns were not found"):
        sync_column_analysis(db, "t", columns=["nope"])
    with pytest.raises(ColumnAnalysisError, match="retention"):
        sync_column_analysis(db, "t", retention=0)
    with pytest.raises(ColumnAnalysisError, match="on_stale"):
        sync_column_analysis(db, "t", on_stale="explode")
    assert db.table_names() == ["t"]


def test_custom_table_names():
    db = make_db(["a"])
    result = sync_column_analysis(
        db,
        "t",
        stats_table="my_stats",
        history_table="my_log",
        archive_table="my_archive",
        now=tick,
    )
    assert (result.stats_table, result.history_table, result.archive_table) == (
        "my_stats",
        "my_log",
        "my_archive",
    )
    assert {"my_stats", "my_log", "my_archive"} <= set(db.table_names())


def test_cli_sync_analysis(tmp_path):
    runner = CliRunner()
    db_path = tmp_path / "data.db"
    with runner.isolated_filesystem():
        db = Database(str(db_path))
        db["t"].insert_all(
            [
                {"cat": "a", "keep": "x"},
                {"cat": "a", "keep": "x"},
            ]
        )
        db.close()

        result = runner.invoke(
            cli.cli,
            ["sync-analysis", str(db_path), "t"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert "2 added, 0 updated, 0 unchanged" in result.output
        assert "2 snapshots added, 0 old snapshots pruned" in result.output
        assert "0 stale columns removed, 0 stale columns archived" in result.output

        # Unchanged rerun
        result = runner.invoke(
            cli.cli,
            ["sync-analysis", str(db_path), "t"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "0 added, 0 updated, 2 unchanged" in result.output

        # A change to cat only - keep stays identical so it is unchanged
        db = Database(str(db_path))
        db.execute("UPDATE \"t\" SET cat = 'b' WHERE rowid = 1")
        db.close()
        result = runner.invoke(
            cli.cli,
            ["sync-analysis", str(db_path), "t"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "0 added, 1 updated, 1 unchanged" in result.output

        # Dropping the column archives its row
        db = Database(str(db_path))
        db.execute('ALTER TABLE "t" DROP COLUMN "cat"')
        db.close()
        result = runner.invoke(
            cli.cli,
            ["sync-analysis", str(db_path), "t"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "1 stale columns removed, 0 stale columns archived" not in result.output
        assert "0 stale columns removed, 1 stale columns archived" in result.output


def test_cli_missing_table_reports_error(tmp_path):
    runner = CliRunner()
    db_path = tmp_path / "data.db"
    Database(str(db_path)).close()
    result = runner.invoke(
        cli.cli,
        ["sync-analysis", str(db_path), "ghost"],
        catch_exceptions=False,
    )
    assert result.exit_code == 1
    assert "Table does not exist: ghost" in result.output
