"""Short-link resolution (go.vansh.nyc/<key> -> the employer's posting)."""

import requests

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
