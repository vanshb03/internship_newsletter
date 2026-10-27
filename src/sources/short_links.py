"""Resolve link-shortener URLs (e.g. go.vansh.nyc/BX) to the posting they lead to.

Some community lists publish short links instead of the employer's URL. Each
one is a plain 302 whose Location header is the application page, so a single
HEAD request (redirects not followed) is enough to read it.

Resolving matters for two reasons:
  * dedup keys on the URL, so the same posting from two lists only collapses
    once both carry the employer URL;
  * auto-apply picks its applicator by ATS host (greenhouse/lever/ashby).

A link that can't be resolved keeps its short URL: it still opens the posting
for whoever clicks it, so the job is never dropped over a failed lookup.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, Optional
from urllib.parse import urljoin, urlsplit

import requests

from .. import config
from ..models import Job

log = logging.getLogger(__name__)

DEFAULT_HOSTS = ("go.vansh.nyc",)
_MAX_HOPS = 3
_REDIRECT_CODES = {301, 302, 303, 307, 308}


def _host(url: str) -> str:
    try:
        return (urlsplit(url or "").hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def short_link_hosts() -> set[str]:
    cfg = config.github_lists()
    hosts = cfg.get("short_link_hosts", DEFAULT_HOSTS) or []
    return {h.lower().rstrip(".") for h in hosts}


def is_short_link(url: str, hosts: Iterable[str]) -> bool:
    return _host(url) in set(hosts)


def resolve(session: requests.Session, url: str, hosts: set[str], timeout: float = 10) -> Optional[str]:
    """Return the URL a short link redirects to, or None if it can't be read."""
    current = url
    for _ in range(_MAX_HOPS):
        try:
            resp = session.head(current, allow_redirects=False, timeout=timeout)
        except requests.RequestException as exc:
            log.warning("short link %s: %s", url, exc)
            return None
        location = resp.headers.get("Location")
        if resp.status_code not in _REDIRECT_CODES or not location:
            log.warning("short link %s: HTTP %s, no redirect", url, resp.status_code)
            return None
        current = urljoin(current, location)
        if not is_short_link(current, hosts):
            return current
    log.warning("short link %s: still a short link after %d hops", url, _MAX_HOPS)
    return None


def resolve_short_links(
    jobs: list[Job],
    session: requests.Session,
    hosts: Optional[set[str]] = None,
    max_workers: int = 8,
) -> list[Job]:
    """Swap each job's short-link URL for the posting URL, in place."""
    hosts = short_link_hosts() if hosts is None else hosts
    short = sorted({j.url for j in jobs if is_short_link(j.url, hosts)})
    if not short:
        return jobs

    timeout = (config.settings().get("http", {}) or {}).get("timeout_seconds", 10)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        targets = dict(zip(short, pool.map(lambda u: resolve(session, u, hosts, timeout), short)))

    for job in jobs:
        target = targets.get(job.url)
        if target:
            job.short_url, job.url = job.url, target
    resolved = sum(1 for t in targets.values() if t)
    log.info("resolved %d/%d short links", resolved, len(short))
    return jobs
