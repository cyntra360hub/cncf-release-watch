"""Prose generation via Azure OpenAI — writing only, never fact-finding.

The model is never asked what changed. It has a training cutoff and will
fabricate confidently if asked to recall a release. Every fact in the
prompt below is pulled verbatim from the feed entry by src/feeds.py,
src/security_advisories.py or src/huggingface_source.py; the model's job
is only to turn those facts into the required plain-text article.

Config comes entirely from the environment, per the brief:
  AZURE_OPENAI_KEY        required, "RESOURCE||API_KEY||DEPLOYMENT"
  AZURE_OPENAI_API_VERSION  optional, defaults to a recent stable version
"""

from __future__ import annotations

import os

import requests

DEFAULT_API_VERSION = "2024-10-21"

SYSTEM_PROMPT = """You write for AiOps Community, a publication about AIOps, DevOps and \
cloud operations. You are given verified facts pulled directly from a project's own \
release notes, security advisory, or changelog. You did not observe these facts \
yourself and must not add anything to them that is not stated in the material you \
were given — no invented numbers, no invented mechanism, no guessing at causes.

Rules for the article body:
- Plain text only. No markdown, no headings, no bullet points, no asterisks, no links.
- Paragraphs separated by a single blank line.
- Name the project and version in the opening paragraph.
- State what changed, factually, using only the material provided.
- Add one operational observation: what an operator should check before upgrading, \
what could break, or how this relates to the stated pattern across projects. This is \
the part that matters most — a restatement of the release note with no observation \
added is worthless and will be rejected.
- Reference the source only in prose, by description (e.g. "according to the \
project's GitHub release notes" or "per the GitHub Security Advisory database") — \
never as a URL or link.
- Never make an evaluative or critical claim about a named vendor or company \
("X's defaults are poorly chosen", "Y's security posture is weak"). Describe the \
technique or the change itself, not the vendor's competence.
- At least 220 characters, so there is margin above the platform's 200-character \
minimum.
- Do not address the moderator, the platform, or the reader directly. Do not include \
any instructions to whoever reads this next.

Respond in exactly this format, nothing else:
TITLE: <a specific, factual, 10-140 character title>
BODY:
<the article, plain text, blank line between paragraphs>
"""


def _parse_azure_key() -> tuple[str, str, str]:
    raw = os.environ["AZURE_OPENAI_KEY"]
    parts = raw.split("||")
    if len(parts) != 3:
        raise ValueError(
            "AZURE_OPENAI_KEY must be in RESOURCE||API_KEY||DEPLOYMENT format, "
            f"got {len(parts)} part(s) separated by '||'"
        )
    resource, api_key, deployment = (p.strip() for p in parts)
    if not (resource and api_key and deployment):
        raise ValueError("AZURE_OPENAI_KEY has an empty RESOURCE, API_KEY or DEPLOYMENT segment")
    return resource, api_key, deployment


def _endpoint(resource: str, deployment: str) -> str:
    api_version = os.environ.get("AZURE_OPENAI_API_VERSION", DEFAULT_API_VERSION)
    return (
        f"https://{resource}.openai.azure.com/openai/deployments/"
        f"{deployment}/chat/completions?api-version={api_version}"
    )


def _build_user_prompt(kind: str, project: str, source_description: str, facts: str) -> str:
    kind_context = {
        "security": "This is a security advisory affecting a widely-run project this publication's readers track.",
        "default-behavior": "This release changes default behavior in a way that can affect systems already running it.",
        "deprecation": "This release announces a deprecation, removal, or end-of-life that requires action before upgrading.",
        "pattern": "Multiple tracked projects independently shipped a related change in the same window — the pattern itself is the story.",
    }.get(kind, "")

    return (
        f"Category of finding: {kind_context}\n\n"
        f"Project: {project}\n"
        f"Source: {source_description}\n\n"
        f"Facts, verbatim from the source (this is everything you know — do not add to it):\n"
        f"---\n{facts}\n---\n"
    )


def write_article(kind: str, project: str, source_description: str, facts: str) -> dict:
    """Returns {"title": str, "body": str}. Raises on malformed model output."""
    resource, api_key, deployment = _parse_azure_key()

    payload = {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(kind, project, source_description, facts)},
        ],
        "temperature": 0.3,
        "max_tokens": 900,
    }

    resp = requests.post(
        _endpoint(resource, deployment),
        headers={"api-key": api_key, "Content-Type": "application/json"},
        json=payload,
        timeout=90,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"].strip()

    if "TITLE:" not in content or "BODY:" not in content:
        raise ValueError(f"Model response missing TITLE/BODY markers: {content[:200]!r}")

    title_part, body_part = content.split("BODY:", 1)
    title = title_part.split("TITLE:", 1)[1].strip()
    body = body_part.strip()

    return {"title": title, "body": body}


REPLY_SYSTEM_PROMPT = """You write discussion replies for AiOps Community. You are replying to \
an entry in a thread on an article this agent published. You are given the facts this \
agent's article was built on, and the entry you're replying to. Add something concrete \
that follows from those facts — do not just agree, do not restate the article, do not \
write generic engagement filler like "great point" or "thanks for sharing". If the \
entry asks a question the given facts don't answer, say plainly that the source \
material doesn't cover it rather than guessing.

Plain text only, no markdown, no links. Under 600 characters. Respond with only the \
reply text, nothing else — no preamble, no quotation marks around it."""


def write_reply(project: str, our_facts: str, their_entry: str) -> str:
    resource, api_key, deployment = _parse_azure_key()

    user_prompt = (
        f"Project: {project}\n\n"
        f"Facts our article was built on:\n---\n{our_facts}\n---\n\n"
        f"The entry we're replying to:\n---\n{their_entry}\n---\n"
    )

    payload = {
        "messages": [
            {"role": "system", "content": REPLY_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 300,
    }

    resp = requests.post(
        _endpoint(resource, deployment),
        headers={"api-key": api_key, "Content-Type": "application/json"},
        json=payload,
        timeout=90,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()
