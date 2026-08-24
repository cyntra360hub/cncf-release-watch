"""Hugging Face model and dataset release tracking.

Hugging Face doesn't publish per-repo RSS, so this polls the models/
datasets list APIs (huggingface.co/api/models, /api/datasets) filtered
to the orgs in config/sources.yaml's huggingface_orgs, sorted by
lastModified. Only repos *created* within the lookback window count as
a release — lastModified also fires on routine metadata edits, which
would otherwise flood every run with noise.

Facts are limited to what the API actually returns: tags, library,
license, file list. There is no changelog text here, so most of these
entries won't clear the "operationally material" bar in analysis.py —
that's expected, not a bug.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.feeds import ReleaseEntry
from src.http_client import PoliteSession

API_BASE = "https://huggingface.co/api"


def _fetch_repo_type(
    session: PoliteSession, repo_type: str, org: str, lookback_hours: int, limit: int = 20
) -> list[ReleaseEntry]:
    url = f"{API_BASE}/{repo_type}?author={org}&sort=lastModified&direction=-1&limit={limit}&full=true"
    resp = session.get(url)
    if resp.status_code != 200:
        return []

    cutoff = datetime.now(UTC) - timedelta(hours=lookback_hours)
    singular = "model" if repo_type == "models" else "dataset"
    entries: list[ReleaseEntry] = []

    for repo in resp.json():
        created_raw = repo.get("createdAt")
        if not created_raw:
            continue
        created_dt = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
        if created_dt < cutoff:
            continue

        repo_id = repo.get("id", "")
        sha = repo.get("sha", "")
        tags = ", ".join(repo.get("tags") or [])
        library = repo.get("library_name", "unspecified")
        files = ", ".join(s.get("rfilename", "") for s in (repo.get("siblings") or [])[:15])

        entries.append(
            ReleaseEntry(
                source_type=f"huggingface-{singular}",
                project=repo_id,
                entry_id=f"hf-{singular}:{repo_id}:{sha}",
                title=f"New {singular} on Hugging Face: {repo_id}",
                link=f"https://huggingface.co/{'datasets/' if repo_type == 'datasets' else ''}{repo_id}",
                published=created_raw,
                body=(
                    f"Hugging Face {singular} {repo_id} created {created_raw}. "
                    f"Library: {library}. Tags: {tags}. Files: {files}."
                ),
            )
        )

    return entries


def fetch_huggingface_entries(
    session: PoliteSession, orgs: list[str], lookback_hours: int = 24
) -> list[ReleaseEntry]:
    entries: list[ReleaseEntry] = []
    for org in orgs:
        entries.extend(_fetch_repo_type(session, "models", org, lookback_hours))
        entries.extend(_fetch_repo_type(session, "datasets", org, lookback_hours))
    return entries
