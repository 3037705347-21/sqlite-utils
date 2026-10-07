from __future__ import annotations

import hashlib
import pathlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Sequence

from .db import quote_identifier
from .utils import chunks

if TYPE_CHECKING:
    from .db import Database

# Events recorded in the history table
EVENT_INSERT = "insert"
EVENT_UPDATE = "update"
EVENT_DELETE = "delete"


class FileSyncError(Exception):
    pass


@dataclass
class FileSyncResult:
    """
    Outcome of a single :func:`sync_files` run.

    :param roots: Resolved identifiers of the synced directories (their
      ``managed_by`` ownership tags).
    :param table: Name of the table holding the current file references.
    :param content_table: Name of the content-addressed BLOB table.
    :param history_table: Name of the table holding change history.
    :param added: Number of new file references written.
    :param updated: Number of file references whose content hash changed.
    :param deleted: Number of sync-managed references removed because the
      file disappeared from the source directory.
    :param unchanged: Number of references whose content hash was the same.
    :param contents_added: Number of new distinct BLOBs stored.
    :param contents_deleted: Number of BLOBs garbage-collected after their
      last reference disappeared.
    :param conflicts: Paths that could not be synced because the current
      table already holds a non-managed (or differently-managed) row.
    """

    roots: tuple[str, ...]
    table: str
    content_table: str
    history_table: str
    added: int = 0
    updated: int = 0
    deleted: int = 0
    unchanged: int = 0
    contents_added: int = 0
    contents_deleted: int = 0
    conflicts: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        """``True`` if the current table or content store was modified."""
        return bool(
            self.added
            or self.updated
            or self.deleted
            or self.contents_added
            or self.contents_deleted
        )


@dataclass(frozen=True)
class _Entry:
    content_hash: str
    content: bytes
    size: int
    mtime: str


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _scan_root(
    root: pathlib.Path, managed_by: str | None
) -> tuple[str, dict[str, _Entry]]:
    # Every file is read in full here, before the database transaction is
    # opened. A read failure therefore cannot leave any partial state behind.
    if not root.is_dir():
        raise FileSyncError(f"Directory does not exist: {root}")
    root_id = managed_by or str(root.resolve())
    scanned: dict[str, _Entry] = {}
    for file_path in sorted(root.rglob("*")):
        if not file_path.is_file():
            continue
        relative_path = file_path.relative_to(root).as_posix()
        data = file_path.read_bytes()
        stat = file_path.stat()
        scanned[relative_path] = _Entry(
            content_hash=hashlib.sha256(data).hexdigest(),
            content=data,
            size=len(data),
            mtime=datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        )
    return root_id, scanned


def _ensure_schema(
    db: "Database", table: str, content_table: str, history_table: str
) -> None:
    q = quote_identifier
    db.execute(f"""
        CREATE TABLE IF NOT EXISTS {q(table)} (
            path TEXT PRIMARY KEY,
            content_hash TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime TEXT,
            managed_by TEXT,
            synced_at TEXT NOT NULL
        )
        """)
    db.execute(f"""
        CREATE TABLE IF NOT EXISTS {q(content_table)} (
            content_hash TEXT PRIMARY KEY,
            content BLOB NOT NULL,
            size INTEGER NOT NULL
        )
        """)
    db.execute(f"""
        CREATE TABLE IF NOT EXISTS {q(history_table)} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event TEXT NOT NULL,
            path TEXT NOT NULL,
            old_hash TEXT,
            new_hash TEXT,
            size INTEGER,
            managed_by TEXT,
            changed_at TEXT NOT NULL
        )
        """)
    db.execute(
        f"CREATE INDEX IF NOT EXISTS {q(table + '_content_hash_idx')} "
        f"ON {q(table)}(content_hash)"
    )
    db.execute(
        f"CREATE INDEX IF NOT EXISTS {q(table + '_managed_by_idx')} "
        f"ON {q(table)}(managed_by)"
    )
    db.execute(
        f"CREATE INDEX IF NOT EXISTS {q(history_table + '_path_idx')} "
        f"ON {q(history_table)}(path)"
    )


def _reconcile_root(
    db: "Database",
    root_id: str,
    scanned: dict[str, _Entry],
    *,
    table: str,
    content_table: str,
    history_table: str,
    timestamp: str,
    result: FileSyncResult,
) -> None:
    q = quote_identifier
    # Current references owned by this sync source
    cursor = db.execute(
        f"SELECT path, content_hash, size FROM {q(table)} WHERE managed_by = ?",
        [root_id],
    )
    existing = {row[0]: (row[1], row[2]) for row in cursor.fetchall()}

    current_paths = set(scanned)
    existing_paths = set(existing)
    insert_paths = current_paths - existing_paths
    delete_paths = existing_paths - current_paths
    update_paths = {
        path
        for path in current_paths & existing_paths
        if scanned[path].content_hash != existing[path][0]
    }

    # Rows that are manual (managed_by IS NULL) or owned by another source
    # are never overwritten or removed by this sync.
    conflicts: set[str] = set()
    for batch in chunks(sorted(insert_paths), 500):
        batch = list(batch)
        params: list[str] = [root_id] + batch
        placeholders = ", ".join("?" for _ in batch)
        rows = db.execute(
            f"SELECT path FROM {q(table)} "
            f"WHERE (managed_by IS NULL OR managed_by != ?) "
            f"AND path IN ({placeholders})",
            params,
        ).fetchall()
        conflicts.update(row[0] for row in rows)
    insert_paths -= conflicts
    if conflicts:
        result.conflicts.extend(sorted(conflicts))

    # 1) Record deletions in history before removing the references, so a
    #    history-write failure rolls the deletions back too.
    history_rows: list[
        tuple[str, str, str | None, str | None, int | None, str | None, str]
    ] = [
        (
            EVENT_DELETE,
            path,
            existing[path][0],
            None,
            existing[path][1],
            root_id,
            timestamp,
        )
        for path in sorted(delete_paths)
    ]
    # 2) Record updates (old hash -> new hash).
    history_rows += [
        (
            EVENT_UPDATE,
            path,
            existing[path][0],
            scanned[path].content_hash,
            scanned[path].size,
            root_id,
            timestamp,
        )
        for path in sorted(update_paths)
    ]
    # 3) Record inserts (no previous hash).
    history_rows += [
        (
            EVENT_INSERT,
            path,
            None,
            scanned[path].content_hash,
            scanned[path].size,
            root_id,
            timestamp,
        )
        for path in sorted(insert_paths)
    ]
    if history_rows:
        db.conn.executemany(
            f"INSERT INTO {q(history_table)} "
            "(event, path, old_hash, new_hash, size, managed_by, changed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            history_rows,
        )

    # 4) Remove references that disappeared from the source.
    for path in sorted(delete_paths):
        db.execute(
            f"DELETE FROM {q(table)} WHERE path = ? AND managed_by = ?",
            [path, root_id],
        )

    # 5) Update references whose content changed.
    for path in sorted(update_paths):
        entry = scanned[path]
        db.execute(
            f"UPDATE {q(table)} SET content_hash = ?, size = ?, mtime = ?, "
            "synced_at = ? WHERE path = ? AND managed_by = ?",
            [
                entry.content_hash,
                entry.size,
                entry.mtime,
                timestamp,
                path,
                root_id,
            ],
        )

    # 6) Store each distinct BLOB once, ignoring ones already present.
    entries_by_hash: dict[str, _Entry] = {}
    for path in insert_paths | update_paths:
        entries_by_hash.setdefault(scanned[path].content_hash, scanned[path])
    for batch in chunks(sorted(entries_by_hash), 500):
        batch = list(batch)
        placeholders = ", ".join("?" for _ in batch)
        present = {
            row[0]
            for row in db.execute(
                f"SELECT content_hash FROM {q(content_table)} "
                f"WHERE content_hash IN ({placeholders})",
                list(batch),
            ).fetchall()
        }
        missing = [hash_value for hash_value in batch if hash_value not in present]
        if missing:
            db.conn.executemany(
                f"INSERT OR IGNORE INTO {q(content_table)} "
                "(content_hash, content, size) VALUES (?, ?, ?)",
                [
                    (
                        hash_value,
                        entries_by_hash[hash_value].content,
                        entries_by_hash[hash_value].size,
                    )
                    for hash_value in missing
                ],
            )
            result.contents_added += len(missing)

    # 7) Insert the new references.
    for path in sorted(insert_paths):
        entry = scanned[path]
        db.execute(
            f"INSERT INTO {q(table)} "
            "(path, content_hash, size, mtime, managed_by, synced_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                path,
                entry.content_hash,
                entry.size,
                entry.mtime,
                root_id,
                timestamp,
            ],
        )

    result.deleted += len(delete_paths)
    result.updated += len(update_paths)
    result.added += len(insert_paths)
    result.unchanged += len(current_paths & existing_paths) - len(update_paths)


def sync_files(
    db: "Database",
    roots: str | pathlib.Path | Sequence[str | pathlib.Path],
    *,
    table: str = "files",
    content_table: str | None = None,
    history_table: str | None = None,
    managed_by: str | None = None,
    now: Callable[[], datetime] | None = None,
) -> FileSyncResult:
    """
    Reconcile a directory (or several directories) with a SQLite database.

    The function is safe to run repeatedly. Each run makes the current table
    match the directories for rows owned by the sync, while leaving manual
    rows untouched. Identical file contents - even at different paths - are
    stored once in a content table and only deleted when the last reference
    to them disappears. Every insert, update and deletion is appended to a
    history table.

    All file reads happen before any database write, and every database
    change happens in a single transaction, so read failures, history-write
    failures and interruptions roll the whole run back. Re-running after an
    interruption converges to the same state: an unchanged directory
    produces no new rows.

    :param db: The ``sqlite_utils.Database`` instance to write to.
    :param roots: A directory path, or a list of directory paths, to sync.
      When several are given they share one transaction; by default each is
      identified by its resolved absolute path.
    :param table: Name of the table holding current file references.
    :param content_table: Name of the content-addressed BLOB table
      (defaults to ``"{table}_content"``).
    :param history_table: Name of the change-history table
      (defaults to ``"{table}_history"``).
    :param managed_by: Ownership tag written to synced rows. Defaults to
      each directory's resolved absolute path. Only rows carrying this tag
      are removed when their source file disappears.
    :param now: Callable returning the current UTC ``datetime`` (mainly
      useful for tests).
    """
    if isinstance(roots, (str, pathlib.Path)):
        root_paths = [pathlib.Path(roots)]
    else:
        root_paths = [pathlib.Path(root) for root in roots]
    if not root_paths:
        raise FileSyncError("At least one directory must be provided")

    content_table = content_table or f"{table}_content"
    history_table = history_table or f"{table}_history"
    clock = now or _utc_now

    # Read every source file first so that an I/O failure never opens the
    # transaction and cannot leave partial state behind.
    scanned_roots = [_scan_root(root, managed_by) for root in root_paths]

    result = FileSyncResult(
        roots=tuple(root_id for root_id, _ in scanned_roots),
        table=table,
        content_table=content_table,
        history_table=history_table,
    )

    # One transaction for the whole run: any failure rolls everything back.
    with db.atomic():
        _ensure_schema(db, table, content_table, history_table)
        timestamp = clock().isoformat()
        for root_id, scanned in scanned_roots:
            _reconcile_root(
                db,
                root_id,
                scanned,
                table=table,
                content_table=content_table,
                history_table=history_table,
                timestamp=timestamp,
                result=result,
            )
        # Only deletions and updates can orphan BLOBs; skipping the sweep
        # otherwise keeps an unchanged rerun free of any write.
        if result.deleted or result.updated:
            # NOT EXISTS (rather than NOT IN) means a NULL content_hash on a
            # manual row can neither pin nor accidentally protect a BLOB.
            q_content = quote_identifier(content_table)
            q_table = quote_identifier(table)
            cursor = db.execute(
                f"DELETE FROM {q_content} WHERE NOT EXISTS ("
                f"SELECT 1 FROM {q_table} f "
                f"WHERE f.content_hash = {q_content}.content_hash)"
            )
            result.contents_deleted = cursor.rowcount

    return result
