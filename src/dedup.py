"""Persisted state: which jobs have already been notified.

State file shape (data/seen_jobs.json):
    { "<job_id>": {"first_seen": "2026-06-22", "company": ..., "title": ..., "url": ...}, ... }
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from .models import Job

log = logging.getLogger(__name__)


def load_state(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("could not read state %s: %s — starting fresh", path, exc)
        return {}


def save_state(path: Path, state: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")


def _short_link_ids(job: Job) -> tuple[str, str]:
    """The ids the job had before its short link was resolved."""
    alias = job.model_copy(update={"url": job.short_url})
    return alias.job_id, alias.dedup_id


def reconcile_aliases(state: dict[str, dict], jobs: list[Job]) -> dict[str, dict]:
    """Remember all known identities of seen jobs, preserving their seen dates.

    Group the entire batch first so a direct URL and multiple short links are
    recognized regardless of source order. Unseen groups remain unseen.
    """
    groups: dict[str, set[str]] = {}
    for job in jobs:
        ids = groups.setdefault(job.dedup_id, set())
        ids.update((job.job_id, job.dedup_id))
        if job.short_url:
            ids.update(_short_link_ids(job))
    for ids in groups.values():
        known = [state[jid] for jid in ids if jid in state]
        if not known:
            continue
        meta = min(known, key=lambda item: item.get("first_seen", ""))
        for jid in ids:
            state.setdefault(jid, dict(meta))
    return state


def new_jobs(jobs: list[Job], state: dict[str, dict]) -> list[Job]:
    """Jobs whose id is not already in state (deduped within the batch too)."""
    known = reconcile_aliases(dict(state), jobs)
    seen_now: set[str] = set()
    out: list[Job] = []
    for job in jobs:
        legacy_id = job.job_id
        dedup_id = job.dedup_id
        if (
            dedup_id in known
            or legacy_id in known
            or dedup_id in seen_now
            or legacy_id in seen_now
        ):
            continue
        seen_now.add(dedup_id)
        seen_now.add(legacy_id)
        out.append(job)
    return out


def update_state(
    state: dict[str, dict], jobs: list[Job], today: date | None = None
) -> dict[str, dict]:
    today = today or datetime.now().date()
    iso = today.isoformat()
    for job in jobs:
        state[job.dedup_id] = {
            "first_seen": iso,
            "company": job.company,
            "title": job.title,
            "url": job.url,
        }
    return reconcile_aliases(state, jobs)


def prune(
    state: dict[str, dict], max_age_days: int, today: date | None = None
) -> dict[str, dict]:
    if not max_age_days or max_age_days <= 0:
        return state
    today = today or datetime.now().date()
    cutoff = today - timedelta(days=max_age_days)
    kept: dict[str, dict] = {}
    for jid, meta in state.items():
        fs = meta.get("first_seen")
        try:
            seen_date = date.fromisoformat(fs) if fs else today
        except ValueError:
            seen_date = today
        if seen_date >= cutoff:
            kept[jid] = meta
    removed = len(state) - len(kept)
    if removed:
        log.info("pruned %d stale state entries (> %d days)", removed, max_age_days)
    return kept
