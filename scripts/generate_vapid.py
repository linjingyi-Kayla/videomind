from __future__ import annotations

import os
from pathlib import Path

from py_vapid import Vapid
from py_vapid.utils import b64urlencode


def _compute_vapid_keys() -> tuple[str, str]:
    v = Vapid()
    v.generate_keys()

    pub = v.public_key.public_numbers()
    x = pub.x
    y = pub.y
    # WebPush VAPID uses uncompressed EC public key: 0x04 + X(32) + Y(32) (65 bytes)
    raw_public = b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")
    vapid_public_key = b64urlencode(raw_public)  # ~87 chars

    priv = v.private_key.private_numbers().private_value
    raw_private = priv.to_bytes(32, "big")  # 32 bytes
    vapid_private_key = b64urlencode(raw_private)  # ~43 chars

    return vapid_public_key, vapid_private_key


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    env_path = repo_root / ".env"

    claims_sub = os.getenv("VAPID_CLAIMS_SUB", "mailto:videomind@example.com")

    public_key, private_key = _compute_vapid_keys()

    # 写回 .env（覆盖旧值）
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()

    def upsert(k: str, v: str) -> None:
        nonlocal lines
        prefix = f"{k}="
        replaced = False
        for i, line in enumerate(lines):
            if line.startswith(prefix):
                lines[i] = f"{k}={v}"
                replaced = True
                break
        if not replaced:
            lines.append(f"{k}={v}")

    upsert("VAPID_PUBLIC_KEY", public_key)
    upsert("VAPID_PRIVATE_KEY", private_key)
    upsert("VAPID_CLAIMS_SUB", claims_sub)

    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("VAPID 公钥已写入 .env：VAPID_PUBLIC_KEY")
    print("VAPID 私钥已写入 .env：VAPID_PRIVATE_KEY")
    print("VAPID claims：VAPID_CLAIMS_SUB =", claims_sub)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

