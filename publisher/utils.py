from __future__ import annotations

import hashlib


def file_key(tweet_external_id: str) -> str:
    """Deterministic short key for a tweet, used as filename prefix."""
    return hashlib.sha1(tweet_external_id.encode("utf-8")).hexdigest()[:16]
