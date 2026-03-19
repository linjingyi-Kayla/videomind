from __future__ import annotations

import json
import os
from typing import Any, Dict

from pywebpush import WebPushException, webpush


def _vapid_from_env() -> tuple[str, str, str]:
    public_key = os.getenv("VAPID_PUBLIC_KEY")
    private_key = os.getenv("VAPID_PRIVATE_KEY")
    claims_sub = os.getenv("VAPID_CLAIMS_SUB", "mailto:videomind@example.com")
    if not public_key or not private_key:
        raise RuntimeError("缺少 VAPID_PUBLIC_KEY 或 VAPID_PRIVATE_KEY（请运行 scripts/generate_vapid.py）")
    return public_key, private_key, claims_sub


def send_web_push(subscription: Dict[str, Any], payload: Dict[str, Any]) -> None:
    """
    subscription: 标准 PushSubscription JSON 形态
      {
        endpoint: "...",
        keys: {p256dh:"...", auth:"..."}
      }
    payload: 发送到 service-worker 的 JSON 内容
    """
    vapid_public_key, vapid_private_key, vapid_claims_sub = _vapid_from_env()

    subscription_info = {
        "endpoint": subscription["endpoint"],
        "keys": {
            "p256dh": subscription["keys"]["p256dh"],
            "auth": subscription["keys"]["auth"],
        },
    }

    data = json.dumps(payload, ensure_ascii=False)
    # 注意：pywebpush 只需要 private_key + claims；public_key 不一定必须，但保留更安全
    webpush(
        subscription_info=subscription_info,
        data=data,
        vapid_private_key=vapid_private_key,
        vapid_claims={"sub": vapid_claims_sub},
        ttl=0,
    )

