"""Short-link resolution (go.vansh.nyc/<key> -> the employer's posting)."""

from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from src.dedup import load_state, new_jobs, reconcile_aliases, save_state, update_state
from src.models import Job
from src.sources.short_links import is_short_link, resolve, resolve_short_links

HOSTS = {"go.vansh.nyc"}


class _Resp:
    def __init__(self, status, location=None):
        self.status_code = status
        self.headers = {"Location": location} if location else {}


class _Session:
    """Answers HEAD requests from a {url: (status, location)} table."""

    def __init__(self, table):
        self.table = table
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def head(self, url, allow_redirects=True, timeout=None):
        assert allow_redirects is False
        self.calls.append(url)
        answer = self.table.get(url)
        if isinstance(answer, Exception):
            raise answer
        return _Resp(*answer) if answer else _Resp(404)


def _job(url, title="Software Engineer"):
    return Job(company="Acme", title=title, url=url, locations=["New York, NY"])


def test_is_short_link_matches_host_only():
    assert is_short_link("https://go.vansh.nyc/BX?s=y", HOSTS)
    assert is_short_link("https://GO.VANSH.NYC./BX", HOSTS)
    assert not is_short_link("https://example.com/?u=https://go.vansh.nyc/BX", HOSTS)
    assert not is_short_link("https://go.vansh.nyc.example.com/BX", HOSTS)


def test_resolve_reads_the_redirect_without_following_it():
    s = _Session({"https://go.vansh.nyc/BX": (302, "https://job-boards.greenhouse.io/affirm/jobs/1")})
    assert resolve(s, "https://go.vansh.nyc/BX", HOSTS) == "https://job-boards.greenhouse.io/affirm/jobs/1"
    assert s.calls == ["https://go.vansh.nyc/BX"]


def test_resolve_follows_a_short_link_to_another_short_link():
    s = _Session({
        "https://go.vansh.nyc/a": (302, "https://go.vansh.nyc/b"),
        "https://go.vansh.nyc/b": (302, "https://jobs.lever.co/acme/1"),
    })
    assert resolve(s, "https://go.vansh.nyc/a", HOSTS) == "https://jobs.lever.co/acme/1"


def test_resolve_gives_up_on_errors_and_dead_links():
    s = _Session({
        "https://go.vansh.nyc/dead": (404, None),
        "https://go.vansh.nyc/down": requests.ConnectionError("boom"),
        "https://go.vansh.nyc/loop": (302, "https://go.vansh.nyc/loop"),
    })
    assert resolve(s, "https://go.vansh.nyc/dead", HOSTS) is None
    assert resolve(s, "https://go.vansh.nyc/down", HOSTS) is None
    assert resolve(s, "https://go.vansh.nyc/loop", HOSTS) is None


def test_jobs_get_the_posting_url_and_unresolved_ones_keep_their_link():
    s = _Session({"https://go.vansh.nyc/BX": (302, "https://jobs.ashbyhq.com/acme/1")})
    jobs = [
        _job("https://go.vansh.nyc/BX"),
        _job("https://go.vansh.nyc/BX", title="Software Engineer II"),
        _job("https://go.vansh.nyc/dead"),
        _job("https://boards.greenhouse.io/acme/jobs/2"),
    ]
    resolve_short_links(jobs, s, hosts=HOSTS)
    assert [j.url for j in jobs] == [
        "https://jobs.ashbyhq.com/acme/1",
        "https://jobs.ashbyhq.com/acme/1",
        "https://go.vansh.nyc/dead",
        "https://boards.greenhouse.io/acme/jobs/2",
    ]
    # One request per distinct short link; employer URLs are never fetched.
    assert sorted(s.calls) == ["https://go.vansh.nyc/BX", "https://go.vansh.nyc/dead"]


def test_resolved_job_dedups_with_the_same_posting_from_another_list():
    s = _Session({"https://go.vansh.nyc/BX": (302, "https://jobs.ashbyhq.com/acme/1")})
    short, direct = _job("https://go.vansh.nyc/BX"), _job("https://jobs.ashbyhq.com/acme/1")
    resolve_short_links([short], s, hosts=HOSTS)
    assert short.dedup_id == direct.dedup_id


def test_job_already_sent_under_its_short_link_is_not_new_again():
    from src.dedup import new_jobs, update_state

    state = update_state({}, [_job("https://go.vansh.nyc/BX")])
    s = _Session({"https://go.vansh.nyc/BX": (302, "https://jobs.ashbyhq.com/acme/1")})
    jobs = resolve_short_links([_job("https://go.vansh.nyc/BX")], s, hosts=HOSTS)
    assert jobs[0].short_url == "https://go.vansh.nyc/BX"
    assert new_jobs(jobs, state) == []


def test_resolved_job_stays_seen_when_a_later_lookup_fails(tmp_path):
    short_url = "https://go.vansh.nyc/BX"
    session = _Session({short_url: (302, "https://jobs.ashbyhq.com/acme/1")})
    resolved = resolve_short_links([_job(short_url)], session, hosts=HOSTS)
    path = tmp_path / "seen.json"
    save_state(path, update_state({}, resolved))

    failed = _Session({short_url: requests.Timeout("lookup timed out")})
    fallback = resolve_short_links([_job(short_url)], failed, hosts=HOSTS)
    assert new_jobs(fallback, load_state(path)) == []


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("legacy", [False, True])
def test_old_short_link_suppresses_all_copies_without_refreshing_date(reverse, legacy):
    short_url = "https://go.vansh.nyc/BX?utm_source=list"
    target = "https://jobs.ashbyhq.com/acme/1"
    old = _job(short_url)
    state = {(old.job_id if legacy else old.dedup_id): {
        "first_seen": "2026-10-25", "company": old.company,
        "title": old.title, "url": old.url,
    }}
    session = _Session({short_url: (302, target)})
    jobs = resolve_short_links([old, _job(target)], session, hosts=HOSTS)
    if reverse:
        jobs.reverse()

    assert new_jobs(jobs, state) == []
    assert len(state) == 1  # Checking for new jobs does not mutate saved state.
    reconcile_aliases(state, jobs)
    assert new_jobs([_job(target)], state) == []
    assert {meta["first_seen"] for meta in state.values()} == {"2026-10-25"}


@pytest.mark.parametrize("already_seen", [False, True])
def test_run_persists_aliases_from_duplicate_sources(monkeypatch, tmp_path, already_seen):
    from src import main

    short_urls = ["https://go.vansh.nyc/BX", "https://go.vansh.nyc/BY"]
    target = "https://jobs.ashbyhq.com/acme/1"
    path = tmp_path / "seen.json"
    if already_seen:
        save_state(path, update_state({}, [_job(short_urls[0])], date.today()))
    monkeypatch.setattr(main.config, "state_path", lambda: path)
    monkeypatch.setattr(main.config, "secrets", lambda: {})
    monkeypatch.setattr(main.config, "settings", lambda: {
        "email": {"enabled": True}, "suppress_when_empty": True,
    })
    monkeypatch.setattr(main, "apply_filters", lambda jobs: jobs)
    # The direct copy wins batch dedup; both short aliases must still be saved.
    monkeypatch.setattr(main, "collect_jobs", lambda **kw: [
        _job(target), *[_job(url) for url in short_urls],
    ])
    monkeypatch.setattr(main, "make_session", lambda: _Session({
        url: (302, target) for url in short_urls
    }))
    send = Mock(return_value=True)
    monkeypatch.setattr(main.email_notify, "send_email", send)
    options = dict(dry_run=False, do_email=True, do_sms=False, do_discord=False, limit=None)
    assert main.run(**options) == 0
    assert send.call_count == (0 if already_seen else 1)
    state = load_state(path)
    assert new_jobs([_job(target), *[_job(url) for url in short_urls]], state) == []

    monkeypatch.setattr(main, "collect_jobs", lambda **kw: [_job(url) for url in short_urls])
    monkeypatch.setattr(main, "make_session", lambda: _Session({
        url: requests.Timeout("lookup timed out") for url in short_urls
    }))
    assert main.run(**options) == 0
    assert send.call_count == (0 if already_seen else 1)


def test_auto_apply_resolves_before_ats_selection_and_application_history(monkeypatch):
    from src.apply import cli

    short_url = "https://go.vansh.nyc/BX"
    target = "https://jobs.ashbyhq.com/acme/1"
    session = _Session({short_url: (302, target)})
    monkeypatch.setattr(cli, "collect_jobs", lambda: [_job(short_url), _job(target)])
    monkeypatch.setattr(cli, "apply_filters", lambda jobs: jobs)
    monkeypatch.setattr(cli, "make_session", lambda: session)
    args = SimpleNamespace(retry_failed=False, category=None, company=None)

    candidates = cli._candidate_jobs(args, {}, {"ashby"})
    assert len(candidates) == 1
    assert candidates[0].url == target
    assert candidates[0].short_url == short_url
    assert session.calls == [short_url]
    applog = {_job(target).job_id: {"status": "submitted"}}
    assert cli._candidate_jobs(args, applog, {"ashby"}) == []
