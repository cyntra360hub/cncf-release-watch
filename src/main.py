"""Orchestrator: two entry points.

  python -m src.main run [--dry-run] [--max-repos N]
      Fetch every source, decide what's worth publishing, write it up,
      publish it. This is what the scheduled workflow runs every 6 hours.

  python -m src.main heartbeat [--dry-run]
      GET /api/v1/home, act on what_to_do_next: reply to activity on our
      own articles, then join active discussions we have standing on.
"""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from src import aiops_client, llm
from src.analysis import Candidate, find_candidates
from src.cncf_landscape import refresh_discovered_feeds
from src.config import load_sources
from src.feeds import ReleaseEntry, fetch_cloud_changelog, fetch_github_releases, fetch_gitlab_releases
from src.http_client import PoliteSession
from src.huggingface_source import fetch_huggingface_entries
from src.security_advisories import fetch_advisories
from src.state import (
    get_feed_cache,
    is_seen,
    load_state,
    mark_seen,
    save_state,
    set_feed_cache,
)

RELEASE_LOOKBACK_HOURS = 72  # only entries published within this window are candidates
FALLBACK_CATEGORIES = [
    "AiOps", "Advanced Concepts", "AiOps Ecosystem", "AiOps Tutorial",
    "Architecture & Implementation", "Cloud in AIOps", "DevOps in AIOps Tutorials",
    "DevSecOps in AIOps", "Events / Announcements", "FinOps In AiOps", "Fundamentals",
    "Market & Trends", "MLOps In AiOps", "MLOps in AIOps Tutorials", "Observability",
    "Product Updates", "Security in AIOps", "Technical Deep Dives", "Tools & Platforms",
    "Tutorials", "Use Cases", "Software Testing",
]
_OBSERVABILITY_HINTS = (
    "grafana", "loki", "tempo", "mimir", "prometheus", "opentelemetry",
    "jaeger", "observability", "vector",
)


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def _fetch_all_entries(session: PoliteSession, state: dict, sources, max_workers: int, max_repos: int | None) -> tuple[list[ReleaseEntry], int]:
    entries: list[ReleaseEntry] = []
    errors = 0

    github_repos = sorted(sources.github_repos)
    gitlab_projects = sorted(sources.gitlab_projects)
    if max_repos is not None:
        github_repos = github_repos[:max_repos]
        gitlab_projects = gitlab_projects[:max_repos]

    print(f"Fetching {len(github_repos)} GitHub release feeds and {len(gitlab_projects)} GitLab release feeds...")

    def do_github(repo: str):
        url = sources.github_release_feed(repo)
        etag, lm = get_feed_cache(state, url)
        return url, fetch_github_releases(session, repo, etag, lm)

    def do_gitlab(project: str):
        url = sources.gitlab_release_feed(project)
        etag, lm = get_feed_cache(state, url)
        return url, fetch_gitlab_releases(session, project, etag, lm)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(do_github, r) for r in github_repos]
        futures += [pool.submit(do_gitlab, p) for p in gitlab_projects]
        for fut in as_completed(futures):
            url, result = fut.result()
            if result.status == "ok":
                set_feed_cache(state, url, result.etag, result.last_modified)
                entries.extend(result.entries)
            elif result.status == "error":
                errors += 1

    print(f"  -> {len(entries)} release entries fetched, {errors} feed errors")

    print(f"Fetching {len(sources.cloud_changelogs)} cloud provider changelogs...")
    for changelog in sources.cloud_changelogs:
        url = changelog["url"]
        etag, lm = get_feed_cache(state, url)
        result = fetch_cloud_changelog(session, changelog["name"], url, etag, lm)
        if result.status == "ok":
            set_feed_cache(state, url, result.etag, result.last_modified)
            entries.extend(result.entries)
        elif result.status == "error":
            errors += 1

    print(f"Fetching Hugging Face model/dataset activity for {len(sources.huggingface_orgs)} orgs...")
    entries.extend(fetch_huggingface_entries(session, sources.huggingface_orgs, lookback_hours=RELEASE_LOOKBACK_HOURS))

    adv_cfg = sources.security_advisories
    if adv_cfg.get("enabled"):
        print("Fetching GitHub Security Advisories for tracked repos...")
        entries.extend(
            fetch_advisories(
                session,
                sources.github_repos,
                lookback_hours=adv_cfg.get("lookback_hours", 72),
                minimum_severity=adv_cfg.get("minimum_severity", "medium"),
            )
        )

    return entries, errors


def _new_entries(state: dict, entries: list[ReleaseEntry]) -> list[ReleaseEntry]:
    """Entries not already marked seen. Does NOT mark them seen itself —
    that happens later, only once each entry reaches a terminal outcome
    (see _mark_terminal_entries_seen). Marking seen here, at fetch time,
    would permanently drop any candidate that simply lost the daily
    publish-quota race: real feed volume regularly exceeds the 2-5/day
    quota, and quota resets tomorrow — a candidate that didn't get a
    turn today deserves another shot, not silent, permanent loss.
    """
    return [e for e in entries if e.entry_id and not is_seen(state, e.entry_id)]


# Outcomes that mean "try this finding again another run" rather than
# "this is settled" — an entry behind one of these must NOT be marked
# seen, or it would never get a second chance once its underlying cause
# clears (quota resets daily; a transient write failure isn't the
# finding's fault).
_RETRYABLE_OUTCOMES = {
    "skipped_quota", "quota_spent", "unavailable",
    "write_error", "invalid_title_length", "body_too_short",
    "dry_run",
}


def _candidate_entry_ids(candidate: Candidate) -> list[str]:
    if candidate.kind == "pattern":
        return [e.entry_id for e in candidate.related if e.entry_id]
    return [candidate.primary.entry_id] if candidate.primary.entry_id else []


def _within_lookback(entries: list[ReleaseEntry], hours: int) -> list[ReleaseEntry]:
    import datetime as dt

    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours)
    kept = []
    for e in entries:
        if not e.published:
            continue
        try:
            published = dt.datetime.fromisoformat(str(e.published).replace("Z", "+00:00"))
        except ValueError:
            from email.utils import parsedate_to_datetime

            try:
                published = parsedate_to_datetime(e.published)
            except (TypeError, ValueError):
                continue
        if published.tzinfo is None:
            published = published.replace(tzinfo=dt.UTC)
        if published >= cutoff:
            kept.append(e)
    return kept


# ---------------------------------------------------------------------------
# Writing + publishing
# ---------------------------------------------------------------------------


def _source_description(entry: ReleaseEntry) -> str:
    if entry.source_type == "github-release":
        return f"the project's GitHub release notes for {entry.project}"
    if entry.source_type == "gitlab-release":
        return f"the project's GitLab release notes for {entry.project}"
    if entry.source_type == "cloud-changelog":
        return f"{entry.project}'s official changelog"
    if entry.source_type == "security-advisory":
        return "the GitHub Security Advisory database"
    if entry.source_type.startswith("huggingface"):
        return "the project's Hugging Face repository metadata"
    return "the project's release notes"


def _facts_for(candidate: Candidate) -> tuple[str, str]:
    """Returns (project_display, facts_text)."""
    if candidate.kind == "pattern":
        related = candidate.related[:6]
        project_display = ", ".join(sorted({e.project for e in related}))
        facts = "\n\n".join(
            f"Project: {e.project}\nTitle: {e.title}\nPublished: {e.published}\n{e.body}"
            for e in related
        )
        return project_display, facts

    e = candidate.primary
    facts = f"Title: {e.title}\nProject: {e.project}\nPublished: {e.published}\n\n{e.body}"
    return e.project, facts


def _pick_category(candidate: Candidate, valid_categories: list[str]) -> str:
    entry = candidate.primary
    guess = "Tools & Platforms"
    if candidate.kind == "security":
        guess = "Security in AIOps"
    elif entry.source_type.startswith("huggingface"):
        guess = "MLOps In AiOps"
    elif entry.source_type == "cloud-changelog":
        guess = "Cloud in AIOps"
    elif candidate.kind == "pattern":
        guess = "AiOps Ecosystem"
    elif any(hint in entry.project.lower() for hint in _OBSERVABILITY_HINTS):
        guess = "Observability"

    if guess in valid_categories:
        return guess
    return "Tools & Platforms" if "Tools & Platforms" in valid_categories else valid_categories[0]


def _process_candidates(candidates: list[Candidate], dry_run: bool) -> list[dict]:
    try:
        categories = aiops_client.get_categories()
    except Exception as exc:
        print(f"Could not fetch live category list ({exc}); using last-known list")
        categories = FALLBACK_CATEGORIES

    # Priority: security first, then default-behavior/deprecation, then patterns.
    order = {"security": 0, "default-behavior": 1, "deprecation": 1, "pattern": 2}
    candidates = sorted(candidates, key=lambda c: order.get(c.kind, 3))

    results = []
    for candidate in candidates:
        project, facts = _facts_for(candidate)
        source_description = _source_description(candidate.primary)

        try:
            article = llm.write_article(candidate.kind, project, source_description, facts)
        except Exception as exc:
            results.append({"outcome": "write_error", "finding_id": candidate.finding_id, "error": str(exc)})
            continue

        title, body = article["title"], article["body"]
        if not (10 <= len(title) <= 140):
            results.append({"outcome": "invalid_title_length", "finding_id": candidate.finding_id, "title": title})
            continue
        if len(body) < 200:
            results.append({"outcome": "body_too_short", "finding_id": candidate.finding_id, "length": len(body)})
            continue

        category = _pick_category(candidate, categories)
        meta = {"project": project, "kind": candidate.kind, "facts": facts}
        # source_url backs the specific factual claim per skill.md — an
        # optional but effectively required field for any article naming a
        # project, or the moderator rejects it as an uncited named-entity
        # claim. candidate.primary.link is always the actual release/
        # advisory/changelog URL the facts were pulled from.
        outcome = aiops_client.publish(
            title, body, category, candidate.finding_id, dry_run=dry_run,
            meta=meta, source_url=candidate.primary.link or None,
        )
        outcome["title"] = title
        outcome["body"] = body
        outcome["category"] = category
        outcome["reasons"] = candidate.reasons
        results.append(outcome)

        if not dry_run:
            time.sleep(3)  # courtesy pacing between submissions to the moderator

        if outcome["outcome"] in ("skipped_quota", "quota_spent"):
            print("Daily quota reached — stopping publish loop for this run")
            break

    return results


def run(dry_run: bool, max_repos: int | None = None, max_workers: int = 8) -> None:
    print("Refreshing CNCF landscape-derived feed list...")
    try:
        payload = refresh_discovered_feeds()
        print(f"  -> {len(payload['github_repos'])} GitHub repos, {len(payload['gitlab_projects'])} GitLab projects discovered")
    except Exception as exc:
        print(f"  landscape refresh failed ({exc}); using last cached discovered_feeds.json")

    sources = load_sources()
    state = load_state()
    session = PoliteSession()

    all_entries, errors = _fetch_all_entries(session, state, sources, max_workers, max_repos)
    if not dry_run:
        save_state(state)  # persist feed cache even if the rest of the run fails

    fresh_entries = _new_entries(state, all_entries)

    candidate_entries = _within_lookback(fresh_entries, RELEASE_LOOKBACK_HOURS)
    print(f"{len(fresh_entries)} entries not seen before, {len(candidate_entries)} within the {RELEASE_LOOKBACK_HOURS}h lookback window")

    candidates = find_candidates(candidate_entries)
    print(f"{len(candidates)} candidates cleared the operational-relevance filter")

    # Fresh entries that never even became a candidate (no operational
    # signal) are settled for good — mark them seen now so future runs
    # don't keep re-classifying the same routine releases.
    candidate_entry_ids = {eid for c in candidates for eid in _candidate_entry_ids(c)}
    for e in fresh_entries:
        if e.entry_id and e.entry_id not in candidate_entry_ids:
            mark_seen(state, e.entry_id)
    if not dry_run:
        save_state(state)

    if not candidates:
        print("Nothing material this run. Publishing nothing — that is the correct outcome most days.")
        return

    results = _process_candidates(candidates, dry_run=dry_run)

    # aiops_client.publish() does its own load_state()/save_state() per
    # candidate above, so the `state` object held since the top of this
    # function is now stale — reload before marking seen and saving, or
    # this write clobbers the published_findings/articles data publish()
    # already persisted.
    if not dry_run:
        state = load_state()

    candidates_by_finding_id = {c.finding_id: c for c in candidates}
    for r in results:
        if r["outcome"] in _RETRYABLE_OUTCOMES:
            continue  # leave unseen so a later run retries this finding
        candidate = candidates_by_finding_id.get(r.get("finding_id"))
        if candidate:
            for eid in _candidate_entry_ids(candidate):
                mark_seen(state, eid)
    if not dry_run:
        save_state(state)

    print("\n=== Run summary ===")
    for r in results:
        print(f"[{r['outcome']}] {r.get('finding_id', '')} — {r.get('title', '')}")
        if r["outcome"] == "dry_run":
            print(f"    category: {r['category']}")
            print(f"    body:\n{r['body']}\n")
        elif r["outcome"] == "rejected":
            print(f"    reason: {r.get('reason')} (reason_code={r.get('reason_code')})")
        elif r["outcome"] == "unavailable":
            print(f"    detail: {r.get('detail')}")
        elif r["outcome"] in ("write_error", "invalid_title_length", "body_too_short"):
            print(f"    error: {r.get('error') or r.get('title') or r.get('length')}")


# ---------------------------------------------------------------------------
# Heartbeat (§4)
# ---------------------------------------------------------------------------


def heartbeat(dry_run: bool) -> None:
    home = aiops_client.get_home()
    account = home.get("your_account", {})
    print(f"Account: {account}")

    print("\nwhat_to_do_next:")
    for item in home.get("what_to_do_next", []):
        print(f"  - {item}")

    state = load_state()

    # Reply to activity on our own articles first (§10 priority 6).
    for activity in home.get("activity_on_your_articles", []):
        slug = activity.get("article_slug")
        meta = state["articles"].get(slug)
        if not meta:
            print(f"No stored facts for {slug} — skipping reply (can't ground it)")
            continue

        r = requests.get(f"{aiops_client.BASE}/posts/{slug}", timeout=30)
        if r.status_code != 200:
            continue
        post = r.json()
        discussion = post.get("discussion") or []
        if not discussion:
            continue
        latest = max(discussion, key=lambda e: e["created_at"])

        try:
            reply_body = llm.write_reply(meta["project"], meta["facts"], latest["body"])
        except Exception as exc:
            print(f"Could not draft reply for {slug}: {exc}")
            continue

        outcome = aiops_client.comment(post["id"], reply_body, reply_to=latest["id"], dry_run=dry_run)
        print(f"Reply on {slug}: {outcome['outcome']}")

    # Join discussions we have standing on, when we have something concrete.
    agent_slug = account.get("slug", "")
    discussions = aiops_client.find_discussions(agent_slug, limit=20)
    joined = 0
    for d in discussions:
        # Only join if this agent has a stored finding whose project is
        # actually named in the thread — otherwise there's nothing
        # concrete to add, just a keyword coincidence.
        matching = [m for m in state["articles"].values() if m["project"].lower() in d["body"].lower()]
        if not matching:
            continue
        meta = matching[0]
        try:
            reply_body = llm.write_reply(meta["project"], meta["facts"], d["body"])
        except Exception as exc:
            print(f"Could not draft discussion reply on {d['slug']}: {exc}")
            continue
        outcome = aiops_client.comment(d["post_id"], reply_body, reply_to=d["entry_id"], dry_run=dry_run)
        print(f"Joined discussion on {d['slug']}: {outcome['outcome']}")
        joined += 1

    if discussions and not joined:
        print(f"{len(discussions)} discussion entries matched our relevance terms, "
              f"but none named a project we have stored facts for — nothing to join")


def main() -> None:
    parser = argparse.ArgumentParser(description="cncf-release-watch")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Fetch, analyze, write, publish")
    run_p.add_argument("--dry-run", action="store_true")
    run_p.add_argument("--max-repos", type=int, default=None, help="Cap repos per host, for local testing")
    run_p.add_argument("--max-workers", type=int, default=8)

    hb_p = sub.add_parser("heartbeat", help="GET /home and act on it")
    hb_p.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()

    if args.command == "run":
        run(dry_run=args.dry_run, max_repos=args.max_repos, max_workers=args.max_workers)
    elif args.command == "heartbeat":
        heartbeat(dry_run=args.dry_run)


if __name__ == "__main__":
    start = time.monotonic()
    try:
        main()
    finally:
        print(f"\nDone in {time.monotonic() - start:.1f}s", file=sys.stderr)
