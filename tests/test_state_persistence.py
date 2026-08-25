"""Regression test for the state-clobber bug in main.run().

aiops_client.publish() persists article metadata (state["articles"][slug])
mid-loop via its own load_state()/save_state() cycle. run() used to hold a
single `state` object loaded before that loop and save it again at the end,
silently overwriting whatever publish() had just written. This test drives
the real main.run() end-to-end (network and LLM calls mocked, state-file
I/O real) and asserts the published article's metadata is still on disk
after run() returns.
"""
from __future__ import annotations

import os
import pathlib
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from src import aiops_client, main
from src import state as state_module
from src.analysis import Candidate
from src.feeds import ReleaseEntry

FINDING_ID = "default-behavior:tag:github.com,2008:Repository/883829350/dev-latest"
SLUG = "hyperlight-prerelease-changes-snapshot-compatibility-and-msr-handling"


def _make_candidate() -> Candidate:
    entry = ReleaseEntry(
        source_type="github-release",
        project="hyperlight",
        entry_id="tag:github.com,2008:Repository/883829350/dev-latest",
        title="dev-latest",
        link="https://github.com/hyperlight-dev/hyperlight/releases/tag/dev-latest",
        published="2026-08-24T10:00:00Z",
        body="Snapshot format and MSR handling changed.",
    )
    return Candidate(kind="default-behavior", primary=entry, reasons=["breaking change"])


class RunPersistsPublishedArticleMetadataTest(unittest.TestCase):
    """The one assertion the state-clobber fix rests on, exercised for real."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._state_path_patch = patch.object(
            state_module, "STATE_PATH", pathlib.Path(self._tmpdir.name) / "agent_state.json"
        )
        self._state_path_patch.start()
        self._env_patch = patch.dict(os.environ, {"AIOPS_COMMUNITY_KEY": "dummy_test_key"})
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        self._state_path_patch.stop()
        self._tmpdir.cleanup()

    def test_article_metadata_survives_runs_final_save(self):
        fake_publish_response = MagicMock(status_code=201)
        fake_publish_response.json.return_value = {
            "id": 664, "status": "published", "url": f"/{SLUG}/",
        }

        with patch("src.main.refresh_discovered_feeds", side_effect=Exception("network disabled in test")), \
             patch("src.main._fetch_all_entries", return_value=([], 0)), \
             patch("src.main.find_candidates", return_value=[_make_candidate()]), \
             patch("src.aiops_client.get_categories", return_value=["Tools & Platforms"]), \
             patch("src.aiops_client.quota_remaining", return_value=3), \
             patch("src.aiops_client.requests.post", return_value=fake_publish_response), \
             patch("src.main.llm.write_article", return_value={
                 "title": "Hyperlight prerelease changes snapshot compatibility",
                 "body": "x" * 250,
             }):
            main.run(dry_run=False)

        on_disk = state_module.load_state()
        self.assertIn(
            SLUG, on_disk["articles"],
            "article metadata written by publish() was lost by the end of run() "
            "— the final save clobbered it with a stale pre-loop state object",
        )
        self.assertEqual(on_disk["articles"][SLUG]["project"], "hyperlight")
        self.assertIn(FINDING_ID, on_disk["published_findings"])


if __name__ == "__main__":
    unittest.main()
