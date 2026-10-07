.. _migrations:

=====================
 Database migrations
=====================

``sqlite-utils`` includes a migration system for applying repeatable changes to SQLite database files.

A migration is a Python function that receives a :class:`sqlite_utils.Database` instance and then executes Python code to modify that database - creating or transforming tables, adding indexes, inserting rows, or any other operation supported by SQLite.

Migrations are grouped into named sets using the :class:`sqlite_utils.Migrations` class, and each applied migration is recorded in the ``_sqlite_migrations`` table in that database.

This means you can run the migrate operation multiple times and it will only apply migrations that have not previously been recorded.

Each record also stores a hash of the migration's code, so editing a migration that has already run is detected and reported as an error instead of silently diverging across databases. Non-transactional migrations additionally record whether they are still running and named checkpoints, so an interrupted migration can show how far it got and resume without repeating completed work.

.. _migrations_define:

Defining migrations
===================

Ordered migration sets are defined by first creating a :class:`sqlite_utils.Migrations` object.

Individual migrations are Python functions that are then registered with that migration set. Each migration function is passed a single argument that is a :ref:`sqlite_utils.Database <reference_db_database>` instance.

The name passed to ``Migrations("creatures")`` identifies that set of migrations. Use a name that is unique for your project, since multiple migration sets can be applied to the same database.

Here is a simple example of a ``migrations.py`` file which creates a table, then adds an extra column to that table in a second migration:

.. code-block:: python

    from sqlite_utils import Migrations

    migrations = Migrations("creatures")

    @migrations()
    def create_table(db):
        db["creatures"].create(
            {"id": int, "name": str, "species": str},
            pk="id",
        )

    @migrations()
    def add_weight(db):
        db["creatures"].add_column("weight", float)

.. _migrations_python:

Applying migrations in Python
=============================

Once you have a ``Migrations(name)`` collection with one or more migrations registered to it, you can execute them in Python code like this:

.. code-block:: python

    from sqlite_utils import Database

    db = Database("creatures.db")
    migrations.apply(db)

Running ``migrations.apply(db)`` repeatedly is safe. Migrations that already have a matching ``migration_set`` and ``name`` row in ``_sqlite_migrations`` will be skipped.

Before applying anything, ``apply()`` reconciles the recorded history against the migrations currently registered: each record with a stored hash is compared to the current code, and a mismatch raises :class:`sqlite_utils.MigrationHistoryError` before any migration runs. You can run the same check without applying anything using ``migrations.verify(db)`` (it raises) or ``migrations.drift(db)`` (which returns the mismatches).

.. code-block:: python

    try:
        migrations.verify(db)
    except sqlite_utils.MigrationHistoryError as error:
        print(error)

Records written by hand or by older releases have no hash, so they cannot be checked - they are trusted and never rewritten. Records belonging to other migration sets are not examined or modified. See :ref:`migrations_history`.

Migration functions are applied in the order that they were registered. The function name is used as the migration name unless you pass one explicitly:

.. code-block:: python

    @migrations(name="001_create_table")
    def create_table(db):
        db["creatures"].create({"id": int, "name": str}, pk="id")

When you apply a set of migrations you can stop part way through by specifying a ``stop_before=`` migration name:

.. code-block:: python

    migrations.apply(db, stop_before="add_weight")

.. _migrations_transactions:

Migrations and transactions
===========================

Each migration runs inside a transaction, together with the ``_sqlite_migrations`` record of it having been applied. If a migration function raises an exception, everything it did is rolled back, no record is written and the migration stays pending - so fixing the error and re-applying will run that migration again from a clean state. Migrations that completed earlier in the same ``apply()`` run stay applied.

Some operations cannot run inside a transaction, for example ``VACUUM`` or changing the journal mode with ``db.enable_wal()``. Register migrations like these with ``transactional=False``:

.. code-block:: python

    @migrations(transactional=False)
    def compact(db):
        db.execute("VACUUM")

A migration registered with ``transactional=False`` runs without a wrapping transaction. A ``running`` record is committed before the function executes and becomes ``applied`` once it returns, so if the process is interrupted the migration is visibly left half-done instead of looking complete. Re-applying invokes the function again - see :ref:`migrations_history` for how to make that safe.

Avoid calling ``db.commit()`` or otherwise managing transactions manually inside a transactional migration - register the migration with ``transactional=False`` if it needs to control its own transactions. Using ``with db.atomic():`` blocks inside a migration is fine: they nest as savepoints within the migration's transaction, so the migration as a whole still commits or rolls back as a single unit. See :ref:`python_api_transactions`.

.. _migrations_history:

Checking migration history and resuming
=======================================

Migration history is more than a list of names - it is used to make repeated runs trustworthy:

1. **Code that has already run cannot silently change.** When a migration is applied, a ``content_hash`` of its code is recorded. On every later ``apply()`` (and in ``migrate --list`` output), the recorded hash is compared against the code currently registered under that migration name in that set. If the code differs - for example after a merge rewrote an existing migration - :class:`sqlite_utils.MigrationHistoryError` is raised naming the migration and both hashes, and no migrations run. Revert the change, or register the new code under a new migration name. Changing the ``transactional`` flag of an applied migration is detected the same way.
2. **New migrations run once.** A migration with a matching, hash-consistent record is skipped; pending migrations run in registration order.
3. **Other records stay untouched.** Verification only looks at the current migration set. Rows with no hash - written by hand or by older sqlite-utils / sqlite-migrate releases - are treated as applied, are not verified, and are never rewritten, even when the tracking table is upgraded with the new columns.

The read-only helpers ``migrations.drift(db)`` and ``migrations.verify(db)`` and the ``applied`` / ``pending`` methods never create tables or modify records.

Resuming an interrupted non-transactional migration
---------------------------------------------------

A non-transactional migration whose record is still ``running`` is treated as pending and invoked again on the next ``apply()``. If its function accepts a second argument, it receives a :class:`sqlite_utils.MigrationContext` which exposes the checkpoints recorded during earlier attempts:

.. code-block:: python

    @migrations(transactional=False)
    def rebuild_dogs(db, ctx):
        if not ctx.done("vacuumed"):
            db.execute("VACUUM")
            ctx.complete("vacuumed")
        if not ctx.done("reinserted"):
            db.execute("INSERT OR IGNORE INTO dogs_new SELECT ... FROM dogs")
            ctx.complete("reinserted")

``ctx.done(name)`` returns whether a checkpoint was recorded, and ``ctx.complete(name)`` records one (committed immediately). ``ctx.step(name, fn)`` is shorthand that calls ``fn()`` and then records the checkpoint when it finishes without raising. Completed steps are skipped when the migration resumes, so repeated runs converge.

Two rules keep a resumable non-transactional migration safe:

- Put a checkpoint after each distinct step, so progress is observable through ``migrations.interrupted(db)`` and ``migrate --list``.
- Make each step's own effects idempotent (``INSERT OR IGNORE``, ``CREATE TABLE IF NOT EXISTS``, existence checks). If the process dies after a step's SQL committed but before its checkpoint was recorded, that step runs again - its checkpoint can only be recorded after the work succeeds.

Checkpoints are stored in the ``_sqlite_migration_checkpoints`` table. When the function returns successfully the migration's record becomes ``applied``; its checkpoints stay in place as a record of what ran.

Applying migrations using the CLI
=================================

Run migrations using the ``sqlite-utils migrate`` command:

.. code-block:: bash

    sqlite-utils migrate creatures.db path/to/migrations.py

The first argument is the database file. The remaining arguments can be paths to migration files or directories containing migration files.

If you omit migration paths, ``sqlite-utils`` searches the current directory and subdirectories for files called ``migrations.py``:

.. code-block:: bash

    sqlite-utils migrate creatures.db

You can also pass a directory. Every ``migrations.py`` file in that directory tree will be considered:

.. code-block:: bash

    sqlite-utils migrate creatures.db path/to/project/

Running the command repeatedly is safe. Migrations that already have a matching ``migration_set`` and ``name`` row in ``_sqlite_migrations`` will be skipped.

Listing migrations
==================

Use ``--list`` to show applied and pending migrations without running them. This is a read-only operation - it will not create the database file or the ``_sqlite_migrations`` table:

.. code-block:: bash

    sqlite-utils migrate creatures.db --list

Example output:

.. code-block:: output

    Migrations for: creatures

      Applied:
        create_table - 2026-06-09 17:23:12.048092+00:00
        add_weight - 2026-06-09 17:23:12.051249+00:00

      Pending:
        add_age

A non-transactional migration left in the ``running`` state by an interruption is shown in an ``Interrupted`` section (with the checkpoints it completed) instead of under ``Pending`` - it will be resumed on the next run:

.. code-block:: output

      Interrupted:
        rebuild_dogs (will resume; steps completed: vacuumed)

If recorded migration code no longer matches the code currently registered, a ``History problems`` section names the affected migrations. Listing is read-only - it reports the drift but applies nothing.

Stopping before a migration
===========================

When applying migrations using the CLI, you can stop before a named migration:

.. code-block:: bash

    sqlite-utils migrate creatures.db path/to/migrations.py --stop-before add_weight

This applies any pending migrations before ``add_weight`` and leaves ``add_weight`` and later migrations pending. An unqualified migration name matches in any migration set.

You can also target a specific migration set using ``migration_set:migration_name``. This is useful if a migrations file contains more than one migration set, or if multiple sets use the same migration name:

.. code-block:: bash

    sqlite-utils migrate creatures.db path/to/migrations.py \
      --stop-before creatures:add_weight \
      --stop-before sales:drop_index

The ``--stop-before`` option can be passed more than once.

If a ``--stop-before`` value does not match any known migration the command exits with an error, rather than silently applying everything. Naming a migration that has already been applied is also an error - stopping before it is impossible to honor - and no pending migrations are applied.

Verbose output
==============

Use ``--verbose`` or ``-v`` to show the schema before and after migrations are applied, plus a unified diff when the schema changes:

.. code-block:: bash

    sqlite-utils migrate creatures.db --verbose

Migrating from sqlite-migrate
=============================

This system uses the same migration table format as the older `sqlite-migrate <https://github.com/simonw/sqlite-migrate>`__ package. To use existing migration files directly with ``sqlite-utils``, update their import from ``sqlite_migrate`` to ``sqlite_utils``:

.. code-block:: python

    from sqlite_utils import Migrations

    migration = Migrations("creatures")

    @migration()
    def create_table(db):
        db["creatures"].create({"id": int, "name": str}, pk="id")

Python API
==========

.. autoclass:: sqlite_utils.migrations.Migrations
   :members:
   :undoc-members:
   :exclude-members: _Migration, _AppliedMigration

.. autoclass:: sqlite_utils.migrations.MigrationContext
   :members:
   :undoc-members:

.. autoexception:: sqlite_utils.migrations.MigrationHistoryError
