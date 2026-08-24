"""Decides what's worth publishing, from facts alone — no model involved.

Publish-worthy, per the brief:
  - a change alters default behaviour in a way that affects running systems
  - a deprecation or removal requires action before upgrading
  - a security advisory affects a widely-run tool
  - two or more tracked projects shipped a related change in the same window

Everything else — routine releases, anything with no operational
signal — is filtered out here, before the LLM ever sees it. Most runs
should find nothing. That's correct, not a failure of the heuristics.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from src.feeds import ReleaseEntry

DEFAULT_BEHAVIOR_KEYWORDS = [
    "now defaults to",
    "default is now",
    "default has changed",
    "default value has changed",
    "changes the default",
    "changed the default",
    "default behavior",
    "default behaviour",
    "breaking change",
    "breaking:",
    "backward incompatible",
    "backwards incompatible",
    "backward-incompatible",
    "incompatible change",
    "action required",
    "migration required",
]

DEPRECATION_KEYWORDS = [
    "deprecat",
    "end of life",
    "end-of-life",
    " eol ",
    "no longer supported",
    "removed support",
    "remove support",
    "removal of",
    "sunset",
    "will be removed",
    "has been removed",
    "unsupported",
]

# Tokens too common in release notes to signal a genuine cross-project
# pattern on their own (release/version/fix boilerplate, common words).
GENERIC_TITLE_TERMS = {
    "release", "released", "version", "update", "updated", "changelog",
    "notes", "changes", "change", "new", "added", "add", "improve",
    "improved", "improvement", "improvements", "fix", "fixes", "fixed",
    "bump", "bugfix", "bugfixes", "patch", "patches", "minor", "major",
    "stable", "final", "general", "availability", "announcing", "announce",
    "introducing", "support", "supports", "supported", "with", "from",
    "into", "this", "that", "your", "their", "have", "will", "which",
}

_TITLE_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9._+-]{4,}")


@dataclass
class Candidate:
    kind: str  # "security" | "default-behavior" | "deprecation" | "pattern"
    primary: ReleaseEntry
    reasons: list[str]
    related: list[ReleaseEntry] = field(default_factory=list)

    @property
    def finding_id(self) -> str:
        if self.kind == "pattern":
            ids = "|".join(sorted(e.entry_id for e in self.related))
            return f"pattern:{ids}"
        return f"{self.kind}:{self.primary.entry_id}"


def classify_entry(entry: ReleaseEntry) -> Candidate | None:
    if entry.source_type == "security-advisory":
        return Candidate(
            kind="security",
            primary=entry,
            reasons=["GitHub Security Advisory affecting a tracked, widely-run project"],
        )

    text = f"{entry.title}\n{entry.body}".lower()

    if any(kw in text for kw in DEFAULT_BEHAVIOR_KEYWORDS):
        return Candidate(
            kind="default-behavior",
            primary=entry,
            reasons=["Release notes describe a change to default behavior"],
        )

    if any(kw in text for kw in DEPRECATION_KEYWORDS):
        return Candidate(
            kind="deprecation",
            primary=entry,
            reasons=["Release notes describe a deprecation, removal, or end-of-life"],
        )

    return None


def _title_terms(title: str) -> set[str]:
    terms = {t.lower() for t in _TITLE_TOKEN_RE.findall(title)}
    return terms - GENERIC_TITLE_TERMS


def find_cross_project_patterns(entries: list[ReleaseEntry], min_projects: int = 2) -> list[Candidate]:
    """Crude but precision-biased: only title-level shared terms count,
    generic release-note boilerplate is excluded, and a project only
    contributes once per term. This will miss real patterns phrased in
    different words and will occasionally surface a coincidental one —
    the LLM prompt still requires concrete shared facts before it writes
    anything, which is the second filter against false positives.
    """
    term_to_entries: dict[str, list[ReleaseEntry]] = defaultdict(list)
    term_to_projects: dict[str, set[str]] = defaultdict(set)

    for entry in entries:
        for term in _title_terms(entry.title):
            if entry.project not in term_to_projects[term]:
                term_to_projects[term].add(entry.project)
                term_to_entries[term].append(entry)

    patterns = []
    seen_project_sets: set[tuple[str, ...]] = set()
    for term, projects in term_to_projects.items():
        if len(projects) < min_projects:
            continue
        key = tuple(sorted(projects))
        if key in seen_project_sets:
            continue
        seen_project_sets.add(key)
        related = term_to_entries[term]
        patterns.append(
            Candidate(
                kind="pattern",
                primary=related[0],
                reasons=[
                    f"Shared theme '{term}' appears in the same window across "
                    f"{len(projects)} tracked projects: {', '.join(sorted(projects))}"
                ],
                related=related,
            )
        )
    return patterns


def find_candidates(entries: list[ReleaseEntry]) -> list[Candidate]:
    candidates = [c for e in entries if (c := classify_entry(e)) is not None]
    candidates.extend(find_cross_project_patterns(entries))
    return candidates
