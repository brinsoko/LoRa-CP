from __future__ import annotations

import hashlib
import hmac
from typing import Iterable, List

from flask import current_app


def _settings() -> tuple[str, int]:
    secret = current_app.config.get("DEVICE_CARD_SECRET") or current_app.config.get("SECRET_KEY", "")
    hmac_len = current_app.config.get("DEVICE_CARD_HMAC_LEN", 12) or 12
    try:
        hmac_len = max(4, int(hmac_len))
    except Exception:
        hmac_len = 12
    return secret, hmac_len


def compute_card_digest(uid: str, dev_id: int) -> str | None:
    """
    Compute the truncated hex HMAC for the pair (device, card).
    Base string is "<dev_id>|<uid>", digest is truncated to DEVICE_CARD_HMAC_LEN.
    """
    uid_norm = (uid or "").strip()
    if not uid_norm:
        return None
    secret, hlen = _settings()
    if not secret:
        return None
    base = f"{dev_id}|{uid_norm}"
    digest = hmac.new(secret.encode("utf-8"), base.encode("utf-8"), hashlib.sha256).hexdigest()
    return digest[:hlen]


def match_digests(uid: str, digests: Iterable[str], device_ids: Iterable[int]) -> List[dict]:
    """
    Return a list of match records per digest:
      {"digest": "...", "matches": [dev_id, ...]}
    """
    uid_norm = (uid or "").strip()
    if not uid_norm:
        return []

    clean_digests = [str(d).strip().lower() for d in digests or [] if str(d).strip()]
    device_ids = [int(d) for d in device_ids or []]

    results: List[dict] = []
    for dg in clean_digests:
        matched = []
        for dev_id in device_ids:
            expected = compute_card_digest(uid_norm, dev_id)
            if expected and expected.lower() == dg:
                matched.append(dev_id)
        results.append({"digest": dg, "matches": matched})
    return results
