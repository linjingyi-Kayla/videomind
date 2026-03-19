from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from .db_models import Base


def _db_path() -> str:
    p = os.getenv("VIDEOMIND_DB_PATH", "data/videomind.sqlite3")
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    return str(Path(p).resolve())


def _db_url() -> str:
    # Railway/生产：优先使用 DATABASE_URL（Postgres/MySQL 等）
    db_url = os.getenv("DATABASE_URL") or os.getenv("VIDEOMIND_DATABASE_URL")
    if db_url:
        return db_url

    # 本地开发：SQLite（兼容异步 worker 线程）
    db_file = _db_path().replace("\\", "/")
    return f"sqlite+pysqlite:///{db_file}"


def create_engine_and_session() -> tuple:
    url = _db_url()
    connect_args = {}
    if url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}

    engine = create_engine(url, connect_args=connect_args, future=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    return engine, SessionLocal


engine, SessionLocal = create_engine_and_session()


def init_db() -> None:
    # SQLite 升级兼容：旧版 tasks 表没有 task_uuid（且 id 为 TEXT 主键）
    # 迁移策略：tasks -> tasks_old -> 重建 tasks（把旧 id 复制到 task_uuid）
    try:
        insp = inspect(engine)
        if "tasks" in insp.get_table_names():
            cols = [c["name"] for c in insp.get_columns("tasks")]
            if "task_uuid" not in cols and engine.dialect.name == "sqlite":
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE tasks RENAME TO tasks_old"))
                Base.metadata.create_all(bind=engine)
                with engine.begin() as conn:
                    conn.execute(
                        text(
                            """
                            INSERT INTO tasks (
                              task_uuid, video_url, title, category, summary, key_points_json,
                              remind_at, is_notified, status, error_message, subscription_id,
                              created_at, updated_at
                            )
                            SELECT
                              id as task_uuid, video_url, title, category, summary, key_points_json,
                              remind_at, is_notified, status, error_message, subscription_id,
                              created_at, updated_at
                            FROM tasks_old
                            """
                        )
                    )
                    conn.execute(text("DROP TABLE tasks_old"))
            elif "task_uuid" not in cols and engine.dialect.name.startswith("postgres"):
                # Postgres 升级兼容：如果旧 tasks 没有 task_uuid，则用旧 id 填充 task_uuid
                old_cols = set(cols)

                def _expr(col: str, fallback: str = "NULL") -> str:
                    return col if col in old_cols else fallback

                video_url_expr = _expr("video_url")
                title_expr = _expr("title")
                category_expr = _expr("category")
                summary_expr = _expr("summary")
                key_points_expr = _expr("key_points_json")
                remind_at_expr = _expr("remind_at")
                is_notified_expr = _expr("is_notified")
                status_expr = _expr("status")
                error_message_expr = _expr("error_message")
                subscription_id_expr = _expr("subscription_id")
                created_at_expr = _expr("created_at")
                updated_at_expr = _expr("updated_at")

                with engine.begin() as conn:
                    conn.execute(text("DROP TABLE IF EXISTS tasks_old"))
                    conn.execute(text("ALTER TABLE tasks RENAME TO tasks_old"))

                Base.metadata.create_all(bind=engine)

                with engine.begin() as conn:
                    conn.execute(
                        text(
                            f"""
                            INSERT INTO tasks (
                              task_uuid, video_url, title, category, summary, key_points_json,
                              remind_at, is_notified, status, error_message, subscription_id,
                              created_at, updated_at
                            )
                            SELECT
                              id::text as task_uuid,
                              {video_url_expr},
                              {title_expr},
                              {category_expr},
                              {summary_expr},
                              {key_points_expr},
                              {remind_at_expr},
                              {is_notified_expr},
                              {status_expr},
                              {error_message_expr},
                              {subscription_id_expr},
                              {created_at_expr},
                              {updated_at_expr}
                            FROM tasks_old
                            """
                        )
                    )
                    conn.execute(text("DROP TABLE tasks_old"))
            else:
                Base.metadata.create_all(bind=engine)
        else:
            Base.metadata.create_all(bind=engine)
    except Exception:
        # 无需阻断启动；数据库权限不足时让应用按只读模型启动
        Base.metadata.create_all(bind=engine)


def new_session() -> Session:
    return SessionLocal()

