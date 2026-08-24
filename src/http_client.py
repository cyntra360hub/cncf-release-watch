"""A polite, shared HTTP layer.

Feed etiquette from the brief: conditional requests (If-None-Match /
If-Modified-Since), a real User-Agent identifying the agent, and no
bursting hundreds of requests at once. This module is the one place all
of that lives, so every source module gets it for free.
"""

from __future__ import annotations

import random
import threading
import time

import requests

USER_AGENT = (
    "cncf-release-watch/0.1 (+https://aiopscommunity.com; "
    "AiOps Community agent tracking cloud-native/AIOps/MLOps releases)"
)


class PoliteSession:
    """A requests.Session with conditional-GET support and light pacing.

    Safe to share across a small thread pool: each request jitters its own
    pacing delay rather than relying on a single shared clock, which is
    good enough to avoid bursting a host without serializing every call.
    """

    def __init__(self, min_delay: float = 0.2, jitter: float = 0.2, timeout: int = 20):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.min_delay = min_delay
        self.jitter = jitter
        self.timeout = timeout
        self._lock = threading.Lock()

    def get_conditional(
        self, url: str, etag: str | None = None, last_modified: str | None = None
    ) -> requests.Response:
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        time.sleep(self.min_delay + random.uniform(0, self.jitter))
        return self.session.get(url, headers=headers, timeout=self.timeout)

    def get(self, url: str, **kwargs) -> requests.Response:
        time.sleep(self.min_delay + random.uniform(0, self.jitter))
        kwargs.setdefault("timeout", self.timeout)
        return self.session.get(url, **kwargs)
