from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from sqlalchemy import create_engine, inspect, text, update
from sqlalchemy.orm import Session, sessionmaker

from .db_models import Base, User  # noqa: F401 — User 需载入以注册 metadata


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


def _ensure_user_columns(engine) -> None:
    """创建 users 表；为 tasks / subscriptions 增加 user_id（多用户）。"""
    insp = inspect(engine)
    dialect = engine.dialect.name
    with engine.begin() as conn:
        if "users" not in insp.get_table_names():
            Base.metadata.create_all(bind=engine)
            insp = inspect(engine)

        if "tasks" in insp.get_table_names():
            cols = {c["name"] for c in insp.get_columns("tasks")}
            if "user_id" not in cols:
                if dialect == "sqlite":
                    conn.execute(text("ALTER TABLE tasks ADD COLUMN user_id INTEGER"))
                else:
                    conn.execute(
                        text("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id)")
                    )

        if "subscriptions" in insp.get_table_names():
            cols = {c["name"] for c in insp.get_columns("subscriptions")}
            if "user_id" not in cols:
                if dialect == "sqlite":
                    conn.execute(text("ALTER TABLE subscriptions ADD COLUMN user_id INTEGER"))
                else:
                    conn.execute(
                        text(
                            "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id)"
                        )
                    )


def _migrate_orphan_user_ids() -> None:
    """
    将升级前遗留的 tasks / subscriptions（user_id 为空）绑定到「迁移用」账号，
    便于多用户改造后仍能访问旧数据。新账号邮箱请勿与 VIDEOMIND_LEGACY_EMAIL 冲突。
    """
    import os

    from sqlalchemy import select

    from .auth import hash_password
    from .db_models import Subscription, Task, User

    session = new_session()
    try:
        orphan_t = session.execute(select(Task).where(Task.user_id.is_(None)).limit(1)).scalars().first()
        orphan_s = (
            session.execute(select(Subscription).where(Subscription.user_id.is_(None)).limit(1))
            .scalars()
            .first()
        )
        if not orphan_t and not orphan_s:
            return

        legacy_email = os.getenv("VIDEOMIND_LEGACY_EMAIL", "legacy@videomind.local").strip().lower()
        legacy_pwd = os.getenv("VIDEOMIND_LEGACY_PASSWORD", "changeme")
        user = session.execute(select(User).where(User.email == legacy_email)).scalars().first()
        if not user:
            user = User(
                email=legacy_email,
                hashed_password=hash_password(legacy_pwd),
            )
            session.add(user)
            session.flush()

        session.execute(update(Task).where(Task.user_id.is_(None)).values(user_id=user.id))
        session.execute(update(Subscription).where(Subscription.user_id.is_(None)).values(user_id=user.id))
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _ensure_task_columns(engine) -> None:
    """为已存在的 tasks 表补齐 ORM 新增列（不删数据）。"""
    insp = inspect(engine)
    if "tasks" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("tasks")}
    dialect = engine.dialect.name
    with engine.begin() as conn:
        if "is_favorite" not in cols:
            if dialect == "sqlite":
                conn.execute(text("ALTER TABLE tasks ADD COLUMN is_favorite BOOLEAN DEFAULT 0"))
            else:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS is_favorite BOOLEAN DEFAULT FALSE"))
        if "annotation" not in cols:
            if dialect == "sqlite":
                conn.execute(text("ALTER TABLE tasks ADD COLUMN annotation TEXT"))
            else:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS annotation TEXT"))


def init_db() -> None:
    """
    初始化表结构：始终 create_all（只创建缺失的表/列，不覆盖已有数据）。

    旧版 tasks（无 task_uuid）的“重命名+复制+DROP”迁移具有误删风险。
    仅当设置环境变量 VIDEOMIND_ALLOW_LEGACY_TASKS_MIGRATION=1 时才执行。
    """
    try:
        # 先确保 ORM 声明的表存在（只创建缺失对象，不覆盖已有数据）
        Base.metadata.create_all(bind=engine)
        _ensure_task_columns(engine)
        _ensure_user_columns(engine)
        _migrate_orphan_user_ids()

        insp = inspect(engine)
        tables = insp.get_table_names()
        if "tasks" not in tables:
            return

        cols = [c["name"] for c in insp.get_columns("tasks")]
        if "task_uuid" in cols:
            # 已是新结构：不再做任何破坏性迁移
            return

        if os.getenv("VIDEOMIND_ALLOW_LEGACY_TASKS_MIGRATION", "").strip() != "1":
            # 检测到旧表结构但未授权迁移：避免误删数据，由运维显式开启后再迁移
            return

        # —— 以下为可选的旧表迁移（需显式开启）——
        if engine.dialect.name == "sqlite":
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
        elif engine.dialect.name.startswith("postgres"):
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
    except Exception:
        Base.metadata.create_all(bind=engine)
        _ensure_task_columns(engine)
        _ensure_user_columns(engine)
        _migrate_orphan_user_ids()


def new_session() -> Session:
    return SessionLocal()

