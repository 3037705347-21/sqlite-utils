import pytest

import sqlite_utils
from sqlite_utils import (
    MigrationContext,
    MigrationHistoryError,
    Migrations,
)
from sqlite_utils.migrations import STATUS_APPLIED, STATUS_RUNNING


@pytest.fixture
def migrations():
    migrations = Migrations("test")

    @migrations()
    def m001(db):
        db.table("dogs").insert({"name": "Cleo"})

    @migrations()
    def m002(db):
        db.table("cats").create({"name": str})
        db.execute("insert into dogs (name) values ('Pancakes')")

    return migrations


@pytest.fixture
def migrations_not_ordered_alphabetically():
    # Names order alphabetically in the wrong direction but this
    # should still be applied correctly.
    migrations = Migrations("test")

    @migrations()
    def m002(db):
        db.table("dogs").insert({"name": "Cleo"})

    @migrations()
    def m001(db):
        db.table("cats").create({"name": str})
        db.execute("insert into dogs (name) values ('Pancakes')")

    return migrations


@pytest.fixture
def migrations2():
    migrations = Migrations("test2")

    @migrations()
    def m001(db):
        db.table("dogs2").insert({"name": "Cleo"})

    return migrations


def test_basic(migrations):
    db = sqlite_utils.Database(memory=True)
    assert db.table_names() == []
    migrations.apply(db)
    assert set(db.table_names()) == {"_sqlite_migrations", "dogs", "cats"}


def test_stop_before(migrations):
    db = sqlite_utils.Database(memory=True)
    assert db.table_names() == []
    migrations.apply(db, stop_before="m002")
    assert set(db.table_names()) == {"_sqlite_migrations", "dogs"}
    migrations.apply(db)
    assert set(db.table_names()) == {"_sqlite_migrations", "dogs", "cats"}


def test_two_migration_sets(migrations, migrations2):
    db = sqlite_utils.Database(memory=True)
    assert db.table_names() == []
    migrations.apply(db)
    migrations2.apply(db)
    assert set(db.table_names()) == {"_sqlite_migrations", "dogs", "cats", "dogs2"}


def test_order_does_not_matter(migrations, migrations_not_ordered_alphabetically):
    db1 = sqlite_utils.Database(memory=True)
    db2 = sqlite_utils.Database(memory=True)
    migrations.apply(db1)
    migrations_not_ordered_alphabetically.apply(db2)
    assert db1.schema == db2.schema


def test_applied_at_is_a_string(migrations):
    db = sqlite_utils.Database(memory=True)
    migrations.apply(db)
    applied = migrations.applied(db)
    assert len(applied) == 2
    for migration in applied:
        # applied_at is the TEXT timestamp straight from the
        # _sqlite_migrations table, e.g. "2026-07-04 12:00:00.000000+00:00"
        assert isinstance(migration.applied_at, str)
        assert migration.applied_at.endswith("+00:00")


def test_failing_migration_rolls_back(migrations):
    @migrations()
    def m003(db):
        db.table("birds").create({"name": str})
        db.execute("insert into dogs (name) values ('Dozer')")
        raise ValueError("boom")

    db = sqlite_utils.Database(memory=True)
    with pytest.raises(ValueError):
        migrations.apply(db)
    # m001 and m002 committed before the failure and stay applied
    assert set(db.table_names()) == {"_sqlite_migrations", "dogs", "cats"}
    assert [r["name"] for r in db.table("dogs").rows] == ["Cleo", "Pancakes"]
    assert [m.name for m in migrations.applied(db)] == ["m001", "m002"]
    # Everything m003 did was rolled back and it is still pending
    assert [m.name for m in migrations.pending(db)] == ["m003"]


def test_rerun_after_failure_applies_each_migration_once():
    state = {"fail": True}
    migrations = Migrations("test")

    @migrations()
    def m001(db):
        db.table("dogs").insert({"name": "Cleo"})

    @migrations()
    def m002(db):
        db.table("dogs").insert({"name": "Pancakes"})
        if state["fail"]:
            raise ValueError("boom")

    db = sqlite_utils.Database(memory=True)
    with pytest.raises(ValueError):
        migrations.apply(db)
    state["fail"] = False
    migrations.apply(db)
    # m001 must not have been re-applied, m002 applied exactly once
    assert [r["name"] for r in db.table("dogs").rows] == ["Cleo", "Pancakes"]
    assert [m.name for m in migrations.applied(db)] == ["m001", "m002"]


def test_non_transactional_migration_allows_vacuum(tmpdir):
    path = str(tmpdir / "test.db")
    db = sqlite_utils.Database(path)
    migrations = Migrations("test")

    @migrations()
    def m001(db):
        db.table("dogs").insert({"name": "Cleo"})

    @migrations(transactional=False)
    def m002(db):
        db.execute("VACUUM")

    migrations.apply(db)
    assert [m.name for m in migrations.applied(db)] == ["m001", "m002"]
    db.close()


def test_apply_composes_inside_outer_transaction(migrations):
    db = sqlite_utils.Database(memory=True)
    with pytest.raises(ZeroDivisionError), db.atomic():
        migrations.apply(db)
        raise ZeroDivisionError
    # The outer transaction rolled back, taking the migrations with it
    assert db.table_names() == []


@pytest.mark.parametrize(
    "create_table,pk",
    (
        (
            {
                "migration_set": str,
                "name": str,
                "applied_at": str,
            },
            "name",
        ),
        (
            {
                "migration_set": str,
                "name": str,
                "applied_at": str,
            },
            ("migration_set", "name"),
        ),
    ),
)
def test_upgrades_sqlite_migrations(migrations, create_table, pk):
    db = sqlite_utils.Database(memory=True)
    db.table("_sqlite_migrations").create(create_table, pk=pk)
    assert db.table_names() == ["_sqlite_migrations"]
    assert db.table("_sqlite_migrations").pks == (
        [pk] if isinstance(pk, str) else list(pk)
    )
    migrations.apply(db)
    assert db.table("_sqlite_migrations").pks == ["id"]


def test_pending_and_applied_are_read_only(migrations):
    db = sqlite_utils.Database(memory=True)
    assert [m.name for m in migrations.pending(db)] == ["m001", "m002"]
    assert migrations.applied(db) == []
    # Neither call should have created the tracking table
    assert db.table_names() == []


def test_duplicate_migration_name_errors():
    migrations = Migrations("test")

    @migrations()
    def m001(db):
        pass

    with pytest.raises(ValueError) as ex:

        @migrations(name="m001")
        def m001_again(db):
            pass

    assert "m001" in str(ex.value)


def test_stop_before_applied_migration_errors(migrations):
    # Stopping before a migration that has already been applied is
    # impossible to honor - previously the stop name was only checked
    # against pending migrations, so everything after it was applied
    db = sqlite_utils.Database(memory=True)
    migrations.apply(db, stop_before="m002")  # applies m001 only
    with pytest.raises(ValueError) as ex:
        migrations.apply(db, stop_before="m001")
    assert "m001" in str(ex.value)
    assert "already been applied" in str(ex.value)
    # Nothing else was applied
    assert not db.table("cats").exists()


def test_stop_before_applied_migration_errors_before_any_apply(migrations):
    # The error fires before any pending migration runs, even those that
    # come before the already-applied stop target in registration order
    db = sqlite_utils.Database(memory=True)
    only_second = Migrations("test")

    @only_second()
    def m002(db):
        db.table("cats").create({"name": str})

    only_second.apply(db)  # m002 applied, m001 still pending
    with pytest.raises(ValueError):
        migrations.apply(db, stop_before="m002")
    assert not db.table("dogs").exists()


# --- content hash history verification ---


def test_records_content_hash_and_applied_status(migrations):
    db = sqlite_utils.Database(memory=True)
    migrations.apply(db)
    rows = list(db.table("_sqlite_migrations").rows)
    assert [r["name"] for r in rows] == ["m001", "m002"]
    for row in rows:
        assert row["status"] == STATUS_APPLIED
        assert isinstance(row["content_hash"], str)
        assert len(row["content_hash"]) == 64
        int(row["content_hash"], 16)
    applied = migrations.applied(db)
    assert [m.status for m in applied] == [STATUS_APPLIED, STATUS_APPLIED]
    assert all(m.content_hash for m in applied)


def test_rerun_with_unchanged_code_converges(migrations):
    db = sqlite_utils.Database(memory=True)
    migrations.apply(db)
    hashes_before = [r["content_hash"] for r in db.table("_sqlite_migrations").rows]
    # Second run applies nothing and rewrites nothing
    migrations.apply(db)
    hashes_after = [r["content_hash"] for r in db.table("_sqlite_migrations").rows]
    assert hashes_after == hashes_before
    assert migrations.pending(db) == []
    assert migrations.interrupted(db) == []
    assert migrations.drift(db) == []


def test_changed_applied_migration_code_raises():
    db = sqlite_utils.Database(memory=True)

    def build_v1():
        v1 = Migrations("test")

        @v1()
        def m001(db):
            db.table("dogs").insert({"name": "Cleo"})

        @v1()
        def m002(db):
            db.execute("insert into dogs (name) values ('Pancakes')")

        return v1

    def build_v2():
        # Simulate a merge that rewrites m002 under the same migration name
        v2 = Migrations("test")

        @v2()
        def m001(db):
            db.table("dogs").insert({"name": "Cleo"})

        @v2()
        def m002(db):
            db.execute("insert into dogs (name) values ('CHANGED')")

        return v2

    v1 = build_v1()
    v1.apply(db)
    v2 = build_v2()

    mismatches = v2.drift(db)
    assert [m.name for m in mismatches] == ["m002"]
    assert mismatches[0].recorded_hash != mismatches[0].current_hash
    assert mismatches[0].running is False

    with pytest.raises(MigrationHistoryError) as ex:
        v2.apply(db)
    message = str(ex.value)
    assert "test" in message
    assert "m002" in message
    assert mismatches[0].recorded_hash in message
    # m001 still matches and must not be accused
    assert "m001" not in [m.name for m in v2.drift(db)]
    # Nothing new ran - no duplicate side effects
    assert [r["name"] for r in db.table("dogs").rows] == ["Cleo", "Pancakes"]
    assert [m.name for m in v2.applied(db)] == ["m001", "m002"]


def test_changed_transactional_flag_raises():
    # The transactional flag is part of a migration's behavior
    db = sqlite_utils.Database(memory=True)
    v1 = Migrations("test")

    @v1()
    def m001(db):
        db.table("dogs").insert({"name": "Cleo"})

    v1.apply(db)
    v2 = Migrations("test")

    @v2(transactional=False)
    def m001(db):  # noqa: F811
        db.table("dogs").insert({"name": "Cleo"})

    with pytest.raises(MigrationHistoryError):
        v2.apply(db)


def test_verify_runs_before_anything_is_applied(migrations):
    # A later migration is pending, but verification failure must stop the
    # whole run before it executes
    db = sqlite_utils.Database(memory=True)
    migrations.apply(db)

    changed = Migrations("test")

    @changed()
    def m001(db):
        db.table("dogs").insert({"name": "Cleo"})

    @changed()
    def m002(db):
        db.execute("insert into dogs (name) values ('DIFFERENT')")

    @changed()
    def m003(db):
        db.table("birds").create({"name": str})

    with pytest.raises(MigrationHistoryError):
        changed.apply(db)
    assert not db.table("birds").exists()
    assert [r["name"] for r in db.table("dogs").rows] == ["Cleo", "Pancakes"]


def test_verify_drift_interrupted_are_read_only(migrations):
    db = sqlite_utils.Database(memory=True)
    # Table must not be created by the read-only inspection methods
    migrations.verify(db)
    assert migrations.drift(db) == []
    assert migrations.applied(db) == []
    assert migrations.pending(db)[0].name == "m001"
    assert migrations.interrupted(db) == []
    assert db.table_names() == []


def test_manual_row_without_hash_is_trusted_and_not_rewritten():
    # A record written by hand or by an older release (NULL content_hash)
    # cannot be verified, but it also must never be mutated
    db = sqlite_utils.Database(memory=True)
    db.table("_sqlite_migrations").create(
        {"migration_set": str, "name": str, "applied_at": str}, pk="id"
    )
    db.table("_sqlite_migrations").insert(
        {"migration_set": "test", "name": "m001", "applied_at": "manual"}
    )

    migrations = Migrations("test")

    @migrations()
    def m001(db):
        # Even a completely different body must not trigger an error here
        db.table("dogs").create({"name": str})

    migrations.apply(db)
    assert migrations.pending(db) == []
    assert migrations.drift(db) == []
    assert not db.table("dogs").exists()
    row = next(
        db.table("_sqlite_migrations").rows_where(
            "migration_set = ? and name = ?", ["test", "m001"]
        )
    )
    assert row["applied_at"] == "manual"
    assert row["content_hash"] is None
    assert row["status"] is None


def test_other_sets_and_manual_rows_are_not_touched():
    db = sqlite_utils.Database(memory=True)

    def build_a1():
        a1 = Migrations("a")

        @a1()
        def m001(db):
            db.table("a_dogs").insert({"name": "Cleo"})

        return a1

    def build_b1():
        b1 = Migrations("b")

        @b1()
        def m001(db):
            db.table("b_dogs").insert({"name": "Cleo"})

        return b1

    def build_a2():
        a2 = Migrations("a")

        @a2()
        def m001(db):
            db.table("a_dogs").insert({"name": "CHANGED"})

        return a2

    def build_b2():
        # Unchanged code in set b - must verify against b1's record
        b2 = Migrations("b")

        @b2()
        def m001(db):
            db.table("b_dogs").insert({"name": "Cleo"})

        return b2

    a1, b1, a2, b2 = build_a1(), build_b1(), build_a2(), build_b2()
    a1.apply(db)
    b1.apply(db)
    # A hand-written record sharing the drifted set must be preserved
    db.table("_sqlite_migrations").insert(
        {
            "migration_set": "a",
            "name": "manual_note",
            "applied_at": "hand-written",
            "content_hash": None,
            "status": None,
        }
    )

    with pytest.raises(MigrationHistoryError):
        a2.apply(db)
    # Set b verifies and applies independently
    b2.apply(db)
    manual = next(
        db.table("_sqlite_migrations").rows_where(
            "migration_set = ? and name = ?", ["a", "manual_note"]
        )
    )
    assert manual["applied_at"] == "hand-written"
    assert manual["content_hash"] is None
    # Set a's applied rows were not rewritten either
    a_row = next(
        db.table("_sqlite_migrations").rows_where(
            "migration_set = ? and name = ?", ["a", "m001"]
        )
    )
    assert a_row["status"] == STATUS_APPLIED
    assert [r["name"] for r in db.table("a_dogs").rows] == ["Cleo"]
    assert [r["name"] for r in db.table("b_dogs").rows] == ["Cleo"]


# --- non-transactional migrations: interruption and resume ---


def test_non_transactional_success_marks_applied(tmpdir):
    path = str(tmpdir / "test.db")
    db = sqlite_utils.Database(path)
    migrations = Migrations("test")
    seen = []

    @migrations()
    def m001(db):
        db.table("dogs").insert({"name": "Cleo"})

    @migrations(transactional=False)
    def m002(db, ctx):
        assert isinstance(ctx, MigrationContext)
        seen.append(ctx.migration_name)
        db.execute("VACUUM")

    migrations.apply(db)
    assert seen == ["m002"]
    statuses = {r["name"]: r["status"] for r in db.table("_sqlite_migrations").rows}
    assert statuses == {"m001": STATUS_APPLIED, "m002": STATUS_APPLIED}
    assert migrations.interrupted(db) == []
    db.close()


def test_context_not_passed_to_single_arg_function():
    db = sqlite_utils.Database(memory=True)
    migrations = Migrations("test")

    @migrations(transactional=False)
    def m001(db):
        db.table("dogs").insert({"name": "Cleo"})

    migrations.apply(db)  # no TypeError from unexpected second argument


def test_interrupted_non_transactional_resumes_from_checkpoints():
    db = sqlite_utils.Database(memory=True)
    state = {"fail": True}
    migrations = Migrations("test")

    @migrations()
    def setup(db):
        db.table("dogs").create({"name": str}, pk="name")

    @migrations(transactional=False)
    def load_dogs(db, ctx):
        # Completed steps are skipped on resume
        ctx.step("cleo", lambda: db.execute("insert into dogs (name) values ('Cleo')"))

        def add_pancakes():
            # A step interrupted at its tail is retried - it must be
            # idempotent about its own side effects
            db.execute("insert or ignore into dogs (name) values ('Pancakes')")
            if state["fail"]:
                raise RuntimeError("interrupted halfway")

        ctx.step("pancakes", add_pancakes)

    with pytest.raises(RuntimeError, match="interrupted halfway"):
        migrations.apply(db)

    # The interruption is observable...
    interrupted = migrations.interrupted(db)
    assert [m.name for m in interrupted] == ["load_dogs"]
    assert interrupted[0].started_at.endswith("+00:00")
    assert interrupted[0].checkpoints == ["cleo"]
    # ...applied/pending reflect it...
    assert [m.name for m in migrations.applied(db)] == ["setup"]
    assert [m.name for m in migrations.pending(db)] == ["load_dogs"]
    # ...and the write statements already executed are durable (the
    # pancakes step itself is idempotent so its retry cannot duplicate)
    assert [r["name"] for r in db.table("dogs").rows] == ["Cleo", "Pancakes"]
    assert db.table("_sqlite_migration_checkpoints").exists()

    # Re-running resumes past the completed checkpoint and converges -
    # pancakes is retried once and its idempotent write does not duplicate
    state["fail"] = False
    migrations.apply(db)
    assert [r["name"] for r in db.table("dogs").rows] == ["Cleo", "Pancakes"]
    assert migrations.interrupted(db) == []
    assert migrations.pending(db) == []
    status = next(
        db.table("_sqlite_migrations").rows_where(
            "migration_set = ? and name = ?", ["test", "load_dogs"]
        )
    )
    assert status["status"] == STATUS_APPLIED

    # A third run does nothing - no duplicate side effects
    migrations.apply(db)
    assert [r["name"] for r in db.table("dogs").rows] == ["Cleo", "Pancakes"]


def test_step_helper_skips_completed_work():
    db = sqlite_utils.Database(memory=True)
    state = {"fail": True}
    calls = []
    migrations = Migrations("test")

    @migrations(transactional=False)
    def m001(db, ctx):
        def first():
            calls.append("first")
            db.table("dogs").create({"name": str})

        def second():
            calls.append("second")
            # Idempotent under retry
            if not db.execute("select 1 from dogs where name = 'Pancakes'").fetchall():
                db.execute("insert into dogs (name) values ('Pancakes')")
            if state["fail"]:
                raise RuntimeError("boom")

        ctx.step("first", first)
        ctx.step("second", second)

    with pytest.raises(RuntimeError):
        migrations.apply(db)
    state["fail"] = False
    migrations.apply(db)
    # first() never reran; second() reran but did not duplicate its row
    assert calls == ["first", "second", "second"]
    assert [r["name"] for r in db.table("dogs").rows] == ["Pancakes"]


def test_checkpoint_survives_interruption_as_committed(tmpdir):
    # Checkpoints written by a non-transactional migration must be visible
    # to a brand new connection (i.e. actually committed on disk)
    path = str(tmpdir / "test.db")
    db = sqlite_utils.Database(path)
    migrations = Migrations("test")

    @migrations(transactional=False)
    def m001(db, ctx):
        db.table("dogs").create({"name": str})
        ctx.complete("created")
        raise RuntimeError("interrupted")

    with pytest.raises(RuntimeError):
        migrations.apply(db)
    db.close()

    db2 = sqlite_utils.Database(path)
    interrupted = migrations.interrupted(db2)
    assert [m.name for m in interrupted] == ["m001"]
    assert interrupted[0].checkpoints == ["created"]
    db2.close()


def test_resume_after_interrupted_running_row_converges():
    # Simulate a process killed mid-migration: a "running" row plus one
    # checkpoint exist on disk; the resumed migration uses guards.
    db = sqlite_utils.Database(memory=True)
    db.table("dogs").create({"name": str})
    migrations = Migrations("test")
    migrations.ensure_migrations_table(db)
    db.table("_sqlite_migrations").insert(
        {
            "migration_set": "test",
            "name": "m001",
            "applied_at": "2026-01-01 00:00:00.000000+00:00",
            "content_hash": None,
            "status": STATUS_RUNNING,
        }
    )

    @migrations(transactional=False)
    def m001(db, ctx):
        if not ctx.done("inserted"):
            db.execute("insert into dogs (name) values ('Cleo')")
            ctx.complete("inserted")

    migrations.apply(db)
    assert [r["name"] for r in db.table("dogs").rows] == ["Cleo"]
    assert migrations.interrupted(db) == []
    # applied_at (start time) from the original running row is retained
    row = next(db.table("_sqlite_migrations").rows)
    assert row["applied_at"] == "2026-01-01 00:00:00.000000+00:00"
    assert row["status"] == STATUS_APPLIED


def test_changed_interrupted_migration_code_raises():
    db = sqlite_utils.Database(memory=True)
    v1 = Migrations("test")

    @v1(transactional=False)
    def m001(db, ctx):
        ctx.complete("step1")
        raise RuntimeError("interrupted")

    with pytest.raises(RuntimeError):
        v1.apply(db)

    v2 = Migrations("test")

    @v2(transactional=False)
    def m001(db, ctx):  # noqa: F811
        ctx.complete("step1-renamed")

    mismatches = v2.drift(db)
    assert [m.name for m in mismatches] == ["m001"]
    assert mismatches[0].running is True
    with pytest.raises(MigrationHistoryError):
        v2.apply(db)
    # Checkpoint state is untouched by the failed verification/resume
    assert v1.interrupted(db)[0].checkpoints == ["step1"]


# --- schema upgrades ---


def test_upgrade_adds_history_columns_preserving_legacy_rows():
    db = sqlite_utils.Database(memory=True)
    db.table("_sqlite_migrations").create(
        {"migration_set": str, "name": str, "applied_at": str}, pk="id"
    )
    db.table("_sqlite_migrations").insert(
        {"migration_set": "test", "name": "old_one", "applied_at": "old"}
    )
    migrations = Migrations("test")

    @migrations()
    def old_one(db):
        # Recorded as applied by an older release, so this must not run
        db.table("skipped").create({"name": str})

    @migrations()
    def new_one(db):
        db.table("fresh").create({"name": str})

    migrations.apply(db)
    columns = db.table("_sqlite_migrations").columns_dict
    assert "content_hash" in columns
    assert "status" in columns
    rows = list(db.table("_sqlite_migrations").rows)
    old = next(r for r in rows if r["name"] == "old_one")
    assert old["applied_at"] == "old"
    assert old["content_hash"] is None
    assert old["status"] is None
    # The legacy migration was not re-run; the new one ran normally
    assert not db.table("skipped").exists()
    assert db.table("fresh").exists()
    new = next(r for r in rows if r["name"] == "new_one")
    assert new["status"] == STATUS_APPLIED
    assert new["content_hash"]
