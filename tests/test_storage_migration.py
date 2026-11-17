"""Migration tests use only synthetic temporary databases, never existing crawler data."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import Text, event, inspect

from storage import database
from storage.database import create_session_factory, init_db
from storage.models import Article, Base


JSON_DEFAULTS = {"media_assets_json": "[]", "field_provenance_json": "{}"}


@pytest.mark.parametrize("existing", [(), ("media_assets_json",), ("field_provenance_json",), tuple(JSON_DEFAULTS)])
def test_sqlite_migration_adds_only_missing_columns_and_preserves_old_rows(tmp_path, existing):
    factory = create_session_factory(f"sqlite:///{tmp_path / 'synthetic-legacy.db'}")
    engine = factory.kw["bind"]
    existing_values = {
        "media_assets_json": json.dumps([{"kind": "video", "id": "kept"}]),
        "field_provenance_json": json.dumps({"title": "legacy_page"}),
    }
    extra = "".join(f", {name} TEXT NOT NULL DEFAULT '{JSON_DEFAULTS[name]}'" for name in existing)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE articles (id INTEGER PRIMARY KEY, title TEXT, content_text TEXT, "
            "content_html TEXT, collect_num INTEGER" + extra + ")"
        )
        connection.exec_driver_sql("CREATE UNIQUE INDEX keep_title_index ON articles (title)")
        connection.exec_driver_sql(
            "INSERT INTO articles (id, title, content_text, content_html, collect_num) "
            "VALUES (1, '旧标题', '旧正文', '<p>旧正文</p>', 7)"
        )
        for name in existing:
            connection.exec_driver_sql(f"UPDATE articles SET {name} = ?", (existing_values[name],))
        connection.exec_driver_sql(
            "CREATE TABLE article_metrics (id INTEGER PRIMARY KEY, article_id INTEGER, "
            "provider TEXT, captured_at TEXT, read_count INTEGER, raw_payload_json TEXT)"
        )
        connection.exec_driver_sql(
            "INSERT INTO article_metrics VALUES (1, 1, 'legacy', '2026-01-01 00:00:00', 0, ?)",
            (json.dumps({"sample": 1}),),
        )
        before = dict(connection.exec_driver_sql("SELECT * FROM articles").mappings().one())
        snapshots = list(connection.exec_driver_sql("SELECT * FROM article_metrics").mappings())
    alterations = []

    @event.listens_for(engine, "before_cursor_execute")
    def capture_alter(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.upper().startswith("ALTER TABLE"):
            alterations.append(statement)

    init_db(factory)
    init_db(factory)

    with engine.connect() as connection:
        after = dict(connection.exec_driver_sql("SELECT * FROM articles").mappings().one())
        assert {key: after[key] for key in before} == before
        for name, default in JSON_DEFAULTS.items():
            expected = existing_values[name] if name in existing else default
            assert after[name] == expected
        assert list(connection.exec_driver_sql("SELECT * FROM article_metrics").mappings()) == snapshots
    assert set(after) == set(before) | set(JSON_DEFAULTS)
    assert len(alterations) == len(set(JSON_DEFAULTS) - set(existing))
    assert {index["name"] for index in inspect(engine).get_indexes("articles")} == {"keep_title_index"}


def test_fresh_sqlite_article_metadata_has_text_defaults(tmp_path):
    factory = create_session_factory(f"sqlite:///{tmp_path / 'fresh.db'}")

    init_db(factory)
    init_db(factory)

    columns = {column["name"]: column for column in inspect(factory.kw["bind"]).get_columns("articles")}
    assert set(JSON_DEFAULTS) <= columns.keys()
    for name, default in JSON_DEFAULTS.items():
        assert isinstance(columns[name]["type"], Text)
        assert columns[name]["nullable"] is False
        assert columns[name]["default"].strip("'") == default
    with factory() as session:
        article = Article(account_id=1, publish_batch_id=1, biz="biz", mid="mid", idx=1)
        session.add(article)
        session.commit()
        assert article.media_assets_json == "[]"
        assert article.field_provenance_json == "{}"


@pytest.mark.parametrize("dialect", ["postgresql", "mysql"])
def test_non_sqlite_legacy_schema_is_rejected_before_any_ddl(monkeypatch, dialect):
    engine = Mock(dialect=SimpleNamespace(name=dialect))
    inspector = Mock()
    inspector.has_table.return_value = True
    inspector.get_columns.return_value = [{"name": "id"}]
    monkeypatch.setattr(database, "inspect", lambda bind: inspector, raising=False)
    create_all = Mock()
    monkeypatch.setattr(Base.metadata, "create_all", create_all)
    factory = SimpleNamespace(kw={"bind": engine})

    with pytest.raises(RuntimeError, match="SQLite"):
        init_db(factory)

    create_all.assert_not_called()
    engine.begin.assert_not_called()


@pytest.mark.parametrize("has_articles", [False, True])
def test_non_sqlite_fresh_or_current_schema_uses_create_all_without_sqlite_ddl(monkeypatch, has_articles):
    engine = Mock(dialect=SimpleNamespace(name="postgresql"))
    inspector = Mock()
    inspector.has_table.return_value = has_articles
    inspector.get_columns.return_value = [{"name": name} for name in JSON_DEFAULTS]
    monkeypatch.setattr(database, "inspect", lambda bind: inspector, raising=False)
    create_all = Mock()
    monkeypatch.setattr(Base.metadata, "create_all", create_all)

    init_db(SimpleNamespace(kw={"bind": engine}))

    create_all.assert_called_once_with(engine)
    engine.begin.assert_not_called()
