from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Sequence, cast

from .db import quote_identifier

if TYPE_CHECKING:
    from .db import ColumnDetails, Database, Table

# What to do with a stats row whose column has disappeared from the table
STALE_ARCHIVE = "archive"
STALE_DELETE = "delete"


class ColumnAnalysisError(Exception):
    pass


@dataclass
class ColumnAnalysisResult:
    """
    Outcome of a single :func:`sync_column_analysis` run.

    :param table: Name of the table whose columns were analyzed.
    :param stats_table: Name of the table holding the current statistics,
      one row per column.
    :param history_table: Name of the table holding deduplicated history
      snapshots - one row per analysis whose statistics changed.
    :param archive_table: Name of the table holding the final statistics
      of columns that disappeared from the source table, together with any
      manual notes attached to them.
    :param columns: Columns analyzed during this run, in analysis order.
    :param added: Number of current-statistics rows newly written.
    :param updated: Number of current-statistics rows whose statistics changed.
    :param unchanged: Number of columns whose statistics were identical to
      the stored row (nothing was rewritten).
    :param snapshots_added: Number of rows appended to the history table.
    :param snapshots_pruned: Number of history rows removed by the retention
      limit.
    :param archived: Number of stale column rows moved to the archive table.
    :param deleted: Number of stale column rows deleted outright (only rows
      without a manual note can be deleted).
    """

    table: str
    stats_table: str
    history_table: str
    archive_table: str
    columns: tuple[str, ...]
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    snapshots_added: int = 0
    snapshots_pruned: int = 0
    archived: int = 0
    deleted: int = 0

    @property
    def changed(self) -> bool:
        """``True`` if any reconciliation table was modified."""
        return bool(
            self.added
            or self.updated
            or self.snapshots_added
            or self.snapshots_pruned
            or self.archived
            or self.deleted
        )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _details_payload(details: "ColumnDetails") -> dict:
    return {
        "total_rows": details.total_rows,
        "num_null": details.num_null,
        "num_blank": details.num_blank,
        "num_distinct": details.num_distinct,
        "most_common": details.most_common,
        "least_common": details.least_common,
    }


def _fingerprint(payload: dict) -> str:
    # A canonical serialization of the statistics - two runs with the same
    # data produce the same fingerprint even if dict ordering differs.
    blob = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _json(value) -> str | None:
    if value is None:
        return None
    return json.dumps(value, default=str, ensure_ascii=False)


def _ensure_schema(
    db: "Database", stats_table: str, history_table: str, archive_table: str
) -> None:
    q = quote_identifier
    db.execute(f"""
        CREATE TABLE IF NOT EXISTS {q(stats_table)} (
            tbl TEXT NOT NULL,
            "column" TEXT NOT NULL,
            total_rows INTEGER,
            num_null INTEGER,
            num_blank INTEGER,
            num_distinct INTEGER,
            most_common TEXT,
            least_common TEXT,
            fingerprint TEXT NOT NULL,
            note TEXT,
            analyzed_at TEXT NOT NULL,
            PRIMARY KEY (tbl, "column")
        )
        """)
    db.execute(f"""
        CREATE TABLE IF NOT EXISTS {q(history_table)} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tbl TEXT NOT NULL,
            "column" TEXT NOT NULL,
            total_rows INTEGER,
            num_null INTEGER,
            num_blank INTEGER,
            num_distinct INTEGER,
            most_common TEXT,
            least_common TEXT,
            fingerprint TEXT NOT NULL,
            analyzed_at TEXT NOT NULL
        )
        """)
    db.execute(f"""
        CREATE TABLE IF NOT EXISTS {q(archive_table)} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tbl TEXT NOT NULL,
            "column" TEXT NOT NULL,
            total_rows INTEGER,
            num_null INTEGER,
            num_blank INTEGER,
            num_distinct INTEGER,
            most_common TEXT,
            least_common TEXT,
            fingerprint TEXT,
            note TEXT,
            archived_at TEXT NOT NULL
        )
        """)
    db.execute(
        f"CREATE INDEX IF NOT EXISTS {q(history_table + '_column_idx')} "
        f'ON {q(history_table)}(tbl, "column")'
    )
    db.execute(
        f"CREATE INDEX IF NOT EXISTS {q(archive_table + '_column_idx')} "
        f'ON {q(archive_table)}(tbl, "column")'
    )


def sync_column_analysis(
    db: "Database",
    table: str,
    *,
    columns: Sequence[str] | None = None,
    common_limit: int = 10,
    value_truncate: int | None = None,
    most_common: bool = True,
    least_common: bool = True,
    retention: int | None = 10,
    on_stale: str = STALE_ARCHIVE,
    stats_table: str | None = None,
    history_table: str | None = None,
    archive_table: str | None = None,
    now: Callable[[], datetime] | None = None,
) -> ColumnAnalysisResult:
    """
    Reconcile saved column analysis for a table with its current data.

    The function is safe to run repeatedly. Each run analyzes the columns
    and makes the current-statistics table reflect the data, while a history
    table accumulates deduplicated snapshots so trends stay visible: a
    snapshot is appended only when the statistics differ from that column's
    latest snapshot, so an unchanged rerun writes nothing and repeated runs
    converge to one row per actual change.

    At most ``retention`` snapshots are kept per column; older snapshots are
    pruned whenever the limit is exceeded (including when the limit is
    lowered between runs).

    Columns that disappeared from the table schema are handled according to
    ``on_stale``: ``"archive"`` (the default) moves their final statistics
    row - including any manually edited ``note`` - to an archive table;
    ``"delete"`` removes rows without a note and still archives rows that
    carry a note, so manual notes are never destroyed. History snapshots of
    dropped columns are always left in place so old records stay trackable.

    Every column is analyzed before any write happens, and all writes commit
    in a single transaction. A failed or interrupted analysis of any single
    column therefore cannot leave half-written current statistics or
    history - rerunning converges to the correct state.

    :param db: The ``sqlite_utils.Database`` instance to write to.
    :param table: Name of the table whose columns should be analyzed.
    :param columns: Columns to analyze. Defaults to every column in the
      table. Columns omitted here but still present in the table are left
      untouched; only columns absent from the table schema count as stale.
    :param common_limit: Show this many most/least common values.
    :param value_truncate: Truncate display of common values to this many
      characters.
    :param most_common: If ``True``, calculate the most common values.
    :param least_common: If ``True``, calculate the least common values.
    :param retention: Number of history snapshots to keep per column. Older
      snapshots are pruned. Set to ``None`` to keep every snapshot.
    :param on_stale: What to do with statistics rows whose column no longer
      exists: ``"archive"`` (default) or ``"delete"``. Rows with a manual
      note are archived in either mode.
    :param stats_table: Name of the current-statistics table (defaults to
      ``"{table}_column_stats"``).
    :param history_table: Name of the history-snapshots table (defaults to
      ``"{stats_table}_history"``).
    :param archive_table: Name of the stale-column archive table (defaults
      to ``"{stats_table}_archive"``).
    :param now: Callable returning the current UTC ``datetime`` (mainly
      useful for tests).
    """
    if table not in db.table_names():
        raise ColumnAnalysisError(f"Table does not exist: {table}")
    if on_stale not in (STALE_ARCHIVE, STALE_DELETE):
        raise ColumnAnalysisError(
            "on_stale must be 'archive' or 'delete', got: {!r}".format(on_stale)
        )
    if retention is not None and (not isinstance(retention, int) or retention < 1):
        raise ColumnAnalysisError(
            "retention must be a positive integer or None, got: {!r}".format(retention)
        )

    stats_table = stats_table or f"{table}_column_stats"
    history_table = history_table or f"{stats_table}_history"
    archive_table = archive_table or f"{stats_table}_archive"
    clock = now or _utc_now

    # The table name was just confirmed against db.table_names(), so this is
    # a Table rather than a View.
    source = cast("Table", db[table])
    live_columns = [column.name for column in source.columns]
    if columns is None:
        target_columns = list(live_columns)
    else:
        # De-duplicate a repeated column name while preserving order
        target_columns = list(dict.fromkeys(columns))
        missing = [name for name in target_columns if name not in live_columns]
        if missing:
            raise ColumnAnalysisError(
                "These columns were not found in {}: {}".format(
                    table, ", ".join(missing)
                )
            )

    # Analyze every column first. A failure here happens before the
    # reconciliation transaction (and even before the schema is created),
    # so it cannot leave partial current or history state behind.
    total_rows = source.count
    analyzed: dict[str, ColumnDetails] = {}
    for name in target_columns:
        analyzed[name] = source.analyze_column(
            name,
            common_limit=common_limit,
            value_truncate=value_truncate,
            total_rows=total_rows,
            most_common=most_common,
            least_common=least_common,
        )

    result = ColumnAnalysisResult(
        table=table,
        stats_table=stats_table,
        history_table=history_table,
        archive_table=archive_table,
        columns=tuple(target_columns),
    )

    # One transaction for every write so an interruption rolls all of it
    # back and a rerun converges to the same state.
    with db.atomic():
        _ensure_schema(db, stats_table, history_table, archive_table)
        timestamp = clock().isoformat()
        _reconcile(
            db,
            table=table,
            live_columns=live_columns,
            analyzed=analyzed,
            retention=retention,
            on_stale=on_stale,
            stats_table=stats_table,
            history_table=history_table,
            archive_table=archive_table,
            timestamp=timestamp,
            result=result,
        )

    return result


def _reconcile(
    db: "Database",
    *,
    table: str,
    live_columns: list[str],
    analyzed: dict[str, "ColumnDetails"],
    retention: int | None,
    on_stale: str,
    stats_table: str,
    history_table: str,
    archive_table: str,
    timestamp: str,
    result: ColumnAnalysisResult,
) -> None:
    q = quote_identifier
    q_stats = q(stats_table)
    q_history = q(history_table)
    q_archive = q(archive_table)

    # Existing current rows for this table:
    # (column, total_rows, num_null, num_blank, num_distinct,
    #  most_common, least_common, fingerprint, note)
    existing = {
        row[0]: row
        for row in db.execute(
            f'SELECT "column", total_rows, num_null, num_blank, num_distinct, '
            f"most_common, least_common, fingerprint, note "
            f"FROM {q_stats} WHERE tbl = ?",
            [table],
        ).fetchall()
    }

    # 1) Bring the current statistics up to date and append deduplicated
    #    snapshots for columns whose statistics changed.
    for name, details in analyzed.items():
        payload = _details_payload(details)
        fingerprint = _fingerprint(payload)
        old = existing.get(name)
        most_common_json = _json(details.most_common)
        least_common_json = _json(details.least_common)

        if old is None:
            db.execute(
                f'INSERT INTO {q_stats} (tbl, "column", total_rows, num_null, '
                "num_blank, num_distinct, most_common, least_common, "
                "fingerprint, note, analyzed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                [
                    table,
                    name,
                    details.total_rows,
                    details.num_null,
                    details.num_blank,
                    details.num_distinct,
                    most_common_json,
                    least_common_json,
                    fingerprint,
                    timestamp,
                ],
            )
            result.added += 1
            changed = True
        elif old[7] != fingerprint:
            # Note is deliberately excluded from the update - manual notes
            # are never overwritten by a reconciliation run.
            db.execute(
                f"UPDATE {q_stats} SET total_rows = ?, num_null = ?, "
                "num_blank = ?, num_distinct = ?, most_common = ?, "
                "least_common = ?, fingerprint = ?, analyzed_at = ? "
                'WHERE tbl = ? AND "column" = ?',
                [
                    details.total_rows,
                    details.num_null,
                    details.num_blank,
                    details.num_distinct,
                    most_common_json,
                    least_common_json,
                    fingerprint,
                    timestamp,
                    table,
                    name,
                ],
            )
            result.updated += 1
            changed = True
        else:
            result.unchanged += 1
            changed = False

        if not changed:
            continue

        # Dedup rule: only append when the fingerprint differs from the
        # column's most recent snapshot. A dropped-and-readded column whose
        # statistics are unchanged therefore gets no duplicate snapshot.
        latest = db.execute(
            f"SELECT fingerprint FROM {q_history} "
            'WHERE tbl = ? AND "column" = ? ORDER BY id DESC LIMIT 1',
            [table, name],
        ).fetchone()
        if latest is None or latest[0] != fingerprint:
            db.execute(
                f'INSERT INTO {q_history} (tbl, "column", total_rows, '
                "num_null, num_blank, num_distinct, most_common, least_common, "
                "fingerprint, analyzed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    table,
                    name,
                    details.total_rows,
                    details.num_null,
                    details.num_blank,
                    details.num_distinct,
                    most_common_json,
                    least_common_json,
                    fingerprint,
                    timestamp,
                ],
            )
            result.snapshots_added += 1

    # 2) Enforce the retention limit per analyzed column. The DELETE is only
    #    issued when the limit is exceeded, so an unchanged rerun performs no
    #    writes at all. History of dropped columns is never pruned here - it
    #    remains trackable after the column disappears.
    if retention is not None:
        for name in analyzed:
            count = db.execute(
                f"SELECT COUNT(*) FROM {q_history} " 'WHERE tbl = ? AND "column" = ?',
                [table, name],
            ).fetchone()[0]
            if count > retention:
                cursor = db.execute(
                    f'DELETE FROM {q_history} WHERE tbl = ? AND "column" = ? '
                    "AND id NOT IN (SELECT id FROM "
                    f'{q_history} WHERE tbl = ? AND "column" = ? '
                    "ORDER BY id DESC LIMIT ?)",
                    [table, name, table, name, retention],
                )
                result.snapshots_pruned += cursor.rowcount

    # 3) Handle rows for columns that disappeared from the table schema.
    stale_columns = sorted(set(existing) - set(live_columns))
    for name in stale_columns:
        old = existing[name]
        note = old[8]
        # Rows carrying a manual note are archived even in delete mode -
        # manual content must never be silently destroyed.
        if on_stale == STALE_DELETE and not note:
            db.execute(
                f'DELETE FROM {q_stats} WHERE tbl = ? AND "column" = ?',
                [table, name],
            )
            result.deleted += 1
        else:
            db.execute(
                f'INSERT INTO {q_archive} (tbl, "column", total_rows, '
                "num_null, num_blank, num_distinct, most_common, least_common, "
                "fingerprint, note, archived_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    table,
                    name,
                    old[1],
                    old[2],
                    old[3],
                    old[4],
                    old[5],
                    old[6],
                    old[7],
                    note,
                    timestamp,
                ],
            )
            db.execute(
                f'DELETE FROM {q_stats} WHERE tbl = ? AND "column" = ?',
                [table, name],
            )
            result.archived += 1
