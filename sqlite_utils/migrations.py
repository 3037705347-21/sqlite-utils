import ast
import datetime
import hashlib
import inspect
import marshal
import textwrap
import types
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, cast

if TYPE_CHECKING:
    from sqlite_utils.db import Database, Table


class _MigrationFunction(Protocol):
    __name__: str
    __code__: types.CodeType

    def __call__(self, db: "Database", /) -> None: ...


_MigrationFunctionT = TypeVar("_MigrationFunctionT", bound=_MigrationFunction)

# Statuses stored in the ``status`` column of ``_sqlite_migrations``.
# A NULL status means the row was written by an older release (or by hand)
# and is treated the same as APPLIED.
STATUS_APPLIED = "applied"
STATUS_RUNNING = "running"


class MigrationHistoryError(ValueError):
    """
    Raised when a migration recorded as applied no longer matches the code
    currently registered under that name in a migration set.
    """


@dataclass
class _HistoryMismatch:
    name: str
    recorded_hash: str
    current_hash: str
    running: bool = False


@dataclass
class _InterruptedMigration:
    name: str
    # Timestamp from the _sqlite_migrations row - the time the migration
    # started running (it never reached "applied")
    started_at: str
    # Named checkpoints the migration had completed before it was interrupted
    checkpoints: list[str] = field(default_factory=list)


class MigrationContext:
    """
    Resume context passed to a migration function that accepts a second
    argument. Only non-transactional migrations need this - their changes
    cannot be rolled back, so named checkpoints let a migration record how
    far it got and skip completed work when it is re-run after an
    interruption.

    Usage::

        @migrations(transactional=False)
        def rebuild_dogs(db, ctx):
            if not ctx.done("vacuumed"):
                db.execute("VACUUM")
                ctx.complete("vacuumed")
            if not ctx.done("reinserted"):
                db.execute("INSERT INTO dogs ...")
                ctx.complete("reinserted")
    """

    def __init__(
        self,
        db: "Database",
        migration_set: str,
        migration_name: str,
        durable: bool = False,
    ):
        self._db = db
        self._migration_set = migration_set
        self._migration_name = migration_name
        # durable=True for non-transactional migrations: their work (and
        # checkpoint records) must be committed immediately so an
        # interruption leaves observable, resumable state
        self._durable = durable
        self._completed: list[str] | None = None

    @property
    def migration_set(self) -> str:
        return self._migration_set

    @property
    def migration_name(self) -> str:
        return self._migration_name

    def completed(self) -> list[str]:
        """Names of checkpoints already recorded, in recording order."""
        if self._completed is None:
            table = _table(self._db, Migrations.checkpoints_table)
            if table.exists():
                self._completed = [
                    cast(str, row["name"])
                    for row in table.rows_where(
                        "migration_set = ? and migration_name = ?",
                        [self._migration_set, self._migration_name],
                        order_by="rowid",
                    )
                ]
            else:
                self._completed = []
        return list(self._completed)

    def done(self, name: str) -> bool:
        """Return ``True`` if checkpoint ``name`` has already been recorded."""
        return name in self.completed()

    def complete(self, name: str) -> None:
        """
        Record checkpoint ``name`` as completed. For a non-transactional
        migration the record is committed immediately (along with any work
        the migration has done so far) so it survives a later failure or
        interruption. Calling this more than once with the same name is a
        no-op.
        """
        if self.done(name):
            return
        _ensure_checkpoints_table(self._db)
        _table(self._db, Migrations.checkpoints_table).insert(
            {
                "migration_set": self._migration_set,
                "migration_name": self._migration_name,
                "name": name,
                "created_at": _utc_now(),
            }
        )
        if self._durable:
            self._db.commit()
        self._completed = None

    def step(self, name: str, fn: Callable[[], Any]) -> None:
        """
        Run ``fn()`` unless checkpoint ``name`` has already been recorded,
        then record the checkpoint if it finished without raising. This is
        shorthand for the :meth:`done` / :meth:`complete` pattern::

            ctx.step("vacuumed", lambda: db.execute("VACUUM"))
        """
        if not self.done(name):
            fn()
            self.complete(name)


class Migrations:
    migrations_table = "_sqlite_migrations"
    checkpoints_table = "_sqlite_migration_checkpoints"

    @dataclass
    class _Migration:
        name: str
        fn: _MigrationFunction
        transactional: bool = True

    @dataclass
    class _AppliedMigration:
        name: str
        # A string timestamp such as "2026-07-04 12:00:00.000000+00:00" -
        # stored as TEXT in the _sqlite_migrations table
        applied_at: str
        # sha256 hex digest of the migration code when it was applied, or
        # None for rows written by an older release or by hand
        content_hash: str | None = None
        # STATUS_APPLIED / STATUS_RUNNING, or None for older/manual rows
        status: str | None = None

    def __init__(self, name: str):
        """
        :param name: The name of the migration set. This should be unique.
        """
        self.name = name
        self._migrations: list[Migrations._Migration] = []

    def __call__(
        self, *, name: str | None = None, transactional: bool = True
    ) -> Callable[[_MigrationFunctionT], _MigrationFunctionT]:
        """
        :param name: The name to use for this migration - if not provided,
          the name of the function will be used.
        :param transactional: If ``True`` (the default) the migration and the
          record of it having been applied are wrapped in a transaction, which
          will be rolled back if the migration raises an exception. Pass
          ``False`` for migrations that cannot run inside a transaction, for
          example those that execute ``VACUUM``.
        """

        def inner(func: _MigrationFunctionT) -> _MigrationFunctionT:
            migration_name = name or func.__name__
            if any(m.name == migration_name for m in self._migrations):
                raise ValueError(
                    f"Migration '{migration_name}' is already registered in set '{self.name}'"
                )
            self._migrations.append(
                self._Migration(migration_name, func, transactional)
            )
            return func

        return inner

    def pending(self, db: "Database") -> list["Migrations._Migration"]:
        """
        Return a list of pending migrations.

        A non-transactional migration that started but never finished (its
        record has status ``running``) is included so that it can resume.

        This is a read-only operation - it does not write to the database.
        """
        completed = {migration.name for migration in self.applied(db)}
        return [
            migration
            for migration in self._migrations
            if migration.name not in completed
        ]

    def applied(self, db: "Database") -> list["Migrations._AppliedMigration"]:
        """
        Return a list of applied migrations, in the order they were applied.

        Rows left in the ``running`` state by an interrupted
        non-transactional migration are excluded - use
        :meth:`interrupted` to see those.

        This is a read-only operation - it does not write to the database.
        """
        return [
            self._AppliedMigration(
                name=row["name"],
                applied_at=row["applied_at"],
                content_hash=row["content_hash"],
                status=row["status"],
            )
            for row in self._history_rows(db)
            if row["status"] != STATUS_RUNNING
        ]

    def interrupted(self, db: "Database") -> list[_InterruptedMigration]:
        """
        Return non-transactional migrations that started (their record has
        status ``running``) but never reached ``applied``, in the order they
        were started, along with the checkpoints each one completed.

        This is a read-only operation - it does not write to the database.
        """
        interrupted = []
        for row in self._history_rows(db):
            if row["status"] != STATUS_RUNNING:
                continue
            interrupted.append(
                _InterruptedMigration(
                    name=row["name"],
                    started_at=row["applied_at"],
                    checkpoints=self._checkpoints(db, row["name"]),
                )
            )
        return interrupted

    def drift(self, db: "Database") -> list[_HistoryMismatch]:
        """
        Compare registered migrations with the recorded history for this set.

        Returns a :class:`_HistoryMismatch` for every recorded migration
        whose content hash no longer matches the code currently registered
        under that name (including interrupted ``running`` migrations).
        Rows with no recorded hash - written by an older release or inserted
        by hand - are not checked and are never modified.

        This is a read-only operation - it does not write to the database.
        """
        rows_by_name = {row["name"]: row for row in self._history_rows(db)}
        mismatches: list[_HistoryMismatch] = []
        for migration in self._migrations:
            row = rows_by_name.get(migration.name)
            if row is None or row["content_hash"] is None:
                continue
            current_hash = _content_hash(migration)
            if row["content_hash"] != current_hash:
                mismatches.append(
                    _HistoryMismatch(
                        name=migration.name,
                        recorded_hash=row["content_hash"],
                        current_hash=current_hash,
                        running=row["status"] == STATUS_RUNNING,
                    )
                )
        return mismatches

    def verify(self, db: "Database") -> None:
        """
        Check that every recorded migration still matches the code currently
        registered under its name.

        :raises MigrationHistoryError: if any applied (or interrupted
          ``running``) migration's code has changed since its record was
          written. Nothing in the database is modified when this raises.
        """
        mismatches = self.drift(db)
        if not mismatches:
            return
        lines = [f"Migration history verification failed for set '{self.name}':"]
        for mismatch in mismatches:
            state = (
                "was interrupted while running"
                if mismatch.running
                else "was recorded as applied"
            )
            lines.append(
                f"  {mismatch.name}: migration code has changed since it {state} "
                f"(recorded hash {mismatch.recorded_hash}, "
                f"current hash {mismatch.current_hash})"
            )
        lines.append(
            "Revert the changed migration(s) back to the code that ran, or "
            "put the new implementation in a new migration registered under "
            "a new name. Migrations that have already run must not be edited."
        )
        raise MigrationHistoryError("\n".join(lines))

    def apply(self, db: "Database", *, stop_before: str | Iterable[str] | None = None):
        """
        Apply any pending migrations to the database.

        Before anything runs, the recorded history is reconciled against the
        registered migrations: if a migration recorded for this set has a
        content hash that no longer matches its current code,
        :class:`MigrationHistoryError` is raised and no migrations are
        applied. Records belonging to other migration sets, and records with
        no hash (written by an older release or by hand), are left untouched.

        Each transactional migration runs inside a transaction, together
        with the record of it having been applied - if the migration raises
        an exception its changes are rolled back, no record is written and
        the migration stays pending. Migrations registered with
        ``transactional=False`` run outside of a transaction: a ``running``
        record is committed before the function executes and becomes
        ``applied`` once it returns. If such a migration is interrupted,
        re-applying invokes it again with a :class:`MigrationContext`, so
        completed checkpoints can be skipped and the run converges without
        repeating their side effects.

        :raises ValueError: if a ``stop_before`` name matches a migration in
          this set that has already been applied - stopping before it is
          impossible to honor, and no pending migrations are applied
        :raises MigrationHistoryError: if recorded migration code has
          changed
        """
        if stop_before is None:
            stop_before_names = set()
        elif isinstance(stop_before, str):
            stop_before_names = {stop_before}
        else:
            stop_before_names = set(stop_before)
        # A stop_before naming an already-applied migration cannot be
        # honored - error rather than applying everything after it. Names
        # not in this set at all are ignored, because unqualified CLI
        # values are offered to every migration set
        already_applied = stop_before_names.intersection(
            migration.name for migration in self.applied(db)
        )
        if already_applied:
            raise ValueError(
                "Cannot stop before migration{} {} in set '{}' - already "
                "been applied".format(
                    "s" if len(already_applied) > 1 else "",
                    ", ".join(sorted(already_applied)),
                    self.name,
                )
            )
        # Reconcile history before writing anything
        self.verify(db)
        self.ensure_migrations_table(db)
        for migration in self.pending(db):
            name = migration.name
            if name in stop_before_names:
                return
            content_hash = _content_hash(migration)
            context = MigrationContext(
                db,
                self.name,
                name,
                durable=not migration.transactional,
            )
            if migration.transactional:
                with db.atomic():
                    _invoke(migration, db, context)
                    self._record_applied(db, name, content_hash)
            else:
                # Commit the "running" record before the non-transactional
                # migration's side effects. Individual write statements are
                # committed by the database layer as they execute, so on
                # failure the function is simply invoked again on the next
                # run and its checkpoints tell it what to skip
                self._record_running(db, name, content_hash)
                _invoke(migration, db, context)
                self._mark_applied(db, name)

    def _record_applied(self, db: "Database", name: str, content_hash: str) -> None:
        _table(db, self.migrations_table).insert(
            {
                "migration_set": self.name,
                "name": name,
                "applied_at": _utc_now(),
                "content_hash": content_hash,
                "status": STATUS_APPLIED,
            }
        )

    def _record_running(self, db: "Database", name: str, content_hash: str) -> None:
        table = _table(db, self.migrations_table)
        existing = next(
            table.rows_where("migration_set = ? and name = ?", [self.name, name]),
            None,
        )
        if existing is not None:
            # Resuming an interrupted migration - leave its record and
            # checkpoints exactly as they were
            return
        table.insert(
            {
                "migration_set": self.name,
                "name": name,
                "applied_at": _utc_now(),
                "content_hash": content_hash,
                "status": STATUS_RUNNING,
            }
        )
        # This record must be on disk before the non-transactional
        # migration's side effects start - an implicit driver transaction
        # left open when the process is killed would be rolled back
        db.commit()

    def _mark_applied(self, db: "Database", name: str) -> None:
        db.execute(
            f"update {self.migrations_table} set status = ? "
            "where migration_set = ? and name = ?",
            [STATUS_APPLIED, self.name, name],
        )
        db.commit()

    def _history_rows(self, db: "Database") -> list[dict[str, Any]]:
        """Rows recorded for this set, tolerating older table schemas."""
        table = _table(db, self.migrations_table)
        if not table.exists():
            return []
        columns = table.columns_dict
        rows: list[dict[str, Any]] = []
        for row in table.rows_where("migration_set = ?", [self.name], order_by="rowid"):
            rows.append(
                {
                    "name": row["name"],
                    "applied_at": row["applied_at"],
                    "content_hash": (
                        row["content_hash"] if "content_hash" in columns else None
                    ),
                    "status": row["status"] if "status" in columns else None,
                }
            )
        return rows

    def _checkpoints(self, db: "Database", migration_name: str) -> list[str]:
        table = _table(db, self.checkpoints_table)
        if not table.exists():
            return []
        return [
            cast(str, row["name"])
            for row in table.rows_where(
                "migration_set = ? and migration_name = ?",
                [self.name, migration_name],
                order_by="rowid",
            )
        ]

    def ensure_migrations_table(self, db: "Database"):
        """
        Ensure the _sqlite_migrations table exists and has the correct schema.
        """
        table = _table(db, self.migrations_table)
        if not table.exists():
            table.create(
                {
                    "id": int,
                    "migration_set": str,
                    "name": str,
                    "applied_at": str,
                    "content_hash": str,
                    "status": str,
                },
                pk="id",
            )
            table.create_index(["migration_set", "name"], unique=True)
        else:
            if table.pks != ["id"]:
                table.transform(pk="id")
                unique_indexes = {tuple(index.columns) for index in table.indexes}
                if ("migration_set", "name") not in unique_indexes:
                    table.create_index(["migration_set", "name"], unique=True)
            # Add columns introduced for history reconciliation. Existing
            # rows keep NULL values - they are older/manual records and are
            # never rewritten.
            for column_name in ("content_hash", "status"):
                if column_name not in table.columns_dict:
                    table.add_column(column_name, str)

    def __repr__(self):
        return "<Migrations '{}': [{}]>".format(
            self.name, ", ".join(m.name for m in self._migrations)
        )


def _invoke(
    migration: "Migrations._Migration", db: "Database", context: MigrationContext
) -> None:
    # Migration functions are typed as taking one argument, but functions
    # that opt into resumability declare a second MigrationContext one
    fn = cast(Callable[..., None], migration.fn)
    if _accepts_context(migration.fn):
        fn(db, context)
    else:
        fn(db)


def _accepts_context(fn: _MigrationFunction) -> bool:
    try:
        parameters = tuple(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return False
    positional_kinds = (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )
    positional = sum(1 for p in parameters if p.kind in positional_kinds)
    has_var_positional = any(
        p.kind == inspect.Parameter.VAR_POSITIONAL for p in parameters
    )
    return positional >= 2 or has_var_positional


def _content_hash(migration: "Migrations._Migration") -> str:
    """
    sha256 of the migration's behavior: the ``transactional`` flag plus the
    function source code starting at its ``def`` line (decorator lines are
    excluded, so the name of the migrations object the function was
    registered through does not affect the hash). Falls back to a hash of
    the code object when source is unavailable (functions created
    dynamically, for example in a REPL).
    """
    payload = _migration_payload(migration)
    return hashlib.sha256(payload).hexdigest()


def _migration_payload(migration: "Migrations._Migration") -> bytes:
    mode = b"transactional\n" if migration.transactional else b"non-transactional\n"
    try:
        lines, _ = inspect.getsourcelines(migration.fn)
    except (OSError, TypeError, IndexError):
        return mode + marshal.dumps(migration.fn.__code__)
    source = _strip_decorators(lines)
    return mode + source.replace("\r\n", "\n").encode("utf-8")


def _strip_decorators(lines: list[str]) -> str:
    """
    Return just the ``def`` line and body of a decorated function.

    ``getsourcelines()`` starts at the first decorator and
    ``co_firstlineno`` points there too, so parse the block and use the AST
    node's line range - this also handles multi-line decorator expressions.
    """
    block = "".join(lines)
    try:
        tree = ast.parse(textwrap.dedent(block))
        node = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
    except (SyntaxError, StopIteration, ValueError):
        # Unparseable or unavailable source - hash the whole block rather
        # than failing to record anything
        return block
    return "".join(lines[node.lineno - 1 : node.end_lineno or node.lineno])


def _ensure_checkpoints_table(db: "Database") -> None:
    table = _table(db, Migrations.checkpoints_table)
    if table.exists():
        return
    table.create(
        {
            "id": int,
            "migration_set": str,
            "migration_name": str,
            "name": str,
            "created_at": str,
        },
        pk="id",
    )
    table.create_index(["migration_set", "migration_name", "name"], unique=True)


def _utc_now() -> str:
    return str(datetime.datetime.now(datetime.timezone.utc))


def _table(db: "Database", name: str) -> "Table":
    return cast("Table", db[name])
