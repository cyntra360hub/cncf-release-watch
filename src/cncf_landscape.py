"""
Seeds the tracked-project list from the CNCF landscape.

The landscape publishes a machine-readable list of every project in the
ecosystem at LANDSCAPE_URL. We walk it, pull out every GitHub- and
GitLab-hosted repo_url (including additional_repos — some landscape
entries list more than one repo), and write the result to
config/discovered_feeds.json.

That file is regenerated wholesale each time this runs — it is derived
data, not hand-curated, so it is kept separate from config/sources.yaml
(see that file's header comment).

A quirk of the landscape2 YAML schema worth knowing if you edit this:
each list entry looks like

    - item:
      name: Airship
      homepage_url: ...
      repo_url: ...

which reads as a single mapping {item: null, name: ..., homepage_url: ...,
repo_url: ...} — "item:" is a bare marker key with a null value, not a
parent key that the other fields nest under. So the node *containing* the
'item' key IS the item record, not node['item'].
"""

from __future__ import annotations

import json
import pathlib
from datetime import UTC, datetime

import requests
import yaml

LANDSCAPE_URL = "https://raw.githubusercontent.com/cncf/landscape/master/landscape.yml"
OUTPUT_PATH = pathlib.Path(__file__).parent.parent / "config" / "discovered_feeds.json"
USER_AGENT = "cncf-release-watch/0.1 (+https://aiopscommunity.com; AiOps Community agent)"


def _walk_items(node, out):
    if isinstance(node, dict):
        if "item" in node and "name" in node:
            out.append(node)
            return
        for v in node.values():
            _walk_items(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_items(v, out)


def _repo_slug(repo_url: str, host: str) -> str | None:
    prefix = f"https://{host}/"
    if not isinstance(repo_url, str) or not repo_url.startswith(prefix):
        return None
    slug = repo_url[len(prefix):].strip("/")
    if slug.endswith(".git"):
        slug = slug[: -len(".git")]
    # A repo slug is org/repo (or group/subgroup/project for GitLab) —
    # anything with no slash at all isn't a repo path.
    return slug if "/" in slug else None


def fetch_landscape_items() -> list[dict]:
    resp = requests.get(LANDSCAPE_URL, headers={"User-Agent": USER_AGENT}, timeout=60)
    resp.raise_for_status()
    data = yaml.safe_load(resp.text)
    items: list[dict] = []
    _walk_items(data.get("landscape", []), items)
    return items


def extract_repo_slugs(items: list[dict]) -> tuple[set[str], set[str]]:
    github_repos: set[str] = set()
    gitlab_projects: set[str] = set()

    for item in items:
        repo_urls = [item.get("repo_url")]
        for extra in item.get("additional_repos") or []:
            if isinstance(extra, dict):
                repo_urls.append(extra.get("repo_url"))

        for repo_url in repo_urls:
            gh = _repo_slug(repo_url, "github.com")
            if gh:
                github_repos.add(gh)
            gl = _repo_slug(repo_url, "gitlab.com")
            if gl:
                gitlab_projects.add(gl)

    return github_repos, gitlab_projects


def refresh_discovered_feeds() -> dict:
    """Fetch the landscape, extract repos, write discovered_feeds.json, return it."""
    items = fetch_landscape_items()
    github_repos, gitlab_projects = extract_repo_slugs(items)

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "source": LANDSCAPE_URL,
        "landscape_item_count": len(items),
        "github_repos": sorted(github_repos),
        "gitlab_projects": sorted(gitlab_projects),
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def load_discovered_feeds() -> dict:
    if not OUTPUT_PATH.exists():
        return {"github_repos": [], "gitlab_projects": []}
    return json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))


if __name__ == "__main__":
    payload = refresh_discovered_feeds()
    print(
        f"Landscape items: {payload['landscape_item_count']} | "
        f"GitHub repos: {len(payload['github_repos'])} | "
        f"GitLab projects: {len(payload['gitlab_projects'])}"
    )
