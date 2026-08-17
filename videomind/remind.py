from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone
from typing import Optional


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_time_hhmm(hhmm: str) -> time:
    hh, mm = hhmm.split(":")
    return time(hour=int(hh), minute=int(mm))


def calc_next_remind_datetime(hhmm: str, tz_offset_minutes: Optional[int] = None) -> datetime:
    """
    将 `HH:MM` 计算为“下一次提醒时间”的 UTC 存库值（naive UTC）。

    - tz_offset_minutes：与浏览器 `Date.getTimezoneOffset()` 一致（东八区通常为 -480）。
    - 为 None 时按 UTC 墙钟解释。
    """
    t = parse_time_hhmm(hhmm)
    now = now_utc()
    now_naive = now.replace(tzinfo=None)

    if tz_offset_minutes is None:
        dt = datetime.combine(now.date(), t)
        if dt <= now_naive:
            dt = dt + timedelta(days=1)
        return dt

    local_wall = now_naive - timedelta(minutes=tz_offset_minutes)
    local_date = local_wall.date()
    candidate = datetime.combine(local_date, t)
    if candidate <= local_wall:
        candidate = candidate + timedelta(days=1)
    utc_candidate = candidate + timedelta(minutes=tz_offset_minutes)
    return utc_candidate


def _local_now(tz_offset_minutes: Optional[int]) -> datetime:
    now_naive = now_utc().replace(tzinfo=None)
    if tz_offset_minutes is None:
        return now_naive
    return now_naive - timedelta(minutes=tz_offset_minutes)


def _local_to_naive_utc(local_dt: datetime, tz_offset_minutes: Optional[int]) -> datetime:
    if tz_offset_minutes is None:
        return local_dt
    return local_dt + timedelta(minutes=tz_offset_minutes)


def parse_remind_at(raw: str, tz_offset_minutes: Optional[int] = None) -> datetime:
    """
    解析自然语言 / HH:MM / ISO，返回 naive UTC（与 Task.remind_at 一致）。
    支持：18:30、明天 20:00、今晚、明晚、2026-08-18T20:00:00。
    """
    s = (raw or "").strip()
    if not s:
        raise ValueError("remind_at 为空")

    iso = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
        # 无时区的 ISO 按用户本地墙钟解释
        return _local_to_naive_utc(dt, tz_offset_minutes)
    except ValueError:
        pass

    local_now = _local_now(tz_offset_minutes)
    day_shift = 0
    default_hhmm = None

    if re.search(r"明晚|明天晚上|明日晚上", s):
        day_shift = 1
        default_hhmm = "20:00"
    elif re.search(r"今晚|今天晚上", s):
        day_shift = 0
        default_hhmm = "20:00"
    elif re.search(r"明天|明日", s):
        day_shift = 1
        default_hhmm = "18:30"

    m = re.search(r"(\d{1,2})\s*[:：]\s*(\d{2})", s)
    if not m:
        m2 = re.search(r"(\d{1,2})\s*点(?:\s*(\d{1,2})\s*分)?", s)
        if m2:
            hh = int(m2.group(1))
            mm = int(m2.group(2) or 0)
            hhmm = f"{hh:02d}:{mm:02d}"
        elif default_hhmm:
            hhmm = default_hhmm
        else:
            raise ValueError(f"无法解析提醒时间：{raw}")
    else:
        hhmm = f"{int(m.group(1)):02d}:{int(m.group(2)):02d}"

    t = parse_time_hhmm(hhmm)
    candidate = datetime.combine(local_now.date(), t) + timedelta(days=day_shift)
    if candidate <= local_now:
        candidate = candidate + timedelta(days=1)
    return _local_to_naive_utc(candidate, tz_offset_minutes)
