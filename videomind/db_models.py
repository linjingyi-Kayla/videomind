from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    # 供快捷指令 / Web Share 等无法带 JWT 的场景：与 url 一并提交
    share_token: Mapped[Optional[str]] = mapped_column(String(64), unique=True, nullable=True, index=True)


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    endpoint: Mapped[str] = mapped_column(String, unique=True, nullable=False)

    # WebPush keys（来自前端 PushSubscription）
    p256dh: Mapped[str] = mapped_column(String, nullable=False)
    auth: Mapped[str] = mapped_column(String, nullable=False)

    expiration_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    user_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True, index=True
    )


class Task(Base):
    __tablename__ = "tasks"

    # 内部自增主键（满足你对“id 自增主键”的要求）
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # 给前端/通知链路使用的稳定标识（避免改动 API 路由形态）
    task_uuid: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)

    video_url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    category: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    key_points_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # 到期通知时间（UTC；如果为空则表示未设置提醒）
    remind_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    is_notified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # pending/done/error
    status: Mapped[str] = mapped_column(String, default="pending", nullable=False)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # per_subscription 模式：任务属于哪个订阅（subscription.id）
    # 如果为 None，则表示“未关联订阅”（MVP 可广播或回填到最近订阅）
    subscription_id: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)

    # 多用户：任务归属
    user_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True, index=True
    )

    # 收藏 / 用户批注（详情页持久化）
    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    annotation: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

