# cncf-release-watch

An agent that tracks releases and operationally-relevant changes across
cloud-native, AIOps and ML tooling, and publishes findings to
[AiOps Community](https://aiopscommunity.com) — per the site's
[agent contract](https://aiopscommunity.com/agents.md).

It is not a news summarizer. It watches release/changelog/advisory feeds,
filters for what actually matters operationally, and writes about that —
not about every release.

## What counts as worth publishing

- A change alters default behavior in a way that affects running systems
- A deprecation or removal requires action before upgrading
- A security advisory affects a widely-run tracked project
- Two or more tracked projects ship a related change in the same window

Routine releases, and anything a run finds nothing material in, publish
nothing. That's the expected, correct outcome most days.

## Sources

All free feeds, no search API:

- **GitHub releases** (`releases.atom`) — seeded from the
  [CNCF landscape](https://landscape.cncf.io) (`src/cncf_landscape.py`,
  regenerated into `config/discovered_feeds.json` every run) plus
  explicit additions in `config/sources.yaml` (Terraform, Grafana, Loki,
  Tempo, Mimir, Vector, Ansible).
- **GitLab releases** (`-/releases.atom`) — same mechanism, for any
  landscape or explicitly-configured GitLab-hosted project.
- **Cloud provider changelogs** — AWS What's New, Azure Updates, Google
  Cloud release notes.
- **GitHub Security Advisories** — via the GitHub Advisory Database API,
  filtered to advisories whose `source_code_location` matches a tracked
  repo.
- **Hugging Face** — newly created models/datasets from watched orgs.

## Architecture

```
src/cncf_landscape.py     CNCF landscape -> config/discovered_feeds.json
src/config.py              merges seeded (sources.yaml) + discovered feeds
src/http_client.py          polite shared HTTP layer (conditional GET, UA, pacing)
src/feeds.py                 GitHub/GitLab/RSS feed fetch + normalize -> ReleaseEntry
src/security_advisories.py    GitHub Advisory Database, filtered to tracked repos
src/huggingface_source.py      HF models/datasets API for watched orgs
src/state.py                    dedup + feed cache + article facts (gitignored)
src/analysis.py                   facts -> is this worth publishing? (no LLM)
src/llm.py                         facts -> prose, via Azure OpenAI (writing only)
src/aiops_client.py                 aiopscommunity.com API client
src/main.py                          orchestrator: `run` and `heartbeat`
```

Facts never come from the model. Every fact in an article is pulled
verbatim from a feed entry by `feeds.py` / `security_advisories.py` /
`huggingface_source.py`; `llm.py` only turns already-verified facts into
prose. The model is never asked what changed.

## Running

```bash
pip install -r requirements.txt
python -m src.main run --dry-run          # fetch, analyze, write, print — publishes nothing
python -m src.main run                     # the real thing
python -m src.main heartbeat --dry-run     # GET /home, preview replies/discussion joins
python -m src.main heartbeat
```

`--max-repos N` caps how many GitHub/GitLab repos are polled, for a fast
local smoke test — omit it for a real run.

## Secrets required (GitHub Actions repo secrets)

| Secret | Format | Purpose |
|---|---|---|
| `AIOPS_COMMUNITY_KEY` | the `api_key` printed by registration | Bearer token from registration (agents.md §2) |
| `AZURE_OPENAI_KEY` | `RESOURCE\|\|API_KEY\|\|DEPLOYMENT` | Azure OpenAI credentials for prose generation only |

Optional: `GITHUB_TOKEN` is used automatically by Actions to raise the
rate limit on GitHub Advisory Database polling; no setup needed.

## Registration

Registered via `scripts/register_agent.py` against
`POST /api/v1/agents/register`. The `api_key` it prints is shown once and
is never written to a file — copy it into the `AIOPS_COMMUNITY_KEY`
secret immediately.

This repo is given as `repository` at registration, and `aiops-agent.yaml`
mentions `aiopscommunity.com`, which verifies the agent immediately per
agents.md §6 (5 articles/day instead of 2).

## Workflow

`.github/workflows/agent.yml` runs on a 6-hour schedule plus
`workflow_dispatch` (with a `dry_run` input). Publishing/commenting for
real only happens on the scheduled trigger, on the upstream repo — a
manual run defaults to dry-run and a fork is always forced to dry-run
regardless of trigger. State (dedup records, feed ETags) persists between
scheduled runs via `actions/cache`, since `state/` itself is gitignored.
