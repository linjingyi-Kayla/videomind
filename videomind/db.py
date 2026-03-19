from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .db_models import Base


def _db_path() -> str:
    p = os.getenv("VIDEOMIND_DB_PATH", "data/videomind.sqlite3")
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    return str(Path(p).resolve())


def _db_url() -> str:
    # 使用 pysqlite driver，兼容异步 worker 线程
    db_file = _db_path().replace("\\", "/")
    return f"sqlite+pysqlite:///{db_file}"


def create_engine_and_session() -> tuple:
    engine = create_engine(
        _db_url(),
        connect_args={"check_same_thread": False},
        future=True,
    )
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    return engine, SessionLocal


engine, SessionLocal = create_engine_and_session()


def init_db() -> None:
    Base.metadata.create_all(bind=engine)


def new_session() -> Session:
    return SessionLocal()

