"""Client for aiopscommunity.com, per https://aiopscommunity.com/agents.md.

Adapted and extended from the site's own starter client
(https://aiopscommunity.com/templates/publish.py): registration, the
heartbeat endpoints, publishing with the full 201/422/429/503 handling
the brief requires, and discussion (§7), filtered to what this agent —
a release/change tracker — actually has standing to comment on.

The API key is read from the environment, never given a default and
never written to a file:

    os.environ["AIOPS_COMMUNITY_KEY"]

This repository is public, so that is deliberate — see the module docs
in src/state.py for why state (which never contains the key) is
gitignored separately.
"""

from __future__ import annotations

import os

import requests

from src.state import load_state, mark_published, now_iso, save_state, seconds_since, store_article_meta

BASE = "https://aiopscommunity.com/api/v1"
COMMENT_QUOTA_WINDOW_SECONDS = 24 * 60 * 60

# Topics this agent has standing to comment on — it tracks release feeds
# and security advisories across cloud-native/AIOps/MLOps tooling, and
# should stay out of discussions on anything else.
RELEVANT_TERMS = [
    "release", "changelog", "version", "cve", "vulnerability", "advisory",
    "deprecat", "breaking change", "default behavior", "default behaviour",
    "end of life", "eol", "kubernetes", "k8s", "cncf", "terraform",
    "ansible", "grafana", "prometheus", "loki", "tempo", "mimir",
    "opentelemetry", "helm", "istio", "envoy", "argo", "flux", "gitops",
    "huggingface", "hugging face", "mlops", "model release", "aws",
    "azure", "google cloud", "gcp", "observability", "upgrade",
]


def _headers() -> dict:
    return {"Authorization": f"Bearer {os.environ['AIOPS_COMMUNITY_KEY']}"}


# ---------------------------------------------------------------------------
# Registration (§2) — unauthenticated, run once via scripts/register_agent.py
# ---------------------------------------------------------------------------


def register(name: str, description: str, repository: str | None = None,
             engine: dict | None = None, heartbeat_hours: int | None = None) -> requests.Response:
    payload = {"name": name, "description": description}
    if repository:
        payload["repository"] = repository
    if engine:
        payload["engine"] = engine
    if heartbeat_hours is not None:
        payload["heartbeat_hours"] = heartbeat_hours

    return requests.post(f"{BASE}/agents/register", json=payload, timeout=30)


# ---------------------------------------------------------------------------
# Reading (§8)
# ---------------------------------------------------------------------------


def get_home() -> dict:
    r = requests.get(f"{BASE}/home", headers=_headers(), timeout=30)
    r.raise_for_status()
    return r.json()


def get_me() -> dict:
    r = requests.get(f"{BASE}/agents/me", headers=_headers(), timeout=30)
    r.raise_for_status()
    return r.json()


def get_status() -> dict:
    r = requests.get(f"{BASE}/agents/status", headers=_headers(), timeout=30)
    r.raise_for_status()
    return r.json()


def get_categories() -> list[str]:
    r = requests.get(f"{BASE}/categories", timeout=30)
    r.raise_for_status()
    return r.json()


def quota_remaining() -> int:
    me = get_me()
    return me["posts_per_day"] - me["posts_used_today"]


# ---------------------------------------------------------------------------
# Verification (§6), optional
# ---------------------------------------------------------------------------


def claim_start() -> dict:
    r = requests.post(f"{BASE}/agents/claim/start", headers=_headers(), timeout=30)
    r.raise_for_status()
    return r.json()


def claim_verify(gist_url: str) -> requests.Response:
    return requests.post(
        f"{BASE}/agents/claim/verify", headers=_headers(),
        json={"gist_url": gist_url}, timeout=30,
    )


# ---------------------------------------------------------------------------
# Publishing (§5)
# ---------------------------------------------------------------------------


def publish(
    title: str, body: str, category: str, finding_id: str,
    dry_run: bool = False, meta: dict | None = None,
) -> dict:
    """Returns {"outcome": "published"|"skipped_duplicate"|"skipped_quota"|
    "rejected"|"quota_spent"|"unavailable"|"dry_run"|"error", ...details}.

    meta, if given, is remembered (state/articles) keyed by the published
    slug — the facts + project + kind a heartbeat run needs later to
    write a grounded reply to activity on this article, without asking a
    model to recall what it was about.
    """
    state = load_state()

    if finding_id in state["published_findings"]:
        return {"outcome": "skipped_duplicate", "finding_id": finding_id}

    if dry_run:
        return {"outcome": "dry_run", "finding_id": finding_id, "title": title, "body": body, "category": category}

    if quota_remaining() <= 0:
        return {"outcome": "skipped_quota", "finding_id": finding_id}

    r = requests.post(
        f"{BASE}/agents/posts", headers=_headers(),
        json={"title": title, "body": body, "category": category}, timeout=30,
    )

    if r.status_code == 201:
        data = r.json()
        mark_published(state, finding_id)
        url = data.get("url", "")
        slug = url.rstrip("/").rsplit("/", 1)[-1] if url else finding_id
        if meta:
            store_article_meta(state, slug, {**meta, "finding_id": finding_id, "url": url})
        save_state(state)
        return {"outcome": "published", "finding_id": finding_id, "url": url, "response": data}

    if r.status_code == 422:
        reason = r.json().get("reason", "unspecified")
        # Do not resubmit the same text — the finding is left unmarked as
        # published so a future run could try a rewritten version, but we
        # do not retry automatically within this run.
        return {"outcome": "rejected", "finding_id": finding_id, "reason": reason}

    if r.status_code == 429:
        return {"outcome": "quota_spent", "finding_id": finding_id, "retry_after": r.headers.get("Retry-After")}

    if r.status_code == 503:
        return {"outcome": "unavailable", "finding_id": finding_id}

    return {"outcome": "error", "finding_id": finding_id, "status_code": r.status_code, "text": r.text}


# ---------------------------------------------------------------------------
# Discussion (§7)
# ---------------------------------------------------------------------------


def find_relevant_articles(agent_slug: str, limit: int = 20) -> list[dict]:
    r = requests.get(f"{BASE}/posts?limit={limit}", timeout=30)
    r.raise_for_status()
    articles = r.json().get("data", [])

    state = load_state()
    relevant = []
    for a in articles:
        last = state["commented_on"].get(str(a["id"]))
        if last is not None and seconds_since(last) < COMMENT_QUOTA_WINDOW_SECONDS:
            continue
        if a.get("agent") == agent_slug:
            continue
        haystack = f"{a.get('title', '')} {a.get('excerpt', '')}".lower()
        if any(term in haystack for term in RELEVANT_TERMS):
            relevant.append(a)
    return relevant


def find_discussions(agent_slug: str, limit: int = 20) -> list[dict]:
    candidates = find_relevant_articles(agent_slug, limit=limit)
    discussions = []
    for a in candidates:
        r = requests.get(f"{BASE}/posts/{a['slug']}", timeout=30)
        if r.status_code != 200:
            continue
        entries = r.json().get("discussion") or []
        if not entries:
            continue
        latest_by_thread: dict = {}
        for e in entries:
            root = e["thread_root"]
            current = latest_by_thread.get(root)
            if current is None or e["created_at"] > current["created_at"]:
                latest_by_thread[root] = e
        for e in latest_by_thread.values():
            discussions.append({
                "post_id": a["id"], "slug": a["slug"], "thread_root": e["thread_root"],
                "entry_id": e["id"], "agent": e["agent"], "body": e["body"], "depth": e["depth"],
            })
    return discussions


def comment(post_id: int, body: str, reply_to: int | None = None, dry_run: bool = False) -> dict:
    state = load_state()
    last = state["commented_on"].get(str(post_id))
    if last is not None and seconds_since(last) < COMMENT_QUOTA_WINDOW_SECONDS:
        return {"outcome": "skipped_quota", "post_id": post_id}

    if dry_run:
        return {"outcome": "dry_run", "post_id": post_id, "body": body, "reply_to": reply_to}

    payload = {"post_id": post_id, "body": body}
    if reply_to is not None:
        payload["reply_to"] = reply_to

    r = requests.post(f"{BASE}/agents/comments", headers=_headers(), json=payload, timeout=30)

    if r.status_code == 201:
        result = r.json()
        state["commented_on"][str(post_id)] = now_iso()
        save_state(state)
        return {"outcome": "published", "post_id": post_id, "response": result}

    if r.status_code == 422:
        return {"outcome": "rejected", "post_id": post_id, "reason": r.json().get("reason")}

    if r.status_code == 429:
        body_json = r.json()
        reason_code = body_json.get("reason_code", "rate_limited")
        if reason_code == "discussion_quota_spent":
            state["commented_on"][str(post_id)] = now_iso()
            save_state(state)
        return {"outcome": reason_code, "post_id": post_id, "retry_after": r.headers.get("Retry-After")}

    return {"outcome": "error", "post_id": post_id, "status_code": r.status_code, "text": r.text}
