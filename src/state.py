"""Local state so a re-run never duplicates work.

Tracks: per-feed conditional-request caching (ETag/Last-Modified), which
release/advisory entries have already been evaluated at all (so we don't
re-judge the same old release every run), which findings have been
published, and which articles we've already commented on and when.

state/ is gitignored — this is runtime data, not source code.
"""

from __future__ import annotations

import json
import pathlib
from datetime import UTC, datetime

STATE_PATH = pathlib.Path(__file__).parent.parent / "state" / "agent_state.json"

_DEFAULT_STATE = {
    "feed_cache": {},  # url -> {"etag": ..., "last_modified": ...}
    "seen_entry_ids": [],  # entry_ids already evaluated this or a prior run
    "published_findings": [],  # finding_id -> already turned into an article
    "commented_on": {},  # post_id (str) -> ISO timestamp of our last entry there
    "articles": {},  # slug -> {finding_id, project, kind, facts, post_id, url}
}


def load_state() -> dict:
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        for key, default in _DEFAULT_STATE.items():
            state.setdefault(key, default() if callable(default) else default)
        return state
    return json.loads(json.dumps(_DEFAULT_STATE))


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def get_feed_cache(state: dict, url: str) -> tuple[str | None, str | None]:
    entry = state["feed_cache"].get(url, {})
    return entry.get("etag"), entry.get("last_modified")


def set_feed_cache(state: dict, url: str, etag: str | None, last_modified: str | None) -> None:
    if etag or last_modified:
        state["feed_cache"][url] = {"etag": etag, "last_modified": last_modified}


def mark_seen(state: dict, entry_id: str) -> None:
    # Cap growth: keep the most recent 20k ids rather than an unbounded list.
    seen = state["seen_entry_ids"]
    if entry_id not in seen:
        seen.append(entry_id)
    if len(seen) > 20_000:
        state["seen_entry_ids"] = seen[-20_000:]


def is_seen(state: dict, entry_id: str) -> bool:
    return entry_id in state["seen_entry_ids"]


def mark_published(state: dict, finding_id: str) -> None:
    if finding_id not in state["published_findings"]:
        state["published_findings"].append(finding_id)


def is_published(state: dict, finding_id: str) -> bool:
    return finding_id in state["published_findings"]


def seconds_since(iso_timestamp: str) -> float:
    then = datetime.fromisoformat(iso_timestamp)
    return (datetime.now(UTC) - then).total_seconds()


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def store_article_meta(state: dict, slug: str, meta: dict) -> None:
    """Remembers the facts an article was built on, keyed by slug, so a
    later heartbeat run can write a grounded reply instead of guessing."""
    state["articles"][slug] = meta


def get_article_meta(state: dict, slug: str) -> dict | None:
    return state["articles"].get(slug)
