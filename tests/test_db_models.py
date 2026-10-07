import os
import stat
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import BigInteger, Boolean, DateTime, Enum, Integer, String, Text, inspect, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlmodel import SQLModel, select

import app.db.engine as engine_module
from app.db.engine import init_db, make_engine, session_scope
from app.db.models import (
    LIVE_LINK_STATUSES,
    AlertKind,
    DeliveryStatus,
    FamilyAlert,
    FamilyAlertDelivery,
    FamilyLink,
    LinkStatus,
    Person,
    UTCDateTime,
)

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


@pytest.fixture
def engine():
    engine = make_engine("sqlite://")
    init_db(engine)
    return engine


def add_person(s, telegram_user_id: int, name: str = "P") -> Person:
    person = Person(telegram_user_id=telegram_user_id, display_name=name, language="en", created_at=NOW)
    s.add(person)
    s.flush()
    return person


def add_link(s, ref: str, protected: Person, guardian: Person | None, status: LinkStatus, **extra) -> FamilyLink:
    link = FamilyLink(
        ref=ref, protected_id=protected.id, guardian_id=guardian.id if guardian else None,
        status=status.value, created_at=NOW, **extra,
    )  # fmt: skip
    s.add(link)
    s.flush()
    return link


# --- schema ---------------------------------------------------------------------------------------------


def test_create_all_makes_the_four_tables_and_is_idempotent(engine):
    init_db(engine)  # a second run must not fail
    assert set(inspect(engine).get_table_names()) == {"person", "family_link", "family_alert", "family_alert_delivery"}


def test_only_family_tables_are_registered():
    assert set(SQLModel.metadata.tables) == {"person", "family_link", "family_alert", "family_alert_delivery"}


def test_telegram_ids_are_bigint_and_hold_values_over_32_bits(engine):
    assert isinstance(Person.__table__.c.telegram_user_id.type, BigInteger)
    assert isinstance(FamilyAlert.__table__.c.source_update_id.type, BigInteger)
    with session_scope(engine) as s:
        add_person(s, 5_000_000_000)
    with session_scope(engine) as s:
        assert s.exec(select(Person)).one().telegram_user_id == 5_000_000_000


# --- timestamps -------------------------------------------------------------------------------------------


def test_datetimes_round_trip_as_aware_utc(engine):
    with session_scope(engine) as s:
        add_person(s, 1)
    with session_scope(engine) as s:
        created = s.exec(select(Person)).one().created_at
    assert created == NOW
    assert created.tzinfo is not None and created.utcoffset() == timedelta(0)


def test_non_utc_datetimes_are_converted_to_utc(engine):
    ist = timezone(timedelta(hours=5, minutes=30))
    with session_scope(engine) as s:
        s.add(Person(telegram_user_id=1, display_name="P", language="en", created_at=datetime(2026, 10, 7, 17, 30, tzinfo=ist)))
    with session_scope(engine) as s:
        assert s.exec(select(Person)).one().created_at == NOW


def test_naive_datetimes_are_rejected(engine):
    with pytest.raises((ValueError, StatementError)):
        with session_scope(engine) as s:
            s.add(Person(telegram_user_id=1, display_name="P", language="en", created_at=datetime(2026, 10, 7, 12, 0)))


def test_time_comparisons_in_sql_use_the_same_representation(engine):
    with session_scope(engine) as s:
        p = add_person(s, 1)
        add_link(s, "a" * 16, p, None, LinkStatus.INVITED, expires_at=NOW + timedelta(hours=1))
    with session_scope(engine) as s:
        later = s.exec(select(FamilyLink).where(FamilyLink.expires_at > NOW)).all()
        earlier = s.exec(select(FamilyLink).where(FamilyLink.expires_at > NOW + timedelta(hours=2))).all()
    assert len(later) == 1 and earlier == []


def test_utc_datetime_type_handles_none():
    type_ = UTCDateTime()
    assert type_.process_bind_param(None, None) is None
    assert type_.process_result_value(None, None) is None


# --- constraints -----------------------------------------------------------------------------------------------


def test_telegram_user_id_is_unique(engine):
    with pytest.raises(IntegrityError):
        with session_scope(engine) as s:
            add_person(s, 7)
            add_person(s, 7)


def test_a_person_cannot_be_their_own_helper(engine):
    with pytest.raises(IntegrityError):
        with session_scope(engine) as s:
            p = add_person(s, 1)
            add_link(s, "a" * 16, p, p, LinkStatus.ACTIVE)


@pytest.mark.parametrize("status", LIVE_LINK_STATUSES)
def test_only_one_live_link_per_pair(engine, status):
    with pytest.raises(IntegrityError):
        with session_scope(engine) as s:
            p, g = add_person(s, 1), add_person(s, 2)
            add_link(s, "a" * 16, p, g, status)
            add_link(s, "b" * 16, p, g, LinkStatus.ACTIVE)


def test_ended_links_do_not_block_a_new_live_link_for_the_same_pair(engine):
    with session_scope(engine) as s:
        p, g = add_person(s, 1), add_person(s, 2)
        for i, status in enumerate((LinkStatus.DECLINED, LinkStatus.REVOKED, LinkStatus.EXPIRED, LinkStatus.REVOKED)):
            add_link(s, f"{i:016d}", p, g, status)
        add_link(s, "f" * 16, p, g, LinkStatus.ACTIVE)  # no error
    with session_scope(engine) as s:
        assert len(s.exec(select(FamilyLink)).all()) == 5


def test_several_unopened_invites_may_coexist(engine):
    with session_scope(engine) as s:
        p = add_person(s, 1)
        for i in range(3):
            add_link(s, f"{i:016d}", p, None, LinkStatus.INVITED, token_hash=f"hash{i}")


def test_one_guardian_may_help_several_people_and_vice_versa(engine):
    with session_scope(engine) as s:
        a, b, g1, g2 = (add_person(s, n) for n in (1, 2, 3, 4))
        add_link(s, "1" * 16, a, g1, LinkStatus.ACTIVE)
        add_link(s, "2" * 16, a, g2, LinkStatus.ACTIVE)
        add_link(s, "3" * 16, b, g1, LinkStatus.ACTIVE)


def test_link_ref_and_token_hash_are_unique(engine):
    with pytest.raises(IntegrityError):
        with session_scope(engine) as s:
            p = add_person(s, 1)
            add_link(s, "a" * 16, p, None, LinkStatus.INVITED, token_hash="same")
            add_link(s, "b" * 16, p, None, LinkStatus.INVITED, token_hash="same")
    with pytest.raises(IntegrityError):
        with session_scope(engine) as s:
            p = add_person(s, 1)
            add_link(s, "a" * 16, p, None, LinkStatus.INVITED)
            add_link(s, "a" * 16, p, None, LinkStatus.INVITED)


def test_null_token_hashes_do_not_collide(engine):
    with session_scope(engine) as s:
        p = add_person(s, 1)
        add_link(s, "a" * 16, p, None, LinkStatus.REVOKED, token_hash=None)
        add_link(s, "b" * 16, p, None, LinkStatus.REVOKED, token_hash=None)


def test_one_alert_per_person_per_update(engine):
    with pytest.raises(IntegrityError):
        with session_scope(engine) as s:
            p = add_person(s, 1)
            for _ in range(2):
                s.add(FamilyAlert(protected_id=p.id, created_at=NOW, alert_kind="normal", source_update_id=99))
                s.flush()


def test_one_delivery_per_alert_and_helper(engine):
    with pytest.raises(IntegrityError):
        with session_scope(engine) as s:
            p, g = add_person(s, 1), add_person(s, 2)
            alert = FamilyAlert(protected_id=p.id, created_at=NOW, alert_kind="normal", source_update_id=1)
            s.add(alert)
            s.flush()
            for _ in range(2):
                s.add(FamilyAlertDelivery(alert_id=alert.id, guardian_id=g.id, status="pending"))
                s.flush()


# --- foreign keys --------------------------------------------------------------------------------------------------------


def test_foreign_keys_are_enforced(engine):
    with pytest.raises(IntegrityError):
        with session_scope(engine) as s:
            s.add(FamilyAlert(protected_id=424242, created_at=NOW, alert_kind="normal", source_update_id=1))


def test_deleting_a_person_cascades_in_the_database(engine):
    with session_scope(engine) as s:
        p, g = add_person(s, 1), add_person(s, 2)
        add_link(s, "a" * 16, p, g, LinkStatus.ACTIVE)
        alert = FamilyAlert(protected_id=p.id, created_at=NOW, alert_kind="normal", source_update_id=1)
        s.add(alert)
        s.flush()
        s.add(FamilyAlertDelivery(alert_id=alert.id, guardian_id=g.id, status="pending"))
    with session_scope(engine) as s:
        s.exec(text("DELETE FROM person WHERE telegram_user_id = 1"))  # raw SQL only to exercise ON DELETE CASCADE
    with session_scope(engine) as s:
        assert s.exec(select(FamilyLink)).all() == []
        assert s.exec(select(FamilyAlert)).all() == []
        assert s.exec(select(FamilyAlertDelivery)).all() == []
        assert len(s.exec(select(Person)).all()) == 1  # the helper remains


# --- privacy and portability of the schema ---------------------------------------------------------------------------------------


ALERT_COLUMNS = {
    "family_alert": {
        "id", "protected_id", "created_at", "alert_kind", "verdict", "scam_type",
        "source_kind", "source_update_id", "suppressed_reason",
    },
    "family_alert_delivery": {"id", "alert_id", "guardian_id", "status", "error_code", "attempted_at", "sent_at"},
}  # fmt: skip


@pytest.mark.parametrize("table_name", sorted(ALERT_COLUMNS))
def test_alert_tables_have_exactly_the_planned_columns(table_name):
    assert {c.name for c in SQLModel.metadata.tables[table_name].columns} == ALERT_COLUMNS[table_name]


@pytest.mark.parametrize("table_name", sorted(ALERT_COLUMNS))
def test_alert_tables_have_no_free_text_column(table_name):
    for column in SQLModel.metadata.tables[table_name].columns:
        kind = column.type
        if isinstance(kind, String):
            assert kind.length is not None and kind.length <= 32, f"{table_name}.{column.name} could hold free text"
        else:
            assert isinstance(kind, (Integer, BigInteger, Boolean, DateTime, UTCDateTime)), (table_name, column.name)


def test_no_table_uses_unbounded_text_or_native_enums():
    for table in SQLModel.metadata.tables.values():
        for column in table.columns:
            assert not isinstance(column.type, (Text, Enum)), f"{table.name}.{column.name} is not portable/bounded"
            if isinstance(column.type, String):
                assert column.type.length is not None, f"{table.name}.{column.name} has no length"


def test_person_stores_no_username_phone_or_content():
    assert {c.name for c in Person.__table__.columns} == {
        "id", "telegram_user_id", "display_name", "language", "bot_blocked", "created_at",
    }  # fmt: skip


def test_link_stores_a_token_hash_not_a_token():
    names = {c.name for c in FamilyLink.__table__.columns}
    assert "token_hash" in names and "token" not in names


def test_partial_unique_index_is_declared_for_both_sqlite_and_postgres():
    index = next(i for i in FamilyLink.__table__.indexes if i.name == "uq_family_link_live_pair")
    assert index.unique
    sqlite_where = index.dialect_options["sqlite"]["where"]
    postgres_where = index.dialect_options["postgresql"]["where"]
    assert sqlite_where is not None and postgres_where is not None
    assert str(sqlite_where) == str(postgres_where)  # the same condition on both backends
    for status in ("awaiting_guardian", "awaiting_protected", "active"):
        assert status in str(postgres_where)
    assert "declined" not in str(postgres_where)


def test_enum_values_are_the_strings_that_get_stored():
    assert LinkStatus.ACTIVE.value == "active" and AlertKind.EMERGENCY.value == "emergency"
    assert DeliveryStatus.SKIPPED_REVOKED.value == "skipped_revoked"
    assert {s.value for s in LinkStatus} == {
        "invited", "awaiting_guardian", "awaiting_protected", "active", "declined", "revoked", "expired",
    }  # fmt: skip


# --- engine behaviour -----------------------------------------------------------------------------------------------------------------------


def test_sqlite_connections_enforce_foreign_keys(engine):
    with session_scope(engine) as s:
        assert s.exec(text("PRAGMA foreign_keys")).scalar() == 1


def test_file_database_uses_wal_and_is_private_to_the_owner(tmp_path):
    path = tmp_path / "ruko.db"
    file_engine = make_engine(f"sqlite:///{path}")
    init_db(file_engine)
    with session_scope(file_engine) as s:
        assert s.exec(text("PRAGMA journal_mode")).scalar() == "wal"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    file_engine.dispose()


def test_transactions_begin_immediate_on_sqlite(tmp_path):
    """A second writer must wait for the first: that is what serialises the
    check-then-insert steps in the alert logic."""
    file_engine = make_engine(f"sqlite:///{tmp_path / 'x.db'}")
    init_db(file_engine)
    with session_scope(file_engine) as first:
        first.exec(text("SELECT 1"))
        other = file_engine.raw_connection()
        try:
            other.driver_connection.execute("PRAGMA busy_timeout=50")
            with pytest.raises(Exception, match="locked"):
                other.driver_connection.execute("BEGIN IMMEDIATE")
        finally:
            other.close()
    file_engine.dispose()


def test_session_scope_rolls_back_on_error(engine):
    with pytest.raises(RuntimeError):
        with session_scope(engine) as s:
            add_person(s, 1)
            raise RuntimeError("boom")
    with session_scope(engine) as s:
        assert s.exec(select(Person)).all() == []


def test_session_scope_commits_on_success(engine):
    with session_scope(engine) as s:
        add_person(s, 1)
    with session_scope(engine) as s:
        assert len(s.exec(select(Person)).all()) == 1


def test_sqlite_specifics_are_not_applied_to_other_databases(monkeypatch):
    """Moving to Postgres must only need a different DATABASE_URL: none of the
    SQLite-only settings may be passed for other backends."""
    seen = {}

    def fake_create_engine(url, **kwargs):
        seen["url"], seen["kwargs"] = url, kwargs
        return "ENGINE"

    monkeypatch.setattr(engine_module, "sa_create_engine", fake_create_engine)
    monkeypatch.setattr(engine_module, "_configure_sqlite", lambda *a, **k: pytest.fail("SQLite setup ran for Postgres"))
    assert make_engine("postgresql+psycopg://user:pw@db.example/ruko") == "ENGINE"
    assert seen["kwargs"] == {"echo": False}


def test_is_sqlite_detection():
    assert engine_module._is_sqlite("sqlite:///./ruko.db") and engine_module._is_sqlite("sqlite://")
    assert not engine_module._is_sqlite("postgresql://u:p@h/db")
    assert engine_module._sqlite_database_path("sqlite://") is None
    assert engine_module._sqlite_database_path("sqlite:///:memory:") is None
    assert engine_module._sqlite_database_path("sqlite:///./ruko.db") == "./ruko.db"
