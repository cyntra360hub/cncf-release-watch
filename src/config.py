"""Loads and merges config/sources.yaml (hand-curated) with
config/discovered_feeds.json (generated from the CNCF landscape) into a
single view of what this agent watches.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field

import yaml

from src.cncf_landscape import load_discovered_feeds

CONFIG_DIR = pathlib.Path(__file__).parent.parent / "config"
SOURCES_PATH = CONFIG_DIR / "sources.yaml"

USER_AGENT = "cncf-release-watch/0.1 (+https://aiopscommunity.com; AiOps Community agent; contact via repo issues)"


@dataclass
class Sources:
    github_repos: set[str] = field(default_factory=set)
    gitlab_projects: set[str] = field(default_factory=set)
    cloud_changelogs: list[dict] = field(default_factory=list)
    huggingface_orgs: list[str] = field(default_factory=list)
    security_advisories: dict = field(default_factory=dict)

    def github_release_feed(self, repo: str) -> str:
        return f"https://github.com/{repo}/releases.atom"

    def gitlab_release_feed(self, project: str) -> str:
        return f"https://gitlab.com/{project}/-/releases.atom"


def load_sources() -> Sources:
    raw = yaml.safe_load(SOURCES_PATH.read_text(encoding="utf-8")) or {}
    discovered = load_discovered_feeds()

    github_repos = set(raw.get("explicit_github_repos") or []) | set(
        discovered.get("github_repos") or []
    )
    gitlab_projects = set(raw.get("explicit_gitlab_projects") or []) | set(
        discovered.get("gitlab_projects") or []
    )

    return Sources(
        github_repos=github_repos,
        gitlab_projects=gitlab_projects,
        cloud_changelogs=raw.get("cloud_changelogs") or [],
        huggingface_orgs=raw.get("huggingface_orgs") or [],
        security_advisories=raw.get("github_security_advisories") or {},
    )
