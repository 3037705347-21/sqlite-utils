import pytest

from sqlite_utils.db import (
    ForeignKey,
    M2MReconcileResult,
    InvalidColumns,
    NoObviousTable,
    NoTable,
    NotFoundError,
    UnsafeRelationshipTable,
)


def test_insert_m2m_single(fresh_db):
    dogs = fresh_db.table("dogs")
    dogs.insert({"id": 1, "name": "Cleo"}, pk="id").m2m(
        "humans", {"id": 1, "name": "Natalie D"}, pk="id"
    )
    assert {"dogs_humans", "humans", "dogs"} == set(fresh_db.table_names())
    humans = fresh_db.table("humans")
    dogs_humans = fresh_db.table("dogs_humans")
    assert [{"id": 1, "name": "Natalie D"}] == list(humans.rows)
    assert [{"humans_id": 1, "dogs_id": 1}] == list(dogs_humans.rows)


def test_insert_m2m_alter(fresh_db):
    dogs = fresh_db.table("dogs")
    dogs.insert({"id": 1, "name": "Cleo"}, pk="id").m2m(
        "humans", {"id": 1, "name": "Natalie D"}, pk="id"
    )
    dogs.update(1).m2m(
        "humans", {"id": 2, "name": "Simon W", "nerd": True}, pk="id", alter=True
    )
    assert list(fresh_db.table("humans").rows) == [
        {"id": 1, "name": "Natalie D", "nerd": None},
        {"id": 2, "name": "Simon W", "nerd": 1},
    ]
    assert list(fresh_db.table("dogs_humans").rows) == [
        {"humans_id": 1, "dogs_id": 1},
        {"humans_id": 2, "dogs_id": 1},
    ]


def test_insert_m2m_list(fresh_db):
    dogs = fresh_db.table("dogs")
    dogs.insert({"id": 1, "name": "Cleo"}, pk="id").m2m(
        "humans",
        [{"id": 1, "name": "Natalie D"}, {"id": 2, "name": "Simon W"}],
        pk="id",
    )
    assert {"dogs", "humans", "dogs_humans"} == set(fresh_db.table_names())
    humans = fresh_db.table("humans")
    dogs_humans = fresh_db.table("dogs_humans")
    assert [{"humans_id": 1, "dogs_id": 1}, {"humans_id": 2, "dogs_id": 1}] == list(
        dogs_humans.rows
    )
    assert [{"id": 1, "name": "Natalie D"}, {"id": 2, "name": "Simon W"}] == list(
        humans.rows
    )
    assert [
        ForeignKey(
            table="dogs_humans", column="dogs_id", other_table="dogs", other_column="id"
        ),
        ForeignKey(
            table="dogs_humans",
            column="humans_id",
            other_table="humans",
            other_column="id",
        ),
    ] == dogs_humans.foreign_keys


def test_insert_m2m_iterable(fresh_db):
    iterable_records = ({"id": 1, "name": "Phineas"}, {"id": 2, "name": "Ferb"})

    def iterable():
        yield from iterable_records

    platypuses = fresh_db.table("platypuses")
    platypuses.insert({"id": 1, "name": "Perry"}, pk="id").m2m(
        "humans",
        iterable(),
        pk="id",
    )

    assert {"platypuses", "humans", "humans_platypuses"} == set(fresh_db.table_names())
    humans = fresh_db.table("humans")
    humans_platypuses = fresh_db.table("humans_platypuses")
    assert [
        {"humans_id": 1, "platypuses_id": 1},
        {"humans_id": 2, "platypuses_id": 1},
    ] == list(humans_platypuses.rows)
    assert [{"id": 1, "name": "Phineas"}, {"id": 2, "name": "Ferb"}] == list(
        humans.rows
    )
    assert [
        ForeignKey(
            table="humans_platypuses",
            column="platypuses_id",
            other_table="platypuses",
            other_column="id",
        ),
        ForeignKey(
            table="humans_platypuses",
            column="humans_id",
            other_table="humans",
            other_column="id",
        ),
    ] == humans_platypuses.foreign_keys


def test_m2m_with_table_objects(fresh_db):
    dogs = fresh_db.table("dogs", pk="id")
    humans = fresh_db.table("humans", pk="id")
    dogs.insert({"id": 1, "name": "Cleo"}).m2m(
        humans, [{"id": 1, "name": "Natalie D"}, {"id": 2, "name": "Simon W"}]
    )
    expected_tables = {"dogs", "humans", "dogs_humans"}
    assert expected_tables == set(fresh_db.table_names())
    assert dogs.count == 1
    assert humans.count == 2
    assert fresh_db.table("dogs_humans").count == 2


def test_m2m_lookup(fresh_db):
    people = fresh_db.table("people", pk="id")
    people.insert({"name": "Wahyu"}).m2m("tags", lookup={"tag": "Coworker"})
    people_tags = fresh_db.table("people_tags")
    tags = fresh_db.table("tags")
    assert people_tags.exists()
    assert tags.exists()
    assert [
        ForeignKey(
            table="people_tags",
            column="people_id",
            other_table="people",
            other_column="id",
        ),
        ForeignKey(
            table="people_tags", column="tags_id", other_table="tags", other_column="id"
        ),
    ] == people_tags.foreign_keys
    assert [{"people_id": 1, "tags_id": 1}] == list(people_tags.rows)
    assert [{"id": 1, "name": "Wahyu"}] == list(people.rows)
    assert [{"id": 1, "tag": "Coworker"}] == list(tags.rows)


def test_m2m_requires_either_records_or_lookup(fresh_db):
    people = fresh_db.table("people", pk="id").insert({"name": "Wahyu"})
    with pytest.raises(ValueError):
        people.m2m("tags")
    with pytest.raises(ValueError):
        people.m2m("tags", {"tag": "hello"}, lookup={"foo": "bar"})


def test_m2m_explicit_table_name_argument(fresh_db):
    people = fresh_db.table("people", pk="id")
    people.insert({"name": "Wahyu"}).m2m(
        "tags", lookup={"tag": "Coworker"}, m2m_table="tagged"
    )
    assert fresh_db.table("tags").exists
    assert fresh_db.table("tagged").exists
    assert not fresh_db.table("people_tags").exists()


def test_m2m_table_candidates(fresh_db):
    fresh_db.create_table("one", {"id": int, "name": str}, pk="id")
    fresh_db.create_table("two", {"id": int, "name": str}, pk="id")
    fresh_db.create_table("three", {"id": int, "name": str}, pk="id")
    # No candidates at first
    assert [] == fresh_db.m2m_table_candidates("one", "two")
    # Create a candidate
    fresh_db.create_table(
        "one_m2m_two", {"one_id": int, "two_id": int}, foreign_keys=["one_id", "two_id"]
    )
    assert ["one_m2m_two"] == fresh_db.m2m_table_candidates("one", "two")
    # Add another table and there should be two candidates
    fresh_db.create_table(
        "one_m2m_two_and_three",
        {"one_id": int, "two_id": int, "three_id": int},
        foreign_keys=["one_id", "two_id", "three_id"],
    )
    assert {"one_m2m_two", "one_m2m_two_and_three"} == set(
        fresh_db.m2m_table_candidates("one", "two")
    )


def test_uses_existing_m2m_table_if_exists(fresh_db):
    # Code should look for an existing table with fks to both tables
    # and use that if it exists.
    people = fresh_db.create_table("people", {"id": int, "name": str}, pk="id")
    fresh_db.table("tags").lookup({"tag": "Coworker"})
    fresh_db.create_table(
        "tagged",
        {"people_id": int, "tags_id": int},
        foreign_keys=["people_id", "tags_id"],
    )
    people.insert({"name": "Wahyu"}).m2m("tags", lookup={"tag": "Coworker"})
    assert fresh_db.table("tags").exists()
    assert fresh_db.table("tagged").exists()
    assert not fresh_db.table("people_tags").exists()
    assert not fresh_db.table("tags_people").exists()
    assert [{"people_id": 1, "tags_id": 1}] == list(fresh_db.table("tagged").rows)


def test_requires_explicit_m2m_table_if_multiple_options(fresh_db):
    # If the code scans for m2m tables and finds more than one candidate
    # it should require that the m2m_table=x argument is used
    people = fresh_db.create_table("people", {"id": int, "name": str}, pk="id")
    fresh_db.table("tags").lookup({"tag": "Coworker"})
    fresh_db.create_table(
        "tagged",
        {"people_id": int, "tags_id": int},
        foreign_keys=["people_id", "tags_id"],
    )
    fresh_db.create_table(
        "tagged2",
        {"people_id": int, "tags_id": int},
        foreign_keys=["people_id", "tags_id"],
    )
    with pytest.raises(NoObviousTable):
        people.insert({"name": "Wahyu"}).m2m("tags", lookup={"tag": "Coworker"})


@pytest.fixture
def dogs_and_humans(fresh_db):
    fresh_db["dogs"].insert_all(
        [
            {"id": 1, "name": "Cleo"},
            {"id": 2, "name": "Pancakes"},
        ],
        pk="id",
    )
    fresh_db["humans"].insert_all(
        [
            {"id": 1, "name": "Natalie"},
            {"id": 2, "name": "Simon"},
        ],
        pk="id",
    )
    return fresh_db


def test_m2m_reconcile_first_run_creates_table_and_links(dogs_and_humans):
    db = dogs_and_humans
    result = db["dogs"].m2m_reconcile("humans", [(1, 1), (1, 2)])
    assert isinstance(result, M2MReconcileResult)
    assert result == M2MReconcileResult(
        m2m_table="dogs_humans",
        managed_by="sync",
        desired=2,
        added=2,
        removed=0,
        unchanged=0,
        unclaimed=0,
    )
    rows = list(db["dogs_humans"].rows)
    assert rows == [
        {"dogs_id": 1, "humans_id": 1, "managed_by": "sync"},
        {"dogs_id": 1, "humans_id": 2, "managed_by": "sync"},
    ]
    # Link table should keep foreign keys to both sides
    assert sorted(
        [
            ForeignKey(
                table="dogs_humans",
                column="dogs_id",
                other_table="dogs",
                other_column="id",
            ),
            ForeignKey(
                table="dogs_humans",
                column="humans_id",
                other_table="humans",
                other_column="id",
            ),
        ]
    ) == sorted(db["dogs_humans"].foreign_keys)


def test_m2m_reconcile_is_idempotent(dogs_and_humans):
    db = dogs_and_humans
    pairs = [(1, 1), (1, 2)]
    db["dogs"].m2m_reconcile("humans", pairs)
    result = db["dogs"].m2m_reconcile("humans", pairs)
    assert result.added == 0
    assert result.removed == 0
    assert result.unchanged == 2
    assert list(db["dogs_humans"].rows) == [
        {"dogs_id": 1, "humans_id": 1, "managed_by": "sync"},
        {"dogs_id": 1, "humans_id": 2, "managed_by": "sync"},
    ]


def test_m2m_reconcile_adds_and_removes_changed_pairs(dogs_and_humans):
    db = dogs_and_humans
    db["dogs"].m2m_reconcile("humans", [(1, 1), (1, 2)])
    # (1,2) dropped from source, (2,1) newly appears
    result = db["dogs"].m2m_reconcile("humans", [(1, 1), (2, 1)])
    assert result.added == 1
    assert result.removed == 1
    assert result.unchanged == 1
    assert list(db["dogs_humans"].rows) == [
        {"dogs_id": 1, "humans_id": 1, "managed_by": "sync"},
        {"dogs_id": 2, "humans_id": 1, "managed_by": "sync"},
    ]


def test_m2m_reconcile_preserves_manual_rows_across_runs(dogs_and_humans):
    db = dogs_and_humans
    db["dogs"].m2m_reconcile("humans", [(1, 1)])
    # A manually maintained link - managed_by is NULL
    db.execute(
        "INSERT INTO dogs_humans (dogs_id, humans_id) VALUES (2, 2)"
    )
    snapshot = list(db["dogs_humans"].rows)
    # Rerun several times with sources that do not include the manual pair
    for pairs in [[], [(1, 1)], [(2, 1)], []]:
        result = db["dogs"].m2m_reconcile("humans", pairs)
        manual_row = [
            row
            for row in db["dogs_humans"].rows
            if row["dogs_id"] == 2 and row["humans_id"] == 2
        ]
        assert manual_row == [
            {"dogs_id": 2, "humans_id": 2, "managed_by": None}
        ]
    assert snapshot[1] == {"dogs_id": 2, "humans_id": 2, "managed_by": None}


def test_m2m_reconcile_preserves_rows_owned_by_other_tag(dogs_and_humans):
    db = dogs_and_humans
    db["dogs"].m2m_reconcile(
        "humans", [(1, 1), (1, 2)], managed_by="file_a"
    )
    # This run manages a different tag
    result = db["dogs"].m2m_reconcile(
        "humans", [(2, 1)], managed_by="file_b"
    )
    assert result.added == 1
    assert result.removed == 0
    # file_a rows must be untouched even though not in file_b's source
    assert list(db["dogs_humans"].rows) == [
        {"dogs_id": 1, "humans_id": 1, "managed_by": "file_a"},
        {"dogs_id": 1, "humans_id": 2, "managed_by": "file_a"},
        {"dogs_id": 2, "humans_id": 1, "managed_by": "file_b"},
    ]
    # And file_a's own rerun still only touches file_a rows
    again = db["dogs"].m2m_reconcile(
        "humans", [(1, 1)], managed_by="file_a"
    )
    assert again.removed == 1
    assert list(db["dogs_humans"].rows) == [
        {"dogs_id": 1, "humans_id": 1, "managed_by": "file_a"},
        {"dogs_id": 2, "humans_id": 1, "managed_by": "file_b"},
    ]


def test_m2m_reconcile_empty_source_clears_only_managed(dogs_and_humans):
    db = dogs_and_humans
    db["dogs"].m2m_reconcile("humans", [(1, 1), (1, 2)])
    db.execute(
        "INSERT INTO dogs_humans (dogs_id, humans_id) VALUES (2, 2)"
    )
    result = db["dogs"].m2m_reconcile("humans", [])
    assert result.desired == 0
    assert result.removed == 2
    assert list(db["dogs_humans"].rows) == [
        {"dogs_id": 2, "humans_id": 2, "managed_by": None}
    ]


def test_m2m_reconcile_deduplicates_source_pairs(dogs_and_humans):
    db = dogs_and_humans
    result = db["dogs"].m2m_reconcile(
        "humans", [(1, 1), (1, 1), [1, 1]]
    )
    assert result.desired == 1
    assert result.added == 1
    assert db["dogs_humans"].count == 1


def test_m2m_reconcile_reports_unclaimed_existing_rows(dogs_and_humans):
    db = dogs_and_humans
    # Pair exists manually first
    db["dogs"].m2m_reconcile("humans", [])
    db.execute(
        "INSERT INTO dogs_humans (dogs_id, humans_id) VALUES (1, 1)"
    )
    # Source now wants the same pair - it must not claim ownership
    result = db["dogs"].m2m_reconcile("humans", [(1, 1)])
    assert result.added == 0
    assert result.unclaimed == 1
    assert list(db["dogs_humans"].rows) == [
        {"dogs_id": 1, "humans_id": 1, "managed_by": None}
    ]
    # Dropping it from the source therefore does not remove it
    next_result = db["dogs"].m2m_reconcile("humans", [])
    assert next_result.removed == 0
    assert db["dogs_humans"].count == 1


def test_m2m_reconcile_adds_marker_column_to_old_table(dogs_and_humans):
    db = dogs_and_humans
    # Simulate a link table created by an older pipeline - no marker column
    db.execute(
        """
        CREATE TABLE dogs_humans (
            dogs_id INTEGER NOT NULL REFERENCES dogs(id),
            humans_id INTEGER NOT NULL REFERENCES humans(id),
            PRIMARY KEY (dogs_id, humans_id)
        )
        """
    )
    db.execute("INSERT INTO dogs_humans (dogs_id, humans_id) VALUES (1, 1)")
    result = db["dogs"].m2m_reconcile("humans", [(2, 2)])
    # Old row treated as manual and preserved even though not in source
    assert result.added == 1
    assert result.removed == 0
    assert list(db["dogs_humans"].rows) == [
        {"dogs_id": 1, "humans_id": 1, "managed_by": None},
        {"dogs_id": 2, "humans_id": 2, "managed_by": "sync"},
    ]


def test_m2m_reconcile_multiple_candidate_tables(dogs_and_humans):
    db = dogs_and_humans
    db.create_table(
        "tagged",
        {"dogs_id": int, "humans_id": int},
        pk=("dogs_id", "humans_id"),
        foreign_keys=["dogs_id", "humans_id"],
    )
    db.create_table(
        "tagged2",
        {"dogs_id": int, "humans_id": int},
        pk=("dogs_id", "humans_id"),
        foreign_keys=["dogs_id", "humans_id"],
    )
    with pytest.raises(NoObviousTable):
        db["dogs"].m2m_reconcile("humans", [(1, 1)])
    # Explicit name resolves the ambiguity
    result = db["dogs"].m2m_reconcile(
        "humans", [(1, 1)], m2m_table="tagged"
    )
    assert result.m2m_table == "tagged"
    assert db["tagged"].count == 1
    assert db["tagged2"].count == 0


def test_m2m_reconcile_missing_record_either_side(dogs_and_humans):
    db = dogs_and_humans
    # Missing on the other side - and no link table should be left behind
    with pytest.raises(NotFoundError) as excinfo:
        db["dogs"].m2m_reconcile("humans", [(1, 1), (1, 9)])
    assert "humans" in str(excinfo.value)
    assert "9" in str(excinfo.value)
    assert "dogs_humans" not in db.table_names()

    # Missing on this side
    db["dogs"].m2m_reconcile("humans", [(1, 1)])
    before = list(db["dogs_humans"].rows)
    with pytest.raises(NotFoundError) as excinfo:
        db["dogs"].m2m_reconcile("humans", [(1, 1), (9, 1)])
    assert "dogs" in str(excinfo.value)
    # Whole run rolled back
    assert list(db["dogs_humans"].rows) == before


def test_m2m_reconcile_unsafe_table_without_pair_pk(dogs_and_humans):
    db = dogs_and_humans
    # Link table whose primary key is not the pair - duplicates possible
    db.execute(
        """
        CREATE TABLE dogs_humans (
            id INTEGER PRIMARY KEY,
            dogs_id INTEGER REFERENCES dogs(id),
            humans_id INTEGER REFERENCES humans(id)
        )
        """
    )
    db.execute("INSERT INTO dogs_humans (dogs_id, humans_id) VALUES (1, 1)")
    with pytest.raises(UnsafeRelationshipTable):
        db["dogs"].m2m_reconcile("humans", [(1, 1)])
    # Nothing changed - no marker column added either
    assert [
        {"id": 1, "dogs_id": 1, "humans_id": 1}
    ] == list(db["dogs_humans"].rows)


def test_m2m_reconcile_ambiguous_key_match(dogs_and_humans):
    db = dogs_and_humans
    # Two dogs with the same name - resolving pairs by name is ambiguous
    db["dogs"].insert({"id": 3, "name": "Cleo"}, pk="id")
    with pytest.raises(UnsafeRelationshipTable):
        db["dogs"].m2m_reconcile(
            "humans", [("Cleo", "Natalie")], left_on="name", right_on="name"
        )


def test_m2m_reconcile_resolves_natural_keys(dogs_and_humans):
    db = dogs_and_humans
    result = db["dogs"].m2m_reconcile(
        "humans",
        [("Cleo", "Natalie"), ("Cleo", "Simon")],
        left_on="name",
        right_on="name",
    )
    assert result.added == 2
    # Link table stores the resolved primary keys
    assert list(db["dogs_humans"].rows) == [
        {"dogs_id": 1, "humans_id": 1, "managed_by": "sync"},
        {"dogs_id": 1, "humans_id": 2, "managed_by": "sync"},
    ]


def test_m2m_reconcile_existing_table_with_fk_to_non_pk_column(fresh_db):
    fresh_db["dogs"].insert_all(
        [{"id": 1, "name": "Cleo"}], pk="id"
    )
    fresh_db["humans"].insert_all(
        [{"id": 10, "badge": "B1", "name": "Natalie"}],
        pk="id",
    )
    fresh_db.execute(
        "CREATE UNIQUE INDEX humans_badge_idx ON humans(badge)"
    )
    # Link table references humans(badge) rather than humans(id)
    fresh_db.execute(
        """
        CREATE TABLE dogs_humans (
            dogs_id INTEGER NOT NULL REFERENCES dogs(id),
            humans_badge TEXT NOT NULL REFERENCES humans(badge),
            PRIMARY KEY (dogs_id, humans_badge)
        )
        """
    )
    result = fresh_db["dogs"].m2m_reconcile("humans", [(1, "B1")])
    assert result.added == 1
    assert list(fresh_db["dogs_humans"].rows) == [
        {"dogs_id": 1, "humans_badge": "B1", "managed_by": "sync"}
    ]
    # Rerun is idempotent
    again = fresh_db["dogs"].m2m_reconcile("humans", [(1, "B1")])
    assert again.added == 0
    assert again.removed == 0
    # Removing the pair from source cleans it up
    gone = fresh_db["dogs"].m2m_reconcile("humans", [])
    assert gone.removed == 1
    assert fresh_db["dogs_humans"].count == 0


def test_m2m_reconcile_compound_keys(fresh_db):
    fresh_db.executescript(
        """
        CREATE TABLE a (x INTEGER, y INTEGER, name TEXT, PRIMARY KEY (x, y));
        CREATE TABLE b (p INTEGER, q INTEGER, name TEXT, PRIMARY KEY (p, q));
        CREATE TABLE a_b (
            a_x INTEGER, a_y INTEGER, b_p INTEGER, b_q INTEGER,
            PRIMARY KEY (a_x, a_y, b_p, b_q),
            FOREIGN KEY (a_x, a_y) REFERENCES a(x, y),
            FOREIGN KEY (b_p, b_q) REFERENCES b(p, q)
        );
        """
    )
    fresh_db.execute(
        "INSERT INTO a (x, y) VALUES (1, 2), (3, 4)"
    )
    fresh_db.execute(
        "INSERT INTO b (p, q) VALUES (5, 6), (7, 8)"
    )
    result = fresh_db["a"].m2m_reconcile(
        "b", [((1, 2), (5, 6)), ((3, 4), (7, 8))]
    )
    assert result.added == 2
    assert list(fresh_db["a_b"].rows) == [
        {"a_x": 1, "a_y": 2, "b_p": 5, "b_q": 6, "managed_by": "sync"},
        {"a_x": 3, "a_y": 4, "b_p": 7, "b_q": 8, "managed_by": "sync"},
    ]
    # Idempotent, then drop one pair
    assert (
        fresh_db["a"].m2m_reconcile(
            "b", [((1, 2), (5, 6)), ((3, 4), (7, 8))]
        ).added
        == 0
    )
    changed = fresh_db["a"].m2m_reconcile(
        "b", [((1, 2), (5, 6))]
    )
    assert changed.removed == 1
    assert fresh_db["a_b"].count == 1
    # Missing key on a compound side
    with pytest.raises(NotFoundError):
        fresh_db["a"].m2m_reconcile("b", [((1, 2), (9, 9))])


def test_m2m_reconcile_interrupted_run_rolls_back(dogs_and_humans, monkeypatch):
    import sqlite_utils.db as db_module

    db = dogs_and_humans
    db["dogs"].m2m_reconcile("humans", [(1, 1), (1, 2)])
    state_before = list(db["dogs_humans"].rows)

    # Simulate an interruption right when the result would be built, after
    # the delete/insert statements have already executed but before commit
    real_result = db_module.M2MReconcileResult

    def fail_result(*args, **kwargs):
        raise KeyboardInterrupt("simulated interruption")

    monkeypatch.setattr(db_module, "M2MReconcileResult", fail_result)
    # (1,2) removed, (2,1) added - interruption hits before commit
    with pytest.raises(KeyboardInterrupt):
        db["dogs"].m2m_reconcile("humans", [(1, 1), (2, 1)])
    # No half-set: the table is exactly as before the run
    assert list(db["dogs_humans"].rows) == state_before
    monkeypatch.undo()
    # Rerunning converges onto the desired state
    result = db["dogs"].m2m_reconcile("humans", [(1, 1), (2, 1)])
    assert isinstance(result, real_result)
    assert result.added == 1
    assert result.removed == 1
    assert list(db["dogs_humans"].rows) == [
        {"dogs_id": 1, "humans_id": 1, "managed_by": "sync"},
        {"dogs_id": 2, "humans_id": 1, "managed_by": "sync"},
    ]


def test_m2m_reconcile_with_foreign_keys_pragma(dogs_and_humans):
    db = dogs_and_humans
    db.execute("PRAGMA foreign_keys=ON")
    result = db["dogs"].m2m_reconcile("humans", [(1, 1), (2, 2)])
    assert result.added == 2
    assert db["dogs_humans"].count == 2


def test_m2m_reconcile_invalid_arguments(fresh_db):
    fresh_db["dogs"].insert({"id": 1}, pk="id")
    fresh_db["humans"].insert({"id": 1}, pk="id")
    # Bad tag
    with pytest.raises(ValueError):
        fresh_db["dogs"].m2m_reconcile("humans", [(1, 1)], managed_by="")
    # Pairs must be 2-tuples (2-element lists are accepted too)
    with pytest.raises(ValueError):
        fresh_db["dogs"].m2m_reconcile("humans", [[1]])
    with pytest.raises(ValueError):
        fresh_db["dogs"].m2m_reconcile("humans", [(1, 1, 1)])
    # Single-column side but multi-value key
    with pytest.raises(ValueError):
        fresh_db["dogs"].m2m_reconcile("humans", [((1, 2), 1)])


def test_m2m_reconcile_missing_side_tables(fresh_db):
    fresh_db["dogs"].insert({"id": 1}, pk="id")
    with pytest.raises(NoTable):
        fresh_db["dogs"].m2m_reconcile("humans", [(1, 1)])


def test_m2m_reconcile_invalid_key_column(dogs_and_humans):
    db = dogs_and_humans
    with pytest.raises(InvalidColumns):
        db["dogs"].m2m_reconcile(
            "humans", [(1, 1)], left_on="does_not_exist"
        )
    with pytest.raises(InvalidColumns):
        db["dogs"].m2m_reconcile(
            "humans", [(1, 1)], right_on="does_not_exist"
        )
