from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import sessionmaker

from storage.models import Base


def create_session_factory(database_url: str):
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, echo=False, connect_args=connect_args)

    if database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def set_sqlite_pragmas(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

    return sessionmaker(bind=engine, expire_on_commit=False)


def init_db(session_factory) -> None:
    """Create tables; automatically add the two JSON metadata columns only on SQLite.

    Other dialects may initialize fresh or already-current schemas, but legacy
    articles tables need an explicit migration before this function can proceed.
    No DDL is issued for an unsupported legacy schema.
    """
    engine = session_factory.kw["bind"]
    schema = inspect(engine)
    missing = {}
    if schema.has_table("articles"):
        present = {column["name"] for column in schema.get_columns("articles")}
        missing = {
            name: default
            for name, default in {"media_assets_json": "[]", "field_provenance_json": "{}"}.items()
            if name not in present
        }
    if missing:
        if engine.dialect.name != "sqlite":
            raise RuntimeError(
                "Automatic article metadata migration supports SQLite only; "
                "migrate the missing columns explicitly before calling init_db."
            )
        with engine.begin() as connection:
            for name, default in missing.items():
                # Names and defaults are fixed constants above, never external input.
                connection.exec_driver_sql(
                    f"ALTER TABLE articles ADD COLUMN {name} TEXT NOT NULL DEFAULT '{default}'"
                )
    Base.metadata.create_all(engine)
