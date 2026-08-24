"""GitHub Security Advisories, filtered to projects being tracked.

There is no separate "advisories feed" endpoint — this polls the GitHub
Advisory Database REST API (api.github.com/advisories), which is the
official free, structured, no-key-required source for GHSA records. Each
advisory is matched to a tracked project by comparing its
source_code_location against the repos this agent watches (explicit +
CNCF-landscape-discovered), so a CVE in some unrelated package never
makes it through.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from src.feeds import ReleaseEntry, strip_html
from src.http_client import PoliteSession

ADVISORIES_URL = "https://api.github.com/advisories"
SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
MAX_PAGES = 5  # safety cap; lookback_hours normally stops us well before this


def _repo_from_location(url: str | None) -> str | None:
    prefix = "https://github.com/"
    if not isinstance(url, str) or not url.startswith(prefix):
        return None
    parts = url[len(prefix):].strip("/").split("/")
    return f"{parts[0]}/{parts[1]}" if len(parts) >= 2 else None


def _auth_headers() -> dict:
    token = os.environ.get("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_advisories(
    session: PoliteSession,
    tracked_repos: set[str],
    lookback_hours: int,
    minimum_severity: str,
) -> list[ReleaseEntry]:
    cutoff = datetime.now(UTC) - timedelta(hours=lookback_hours)
    min_rank = SEVERITY_ORDER.get(minimum_severity, 1)
    entries: list[ReleaseEntry] = []

    url = f"{ADVISORIES_URL}?per_page=100&sort=published&direction=desc"
    headers = _auth_headers()

    for _ in range(MAX_PAGES):
        if not url:
            break
        resp = session.get(url, headers=headers)
        if resp.status_code != 200:
            break
        advisories = resp.json()
        if not advisories:
            break

        stop = False
        for adv in advisories:
            published = adv.get("published_at")
            if published:
                pub_dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
                if pub_dt < cutoff:
                    stop = True
                    break

            repo = _repo_from_location(adv.get("source_code_location"))
            if not repo or repo not in tracked_repos:
                continue
            if SEVERITY_ORDER.get(adv.get("severity"), -1) < min_rank:
                continue

            summary = strip_html(adv.get("summary", ""))
            description = strip_html(adv.get("description", ""))[:2000]
            entries.append(
                ReleaseEntry(
                    source_type="security-advisory",
                    project=repo,
                    entry_id=adv.get("ghsa_id"),
                    title=f"{adv.get('ghsa_id')}: {summary}".strip(),
                    link=adv.get("html_url", ""),
                    published=published,
                    body=f"Severity: {adv.get('severity')}\n{summary}\n\n{description}",
                )
            )

        if stop:
            break

        # RFC 5988 Link header pagination.
        next_url = None
        for part in resp.headers.get("Link", "").split(","):
            if 'rel="next"' in part:
                next_url = part.split(";")[0].strip().strip("<>")
        url = next_url

    return entries
