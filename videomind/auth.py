"""
密码哈希（bcrypt）与 JWT 签发/校验。
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db_models import User


JWT_ALGORITHM = "HS256"


def _jwt_secret() -> str:
    s = (os.getenv("JWT_SECRET") or "").strip()
    if not s:
        # 开发兜底；生产务必设置强随机 JWT_SECRET
        s = "dev-videomind-insecure-change-me"
    return s


def jwt_expire_minutes() -> int:
    return int(os.getenv("JWT_EXPIRE_MINUTES", "10080"))  # 默认 7 天


def hash_password(password: str) -> str:
    """bcrypt 加盐哈希；密码最长 72 字节。"""
    raw = password.encode("utf-8")
    if len(raw) > 72:
        raw = raw[:72]
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("ascii")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        raw = plain_password.encode("utf-8")
        if len(raw) > 72:
            raw = raw[:72]
        return bcrypt.checkpw(raw, hashed_password.encode("ascii"))
    except Exception:
        return False


def create_access_token(user_id: int) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=jwt_expire_minutes())
    payload = {"sub": str(user_id), "exp": expire}
    return jwt.encode(payload, _jwt_secret(), algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    return jwt.decode(token, _jwt_secret(), algorithms=[JWT_ALGORITHM])


def get_user_id_from_token(token: str) -> int:
    payload = decode_token(token)
    sub = payload.get("sub")
    if sub is None:
        raise ValueError("invalid token")
    return int(sub)


def get_user_by_token(session: Session, token: str) -> Optional[User]:
    try:
        uid = get_user_id_from_token(token)
    except Exception:
        return None
    return session.execute(select(User).where(User.id == uid)).scalars().first()
