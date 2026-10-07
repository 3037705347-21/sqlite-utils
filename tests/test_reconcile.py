import pytest
from click.testing import CliRunner

from sqlite_utils import cli
from sqlite_utils.db import InvalidColumns, NoTable, ReconcileConflict

REGISTRY = "_sqlite_utils_extract_rows"


def setup_items(db, rows):
    """Create items(id, category), extract category, then put the raw
    column back populated with the current source values."""
    db["items"].insert_all(rows, pk="id")
    db["items"].extract("category")
    reimport(db, {row["id"]: row["category"] for row in rows})
    return db["items"]


def reimport(db, values_by_id, column="category"):
    """Simulate re-importing the raw source data with --alter."""
    items = db["items"]
    if column not in items.columns_dict:
        items.add_column(column, str)
    for pk, value in values_by_id.items():
        db.execute(f"UPDATE items SET {column} = ? WHERE id = ?", [value, pk])


def managed_ids(db, table="category"):
    return {
        row[0]
        for row in db.execute(
            f'SELECT lookup_id FROM "{REGISTRY}" WHERE lookup_table = ?',
            [table],
        ).fetchall()
    }


def test_extract_does_not_create_registry(fresh_db):
    # The one-shot extract() usage must remain completely unchanged
    fresh_db["items"].insert_all(
        [{"id": 1, "category": "Fish"}, {"id": 2, "category": "Fish"}], pk="id"
    )
    fresh_db["items"].extract("category")
    assert REGISTRY not in fresh_db.table_names()
    assert "_sqlite_utils_extract_lookups" not in fresh_db.table_names()


def test_reconcile_noop_is_idempotent(fresh_db):
    setup_items(
        fresh_db,
        [
            {"id": 1, "category": "Fish"},
            {"id": 2, "category": "Fruit"},
            {"id": 3, "category": "Fish"},
        ],
    )
    snapshot = list(fresh_db["category"].rows)
    fk_snapshot = list(fresh_db["items"].rows)
    fresh_db["items"].reconcile("category")
    fresh_db["items"].reconcile("category")
    assert list(fresh_db["category"].rows) == snapshot
    assert list(fresh_db["items"].rows) == fk_snapshot
    # Raw column is retained so the reconciliation can be repeated
    assert "category" in fresh_db["items"].columns_dict


def test_reconcile_adds_new_value_with_new_id(fresh_db):
    setup_items(
        fresh_db,
        [{"id": 1, "category": "Fish"}, {"id": 2, "category": "Fruit"}],
    )
    fresh_db["items"].reconcile("category")
    reimport(fresh_db, {1: "Fish", 2: "Fruit", 3: "Meat"})
    fresh_db.execute("INSERT INTO items (id, category) VALUES (3, 'Meat')")
    fresh_db["items"].reconcile("category")
    assert list(fresh_db["category"].rows) == [
        {"id": 1, "category": "Fish"},
        {"id": 2, "category": "Fruit"},
        {"id": 3, "category": "Meat"},
    ]
    assert list(fresh_db["items"].rows_where("id = 3")) == [
        {"id": 3, "category": "Meat", "category_id": 3}
    ]
    assert managed_ids(fresh_db) == {1, 2, 3}


def test_reconcile_rename_reuses_identity(fresh_db):
    setup_items(
        fresh_db,
        [
            {"id": 1, "category": "Fish"},
            {"id": 2, "category": "Fruit"},
            {"id": 3, "category": "Fish"},
        ],
    )
    fresh_db["items"].reconcile("category")
    # Fish -> Seafood: exactly one managed value gone, one new value appears
    reimport(fresh_db, {1: "Seafood", 2: "Fruit", 3: "Seafood"})
    fresh_db["items"].reconcile("category")
    # ID 1 is reused, only the value changed
    assert list(fresh_db["category"].rows) == [
        {"id": 1, "category": "Seafood"},
        {"id": 2, "category": "Fruit"},
    ]
    assert list(fresh_db["items"].rows) == [
        {"id": 1, "category": "Seafood", "category_id": 1},
        {"id": 2, "category": "Fruit", "category_id": 2},
        {"id": 3, "category": "Seafood", "category_id": 1},
    ]
    assert managed_ids(fresh_db) == {1, 2}


def test_reconcile_rename_external_references_follow(fresh_db):
    setup_items(
        fresh_db,
        [{"id": 1, "category": "Fish"}, {"id": 2, "category": "Fruit"}],
    )
    fresh_db["orders"].create(
        {"id": int, "category_id": int},
        pk="id",
        foreign_keys=[("category_id", "category", "id")],
    )
    fresh_db["orders"].insert({"id": 10, "category_id": 1})
    fresh_db["items"].reconcile("category")
    reimport(fresh_db, {1: "Seafood", 2: "Fruit"})
    fresh_db["items"].reconcile("category")
    # The external reference keeps pointing at the same identity, which now
    # carries the new name
    assert fresh_db.execute(
        "SELECT orders.id, category.category FROM orders JOIN category "
        "ON orders.category_id = category.id"
    ).fetchall() == [(10, "Seafood")]


def test_reconcile_first_run_protects_unreferenced_rows(fresh_db):
    # Baseline run: only rows this table's foreign key references are
    # managed; unreferenced rows are treated as manually maintained
    fresh_db["items"].insert_all([{"id": 1, "category": "Fish"}], pk="id")
    fresh_db["items"].extract("category")
    fresh_db["category"].insert({"id": 5, "category": "Old stuff"})
    reimport(fresh_db, {1: "Fish"})
    fresh_db["items"].reconcile("category")
    assert list(fresh_db["category"].rows) == [
        {"id": 1, "category": "Fish"},
        {"id": 5, "category": "Old stuff"},
    ]
    # Only the referenced row matching a current value is managed
    assert managed_ids(fresh_db) == {1}


def test_reconcile_first_run_can_rename_and_delete(fresh_db):
    # The very first run already sees renames and deletions, because the
    # extracted foreign keys tell us which rows it manages
    fresh_db["items"].insert_all(
        [
            {"id": 1, "category": "Fish"},
            {"id": 2, "category": "Fruit"},
            {"id": 3, "category": "Meat"},
        ],
        pk="id",
    )
    fresh_db["items"].extract("category")
    fresh_db["category"].insert({"id": 99, "category": "Manual"})
    reimport(fresh_db, {1: "Seafood", 2: "Fruit", 3: None})
    # Fish->Seafood is a rename, Meat simply disappears: spell out the
    # rename so the mapping is unambiguous
    fresh_db["items"].reconcile("category", renames={"Fish": "Seafood"})
    assert list(fresh_db["category"].rows) == [
        {"id": 1, "category": "Seafood"},
        {"id": 2, "category": "Fruit"},
        {"id": 99, "category": "Manual"},
    ]
    assert managed_ids(fresh_db) == {1, 2}
    assert fresh_db.execute(
        "SELECT id, category_id FROM items ORDER BY id"
    ).fetchall() == [(1, 1), (2, 2), (3, None)]


def test_reconcile_deletes_managed_value_that_disappeared(fresh_db):
    setup_items(
        fresh_db,
        [{"id": 1, "category": "Fish"}, {"id": 2, "category": "Fruit"}],
    )
    fresh_db["items"].reconcile("category")
    reimport(fresh_db, {1: "Fish", 2: "Fish"})
    fresh_db["items"].reconcile("category")
    assert list(fresh_db["category"].rows) == [{"id": 1, "category": "Fish"}]
    assert list(fresh_db["items"].rows) == [
        {"id": 1, "category": "Fish", "category_id": 1},
        {"id": 2, "category": "Fish", "category_id": 1},
    ]
    assert managed_ids(fresh_db) == {1}


def test_reconcile_never_deletes_manual_rows(fresh_db):
    setup_items(
        fresh_db,
        [{"id": 1, "category": "Fish"}, {"id": 2, "category": "Fruit"}],
    )
    # A manually maintained lookup row, unknown to the source data
    fresh_db["category"].insert({"id": 99, "category": "Manual drafts"})
    fresh_db["items"].reconcile("category")
    reimport(fresh_db, {1: "Fish", 2: "Fish"})
    fresh_db["items"].reconcile("category")
    # Fruit was managed and disappeared so it is cleaned up; the manual row
    # survives even though nothing references it
    assert list(fresh_db["category"].rows) == [
        {"id": 1, "category": "Fish"},
        {"id": 99, "category": "Manual drafts"},
    ]
    assert 99 not in managed_ids(fresh_db)


def test_reconcile_manual_row_used_by_source_stays_unmanaged(fresh_db):
    # A manual row that the source starts referencing must not be adopted:
    # when the source stops using it later it must still survive
    setup_items(fresh_db, [{"id": 1, "category": "Fish"}])
    fresh_db["category"].insert({"id": 99, "category": "Custom"})
    fresh_db["items"].reconcile("category")
    reimport(fresh_db, {1: "Custom"})
    fresh_db["items"].reconcile("category")
    assert fresh_db.execute(
        "SELECT category_id FROM items WHERE id = 1"
    ).fetchone() == (99,)
    assert 99 not in managed_ids(fresh_db)
    reimport(fresh_db, {1: "Fish"})
    fresh_db["items"].reconcile("category")
    # Fish could not reuse the manual row's identity - it gets a fresh one
    assert list(fresh_db["category"].rows) == [
        {"id": 99, "category": "Custom"},
        {"id": 100, "category": "Fish"},
    ]
    assert fresh_db.execute(
        "SELECT category_id FROM items WHERE id = 1"
    ).fetchone() == (100,)
    assert 99 not in managed_ids(fresh_db)


@pytest.mark.parametrize(
    "start,mapping",
    [
        # 2 removed, 2 added
        (
            [
                {"id": 1, "category": "Fish"},
                {"id": 2, "category": "Fruit"},
            ],
            {1: "Seafood", 2: "Veg"},
        ),
        # 1 removed, 2 added
        (
            [
                {"id": 1, "category": "Fish"},
                {"id": 2, "category": "Fruit"},
                {"id": 3, "category": "Meat"},
                {"id": 4, "category": "Fish"},
            ],
            {1: "Fish", 2: "Fruit", 3: "A", 4: "B"},
        ),
        # 2 removed, 1 added (row 3 no longer carries a value)
        (
            [
                {"id": 1, "category": "Fish"},
                {"id": 2, "category": "Fruit"},
                {"id": 3, "category": "Meat"},
            ],
            {1: "Fish", 2: "Seafood", 3: None},
        ),
    ],
)
def test_reconcile_ambiguous_mapping_fails_atomically(fresh_db, start, mapping):
    setup_items(fresh_db, start)
    fresh_db["items"].reconcile("category")
    reimport(fresh_db, mapping)
    lookup_before = list(fresh_db["category"].rows)
    fk_before = fresh_db.execute(
        "SELECT id, category_id FROM items ORDER BY id"
    ).fetchall()
    with pytest.raises(ReconcileConflict, match="ambiguous"):
        fresh_db["items"].reconcile("category")
    # Whole operation failed: nothing changed
    assert list(fresh_db["category"].rows) == lookup_before
    assert (
        fresh_db.execute("SELECT id, category_id FROM items ORDER BY id").fetchall()
        == fk_before
    )


def test_reconcile_explicit_renames_resolve_ambiguity(fresh_db):
    setup_items(
        fresh_db,
        [{"id": 1, "category": "Fish"}, {"id": 2, "category": "Fruit"}],
    )
    fresh_db["items"].reconcile("category")
    reimport(fresh_db, {1: "Seafood", 2: "Veg"})
    fresh_db["items"].reconcile("category", renames={"Fish": "Seafood", "Fruit": "Veg"})
    assert list(fresh_db["category"].rows) == [
        {"id": 1, "category": "Seafood"},
        {"id": 2, "category": "Veg"},
    ]
    assert list(fresh_db["items"].rows) == [
        {"id": 1, "category": "Seafood", "category_id": 1},
        {"id": 2, "category": "Veg", "category_id": 2},
    ]


def test_reconcile_explicit_renames_mixed_with_insert_and_delete(fresh_db):
    setup_items(
        fresh_db,
        [
            {"id": 1, "category": "Fish"},
            {"id": 2, "category": "Fruit"},
            {"id": 3, "category": "Meat"},
        ],
    )
    fresh_db["items"].reconcile("category")
    # Fruit renames to Seafood (id reused), Meat is explicitly deleted,
    # Veg is inserted
    reimport(fresh_db, {1: "Fish", 2: "Seafood", 3: "Veg"})
    fresh_db["items"].reconcile(
        "category", renames={"Fruit": "Seafood"}, deletes=["Meat"]
    )
    assert list(fresh_db["category"].rows) == [
        {"id": 1, "category": "Fish"},
        {"id": 2, "category": "Seafood"},
        {"id": 4, "category": "Veg"},
    ]
    assert list(fresh_db["items"].rows) == [
        {"id": 1, "category": "Fish", "category_id": 1},
        {"id": 2, "category": "Seafood", "category_id": 2},
        {"id": 3, "category": "Veg", "category_id": 4},
    ]
    assert managed_ids(fresh_db) == {1, 2, 4}


def test_reconcile_explicit_deletes_resolve_ambiguity(fresh_db):
    # 3 removed, 2 added: spell out one rename and two deletions to make
    # the mapping unique
    setup_items(
        fresh_db,
        [
            {"id": 1, "category": "Fish"},
            {"id": 2, "category": "Fruit"},
            {"id": 3, "category": "Meat"},
        ],
    )
    fresh_db["items"].reconcile("category")
    reimport(fresh_db, {1: "A", 2: "B", 3: "B"})
    with pytest.raises(ReconcileConflict, match="ambiguous"):
        fresh_db["items"].reconcile("category", renames={"Fish": "A"})
    fresh_db["items"].reconcile(
        "category",
        renames={"Fish": "A"},
        deletes=["Fruit", "Meat"],
    )
    assert list(fresh_db["category"].rows) == [
        {"id": 1, "category": "A"},
        {"id": 4, "category": "B"},
    ]
    assert managed_ids(fresh_db) == {1, 4}
    assert fresh_db.execute(
        "SELECT id, category_id FROM items ORDER BY id"
    ).fetchall() == [(1, 1), (2, 4), (3, 4)]


def test_reconcile_bad_delete_hint(fresh_db):
    setup_items(
        fresh_db,
        [{"id": 1, "category": "Fish"}, {"id": 2, "category": "Fruit"}],
    )
    fresh_db["items"].reconcile("category")
    reimport(fresh_db, {1: "Fish", 2: "Fruit"})
    with pytest.raises(ReconcileConflict, match="disappeared"):
        fresh_db["items"].reconcile("category", deletes=["Fruit"])
    reimport(fresh_db, {1: "Seafood", 2: "Fruit"})
    with pytest.raises(ReconcileConflict, match="both renamed and deleted"):
        fresh_db["items"].reconcile(
            "category", renames={"Fish": "Seafood"}, deletes=["Fish"]
        )


def test_reconcile_bad_rename_hint_old_value_still_present(fresh_db):
    setup_items(
        fresh_db,
        [{"id": 1, "category": "Fish"}, {"id": 2, "category": "Fruit"}],
    )
    fresh_db["items"].reconcile("category")
    reimport(fresh_db, {1: "Fish", 2: "Fruit"})
    with pytest.raises(ReconcileConflict, match="disappeared"):
        fresh_db["items"].reconcile("category", renames={"Fish": "Seafood"})


def test_reconcile_bad_rename_hint_target_already_exists(fresh_db):
    setup_items(
        fresh_db,
        [{"id": 1, "category": "Fish"}, {"id": 2, "category": "Fruit"}],
    )
    fresh_db["items"].reconcile("category")
    # Fish disappeared but Fruit (the rename target) is still present
    reimport(fresh_db, {1: "Seafood", 2: "Fruit"})
    with pytest.raises(ReconcileConflict, match="already exists"):
        fresh_db["items"].reconcile("category", renames={"Fish": "Fruit"})
    # The unambiguous 1:1 rename still works without a hint
    fresh_db["items"].reconcile("category")
    assert fresh_db.execute(
        "SELECT id, category_id FROM items ORDER BY id"
    ).fetchall() == [(1, 1), (2, 2)]
    assert list(fresh_db["category"].rows_where("id = 1")) == [
        {"id": 1, "category": "Seafood"}
    ]


def test_reconcile_delete_blocked_by_external_reference(fresh_db):
    setup_items(
        fresh_db,
        [{"id": 1, "category": "Fish"}, {"id": 2, "category": "Fruit"}],
    )
    fresh_db["orders"].create(
        {"id": int, "category_id": int},
        pk="id",
        foreign_keys=[("category_id", "category", "id")],
    )
    fresh_db["orders"].insert({"id": 10, "category_id": 2})
    fresh_db["items"].reconcile("category")
    reimport(fresh_db, {1: "Fish", 2: "Fish"})
    with pytest.raises(ReconcileConflict, match="still reference"):
        fresh_db["items"].reconcile("category")
    # Fruit survives the failed reconciliation
    assert list(fresh_db["category"].rows) == [
        {"id": 1, "category": "Fish"},
        {"id": 2, "category": "Fruit"},
    ]
    # Once the external reference is gone, deletion succeeds
    fresh_db.execute("DELETE FROM orders WHERE id = 10")
    fresh_db["items"].reconcile("category")
    assert list(fresh_db["category"].rows) == [{"id": 1, "category": "Fish"}]


def test_reconcile_null_values(fresh_db):
    setup_items(
        fresh_db,
        [
            {"id": 1, "category": "Fish"},
            {"id": 2, "category": None},
            {"id": 3, "category": "Fish"},
        ],
    )
    fresh_db["items"].reconcile("category")
    # A row changing to null gets a null foreign key and no lookup row
    reimport(fresh_db, {1: None, 2: None, 3: "Fish"})
    fresh_db["items"].reconcile("category")
    assert list(fresh_db["category"].rows) == [{"id": 1, "category": "Fish"}]
    assert fresh_db.execute(
        "SELECT id, category_id FROM items ORDER BY id"
    ).fetchall() == [(1, None), (2, None), (3, 1)]
    assert managed_ids(fresh_db) == {1}


def test_reconcile_merges_duplicate_row_into_managed_identity(fresh_db):
    # A later import using extracts= can insert a duplicate lookup row for
    # a renamed value (new ID) and point rows at it. Reconciliation folds
    # that row back into the original managed identity
    setup_items(
        fresh_db,
        [{"id": 1, "category": "Fish"}, {"id": 2, "category": "Fruit"}],
    )
    fresh_db["orders"].create(
        {"id": int, "category_id": int},
        pk="id",
        foreign_keys=[("category_id", "category", "id")],
    )
    fresh_db["orders"].insert({"id": 10, "category_id": 1})
    fresh_db["items"].reconcile("category")

    # Simulate the duplicate created by a second import with extracts=
    fresh_db["category"].insert({"id": 3, "category": "Seafood"})
    fresh_db.execute(
        "UPDATE items SET category = 'Seafood', category_id = 3 WHERE id = 1"
    )
    fresh_db["items"].reconcile("category")

    assert list(fresh_db["category"].rows) == [
        {"id": 1, "category": "Seafood"},
        {"id": 2, "category": "Fruit"},
    ]
    # Both this table and the external reference now use the canonical ID
    assert fresh_db.execute(
        "SELECT category_id FROM items WHERE id = 1"
    ).fetchone() == (1,)
    assert fresh_db.execute(
        "SELECT category_id FROM orders WHERE id = 10"
    ).fetchone() == (1,)
    assert 3 not in managed_ids(fresh_db)


def test_reconcile_multiple_columns_with_column_rename(fresh_db):
    fresh_db["items"].insert_all(
        [
            {"id": 1, "common_name": "Palm", "latin_name": "Arecaceae"},
            {"id": 2, "common_name": "Oak", "latin_name": "Quercus"},
            {"id": 3, "common_name": "Palm", "latin_name": "Arecaceae"},
        ],
        pk="id",
    )
    fresh_db["items"].extract(
        ["common_name", "latin_name"],
        table="species",
        rename={"common_name": "name"},
    )
    # Put the raw columns back
    fresh_db["items"].add_column("common_name", str)
    fresh_db["items"].add_column("latin_name", str)
    fresh_db.execute(
        "UPDATE items SET common_name = 'Palm', latin_name = 'Arecaceae' "
        "WHERE id IN (1, 3)"
    )
    fresh_db.execute(
        "UPDATE items SET common_name = 'Oak tree', latin_name = 'Quercus' WHERE id = 2"
    )
    fresh_db["items"].reconcile(
        ["common_name", "latin_name"],
        table="species",
        rename={"common_name": "name"},
    )
    assert list(fresh_db["species"].rows) == [
        {"id": 1, "name": "Palm", "latin_name": "Arecaceae"},
        {"id": 2, "name": "Oak tree", "latin_name": "Quercus"},
    ]
    assert fresh_db.execute(
        "SELECT id, species_id FROM items ORDER BY id"
    ).fetchall() == [(1, 1), (2, 2), (3, 1)]


def test_reconcile_multiple_columns_explicit_tuple_rename(fresh_db):
    fresh_db["items"].insert_all(
        [
            {"id": 1, "common_name": "Palm", "latin_name": "Arecaceae"},
            {"id": 2, "common_name": "Oak", "latin_name": "Quercus"},
        ],
        pk="id",
    )
    fresh_db["items"].extract(["common_name", "latin_name"], table="species")
    fresh_db["items"].add_column("common_name", str)
    fresh_db["items"].add_column("latin_name", str)
    fresh_db.execute(
        "UPDATE items SET common_name = 'Coconut', latin_name = 'Arecaceae' "
        "WHERE id = 1"
    )
    fresh_db.execute(
        "UPDATE items SET common_name = 'Oak', latin_name = 'Quercus' WHERE id = 2"
    )
    # 1 removed / 1 added would auto-rename anyway; assert the explicit
    # tuple form works and reuses the identity
    fresh_db["items"].reconcile(
        ["common_name", "latin_name"],
        table="species",
        renames={("Palm", "Arecaceae"): ("Coconut", "Arecaceae")},
    )
    assert list(fresh_db["species"].rows) == [
        {"id": 1, "common_name": "Coconut", "latin_name": "Arecaceae"},
        {"id": 2, "common_name": "Oak", "latin_name": "Quercus"},
    ]


def test_reconcile_duplicate_lookup_rows_conflict(fresh_db):
    # Unique indexes treat NULLs as distinct, so a hand-created duplicate
    # with a NULL in the key cannot be assigned a unique identity
    fresh_db["items"].insert_all([{"id": 1, "species": None, "common": "X"}], pk="id")
    fresh_db["items"].extract(["species", "common"], table="lk")
    fresh_db.execute("INSERT INTO lk (species, common) VALUES (NULL, 'X')")
    fresh_db["items"].add_column("species", str)
    fresh_db["items"].add_column("common", str)
    fresh_db.execute("UPDATE items SET species = NULL, common = 'X'")
    with pytest.raises(ReconcileConflict, match="unique identity"):
        fresh_db["items"].reconcile(["species", "common"], table="lk")


def test_reconcile_requires_existing_extract(fresh_db):
    fresh_db["items"].insert({"id": 1, "category": "Fish"}, pk="id")
    with pytest.raises(NoTable, match="use .extract"):
        fresh_db["items"].reconcile("category")
    fresh_db["category"].insert({"id": 1, "category": "Fish"})
    with pytest.raises(InvalidColumns, match="Foreign key column"):
        fresh_db["items"].reconcile("category")
    with pytest.raises(InvalidColumns, match="Invalid columns"):
        fresh_db["items"].reconcile("nope")


def test_reconcile_repeated_runs_keep_stable_identities(fresh_db):
    setup_items(
        fresh_db,
        [{"id": 1, "category": "Fish"}, {"id": 2, "category": "Fruit"}],
    )
    fresh_db["orders"].create(
        {"id": int, "category_id": int},
        pk="id",
        foreign_keys=[("category_id", "category", "id")],
    )
    fresh_db["orders"].insert({"id": 10, "category_id": 1})
    fresh_db["items"].reconcile("category")
    # Run 1: Fish -> Seafood (rename, external order follows)
    reimport(fresh_db, {1: "Seafood", 2: "Fruit"})
    fresh_db["items"].reconcile("category")
    # Run 2: Fruit -> Veg (rename), Seafood stays
    reimport(fresh_db, {1: "Seafood", 2: "Veg"})
    fresh_db["items"].reconcile("category")
    # Run 3: Veg deleted, external order still attached to id 1 (Seafood)
    reimport(fresh_db, {1: "Seafood", 2: "Seafood"})
    fresh_db["items"].reconcile("category")
    assert list(fresh_db["category"].rows) == [{"id": 1, "category": "Seafood"}]
    assert fresh_db.execute(
        "SELECT category.category FROM orders JOIN category "
        "ON orders.category_id = category.id"
    ).fetchone() == ("Seafood",)


def test_reconcile_custom_table_and_fk_names(fresh_db):
    fresh_db["items"].insert_all([{"id": 1, "species": "Wolf"}], pk="id")
    fresh_db["items"].extract(
        "species",
        table="Species",
        fk_column="species_fk",
        rename={"species": "name"},
    )
    fresh_db["items"].add_column("species", str)
    fresh_db.execute("UPDATE items SET species = 'Fox'")
    fresh_db["items"].reconcile(
        "species",
        table="Species",
        fk_column="species_fk",
        rename={"species": "name"},
    )
    assert list(fresh_db["Species"].rows) == [{"id": 1, "name": "Fox"}]
    assert fresh_db.execute("SELECT species_fk FROM items").fetchall() == [(1,)]


def test_cli_reconcile(tmp_path):
    db_path = str(tmp_path / "test.db")
    import sqlite_utils

    db = sqlite_utils.Database(db_path)
    db["items"].insert_all(
        [{"id": 1, "category": "Fish"}, {"id": 2, "category": "Fruit"}],
        pk="id",
    )
    db["items"].extract("category")
    db["items"].add_column("category", str)
    db.execute("UPDATE items SET category = 'Seafood' WHERE id = 1")
    db.execute("UPDATE items SET category = 'Fruit' WHERE id = 2")
    db.close()

    result = CliRunner().invoke(
        cli.cli,
        [
            "reconcile",
            db_path,
            "items",
            "category",
            "--rename-value",
            "Fish",
            "Seafood",
        ],
    )
    assert result.exit_code == 0, result.output

    db = sqlite_utils.Database(db_path)
    assert list(db["category"].rows) == [
        {"id": 1, "category": "Seafood"},
        {"id": 2, "category": "Fruit"},
    ]
    db.close()


def test_cli_reconcile_conflict_fails(tmp_path):
    db_path = str(tmp_path / "test.db")
    import sqlite_utils

    db = sqlite_utils.Database(db_path)
    db["items"].insert_all(
        [{"id": 1, "category": "Fish"}, {"id": 2, "category": "Fruit"}],
        pk="id",
    )
    db["items"].extract("category")
    db["items"].add_column("category", str)
    db.execute("UPDATE items SET category = 'Seafood' WHERE id = 1")
    db.execute("UPDATE items SET category = 'Veg' WHERE id = 2")
    db.close()

    result = CliRunner().invoke(cli.cli, ["reconcile", db_path, "items", "category"])
    assert result.exit_code == 1
    assert "ambiguous" in result.output
