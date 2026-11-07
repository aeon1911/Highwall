from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


def _make_engine(url: str):
    kwargs = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()
    return engine


engine = _make_engine(get_settings().database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db(bind=None) -> None:
    from . import models  # noqa: F401  (registra as tabelas)

    bind = bind or engine
    _migrate(bind)
    Base.metadata.create_all(bind=bind)


def _migrate(bind) -> None:
    """Migrações simples para bancos SQLite criados por versões anteriores."""
    from sqlalchemy import inspect, text

    insp = inspect(bind)
    tables = insp.get_table_names()
    with bind.begin() as conn:
        if "users" in tables:
            cols = {c["name"] for c in insp.get_columns("users")}
            for name, ddl in (
                ("kdf_salt", "VARCHAR(64)"),
                ("kdf_iterations", "INTEGER"),
                ("recovery_auth_hash", "VARCHAR(255)"),
            ):
                if name not in cols:
                    conn.execute(text(f"ALTER TABLE users ADD COLUMN {name} {ddl}"))
        if "vault_keys" in tables and "salt" in {c["name"] for c in insp.get_columns("vault_keys")}:
            # formato antigo (senha separada do vault): incompatível com o novo esquema
            if conn.execute(text("SELECT COUNT(*) FROM vault_keys")).scalar():
                raise RuntimeError("vault_keys no formato antigo contém dados; migre manualmente")
            conn.execute(text("DROP TABLE vault_keys"))
