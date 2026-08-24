"""Fetches and normalizes GitHub/GitLab release feeds and generic
RSS/Atom changelogs into a common ReleaseEntry shape.

Facts only flow from here. Nothing in this module ever asks a model
what changed — it reads what the feed itself says.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

import feedparser

from src.http_client import PoliteSession

_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(raw: str | None) -> str:
    if not raw:
        return ""
    text = _TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    return re.sub(r"[ \t]+", " ", text).strip()


@dataclass
class ReleaseEntry:
    source_type: str  # "github-release" | "gitlab-release" | "cloud-changelog"
    project: str  # display name, e.g. "grafana/grafana" or "AWS What's New"
    entry_id: str  # stable id for dedup (feed guid/link)
    title: str
    link: str
    published: str | None
    body: str  # plain-text facts pulled from the feed itself


class FetchResult:
    def __init__(self, status: str, entries: list[ReleaseEntry], etag=None, last_modified=None):
        self.status = status  # "ok" | "not_modified" | "error"
        self.entries = entries
        self.etag = etag
        self.last_modified = last_modified


def _fetch_and_parse(
    session: PoliteSession, url: str, etag: str | None, last_modified: str | None
) -> tuple[str, feedparser.FeedParserDict | None, str | None, str | None]:
    try:
        resp = session.get_conditional(url, etag=etag, last_modified=last_modified)
    except Exception as exc:  # network errors: treat as transient, skip this run
        return "error", None, None, f"{type(exc).__name__}: {exc}"

    if resp.status_code == 304:
        return "not_modified", None, etag, last_modified
    if resp.status_code == 404:
        return "not_found", None, None, None
    if resp.status_code >= 400:
        return "error", None, None, f"HTTP {resp.status_code}"

    parsed = feedparser.parse(resp.content)
    new_etag = resp.headers.get("ETag")
    new_last_modified = resp.headers.get("Last-Modified")
    return "ok", parsed, new_etag, new_last_modified


def fetch_github_releases(
    session: PoliteSession, repo: str, etag: str | None, last_modified: str | None
) -> FetchResult:
    url = f"https://github.com/{repo}/releases.atom"
    status, parsed, new_etag, new_lm = _fetch_and_parse(session, url, etag, last_modified)
    if status != "ok":
        return FetchResult(status, [], new_etag, new_lm)

    entries = []
    for e in parsed.entries:
        entries.append(
            ReleaseEntry(
                source_type="github-release",
                project=repo,
                entry_id=e.get("id") or e.get("link"),
                title=e.get("title", "").strip(),
                link=e.get("link", ""),
                published=e.get("published") or e.get("updated"),
                body=strip_html(e.get("content", [{}])[0].get("value") if e.get("content") else e.get("summary")),
            )
        )
    return FetchResult("ok", entries, new_etag, new_lm)


def fetch_gitlab_releases(
    session: PoliteSession, project: str, etag: str | None, last_modified: str | None
) -> FetchResult:
    url = f"https://gitlab.com/{project}/-/releases.atom"
    status, parsed, new_etag, new_lm = _fetch_and_parse(session, url, etag, last_modified)
    if status != "ok":
        return FetchResult(status, [], new_etag, new_lm)

    entries = []
    for e in parsed.entries:
        entries.append(
            ReleaseEntry(
                source_type="gitlab-release",
                project=project,
                entry_id=e.get("id") or e.get("link"),
                title=e.get("title", "").strip(),
                link=e.get("link", ""),
                published=e.get("published") or e.get("updated"),
                body=strip_html(e.get("summary")),
            )
        )
    return FetchResult("ok", entries, new_etag, new_lm)


def fetch_cloud_changelog(
    session: PoliteSession,
    name: str,
    url: str,
    etag: str | None,
    last_modified: str | None,
) -> FetchResult:
    status, parsed, new_etag, new_lm = _fetch_and_parse(session, url, etag, last_modified)
    if status != "ok":
        return FetchResult(status, [], new_etag, new_lm)

    entries = []
    for e in parsed.entries:
        entries.append(
            ReleaseEntry(
                source_type="cloud-changelog",
                project=name,
                entry_id=e.get("id") or e.get("guid") or e.get("link"),
                title=e.get("title", "").strip(),
                link=e.get("link", ""),
                published=e.get("published") or e.get("updated"),
                body=strip_html(e.get("summary") or e.get("description")),
            )
        )
    return FetchResult("ok", entries, new_etag, new_lm)
