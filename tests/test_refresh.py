import contextlib
import io
import json
from pathlib import Path
import sqlite3
import threading
from urllib.error import HTTPError
from unittest.mock import patch
import pytest

from wiki_llm.dependencies.wingman_wiki import corpus, refresh


class Publisher:
    def __init__(self):
        self.calls = []
        self.fail = set()
        self.modified = "2026-11-20"
        self.text = "This source paragraph has enough readable words to identify a useful ship reference for the local knowledge index."

    def allowed(self, url):
        return True

    def get(self, url, limit=0):
        self.calls.append(url)
        if url in self.fail:
            raise OSError("synthetic publisher outage")
        if url in corpus.SITEMAPS:
            name = refresh.CATALOGS[corpus.SITEMAPS.index(url)]
            links = ({"community": [corpus.COMMUNITY + "/Alpha", corpus.COMMUNITY + "/Beta"],
                "support": [corpus.SUPPORT + "/hc/en-us/articles/12345-Help"],
                "official": [corpus.RSI + "/comm-link/engineering/12345-Ship"]})[name]
            return '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + ''.join(
                f'<url><loc>{link}</loc><lastmod>{self.modified}</lastmod></url>' for link in links) + '</urlset>'
        return '<meta property="og:title" content="Alpha"><article><p>' + self.text + '</p></article>'

    def galactapedia(self):
        if "galactapedia" in self.fail:
            raise ValueError("synthetic unavailable catalog")
        yield corpus.GALACTAPEDIA + "/article/ABC123def0-ship", "Verified official title"


def test_incremental_catalogs_and_page_batch(tmp_path):
    db = tmp_path / "wiki.sqlite3"
    source = Publisher()
    first = refresh.refresh_once(db, publisher=source, batch_size=1, clock=lambda: 1000)
    assert first.indexed == 1
    assert {x["state"] for x in first.catalogs.values()} == {"refreshed"}
    assert first.coverage["community"] == {"catalog_urls": 2, "indexed_pages": 1,
        "last_catalog_success": 1000.0, "last_catalog_error": None}
    assert first.coverage["official"]["catalog_urls"] == 1
    assert first.coverage["support"]["indexed_pages"] == 0
    assert first.coverage["galactapedia"]["catalog_urls"] == 1
    source.calls.clear()
    second = refresh.refresh_once(db, publisher=source, batch_size=1, clock=lambda: 1001)
    assert second.indexed == 1
    assert source.calls == [corpus.COMMUNITY + "/Beta"]
    source.calls.clear()
    third = refresh.refresh_once(db, publisher=source, clock=lambda: 1002)
    assert third.indexed == 0 and source.calls == []


def test_catalog_failure_independent_and_last_good_kept(tmp_path):
    db = tmp_path / "wiki.sqlite3"
    source = Publisher()
    refresh.refresh_once(db, publisher=source, clock=lambda: 1000)
    source.fail = {corpus.SITEMAPS[0], "galactapedia"}
    report = refresh.refresh_once(db, publisher=source, clock=lambda: 90_000)
    assert report.catalogs["community"]["state"] == "failed"
    assert report.catalogs["galactapedia"]["stale"]
    assert report.catalogs["support"]["state"] == "refreshed"
    assert report.coverage["community"]["indexed_pages"] == 2
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT count(*) FROM page_search WHERE page_search MATCH 'source'").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM wiki_urls WHERE status='removed'").fetchone()[0] == 0
    # Backoff applies to a new caller too; failures do not cause a request storm.
    source.calls.clear()
    again = refresh.refresh_once(db, publisher=source, clock=lambda: 90_001)
    assert again.catalogs["community"] == {"state": "not_due", "stale": True}
    assert source.calls == []


def test_changed_page_failure_preserves_searchable_excerpt_and_recovers(tmp_path):
    db = tmp_path / "wiki.sqlite3"
    source = Publisher()
    refresh.refresh_once(db, publisher=source, clock=lambda: 1000)
    source.modified = "2026-11-21"
    source.fail.add(corpus.COMMUNITY + "/Alpha")
    report = refresh.refresh_once(db, publisher=source, clock=lambda: 90_000)
    assert report.failed == 1 and report.indexed == 1
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT status FROM wiki_urls WHERE url=?", (corpus.COMMUNITY + "/Alpha",)).fetchone()[0] == "indexed"
        assert connection.execute("SELECT last_error FROM refresh_pages WHERE url=?", (corpus.COMMUNITY + "/Alpha",)).fetchone()[0] == "OSError"
        assert connection.execute("SELECT count(*) FROM pages WHERE extract!=''").fetchone()[0] == 2
    source.fail.clear()
    recovered = refresh.refresh_once(db, publisher=source, clock=lambda: 91_000)
    assert recovered.indexed == 1
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT last_error FROM refresh_pages WHERE url=?", (corpus.COMMUNITY + "/Alpha",)).fetchone()[0] is None


def test_shared_database_lease_and_expired_recovery(tmp_path):
    db = tmp_path / "wiki.sqlite3"
    entered, release = threading.Event(), threading.Event()
    class Blocking(Publisher):
        def get(self, url, limit=0):
            if url == corpus.SITEMAPS[0]:
                entered.set()
                assert release.wait(3)
            return super().get(url, limit)
    first = threading.Thread(target=refresh.refresh_once, args=(db,), kwargs={"publisher": Blocking()})
    first.start()
    try:
        assert entered.wait(3)
        second_source = Publisher()
        second = refresh.refresh_once(db, publisher=second_source)
        assert second.leased_elsewhere and second_source.calls == []
        # Queries use the available local database while the refresh is blocked.
        with sqlite3.connect(db, timeout=.1) as connection:
            assert connection.execute("SELECT count(*) FROM wiki_urls").fetchone() == (0,)
    finally:
        release.set()
        first.join(4)
    assert not first.is_alive()
    with sqlite3.connect(db) as connection:
        connection.execute("INSERT INTO refresh_lease VALUES(1,'abandoned',0)")
    assert not refresh.refresh_once(db, publisher=Publisher()).leased_elsewhere


def test_robots_refusal_and_cancellable_pacing():
    stop = threading.Event()
    source = corpus.Publisher(stop)
    calls = []
    def get(url, limit):
        calls.append(url)
        return "User-agent: *\nDisallow: /Alpha\nCrawl-delay: 6\n"
    with patch.object(source, "_get", side_effect=get):
        try:
            source.get(corpus.COMMUNITY + "/Alpha")
        except PermissionError:
            pass
        else:
            raise AssertionError("robots refusal bypassed")
    assert calls == [corpus.COMMUNITY + "/robots.txt"]
    assert corpus.robots_delay("User-agent: *\nCrawl-delay: 6", 5) == 6
    stop.set()
    try:
        source._pace(corpus.COMMUNITY + "/Beta")
    except corpus.RefreshCancelled:
        pass
    else:
        raise AssertionError("cancelled refresh paced/requested again")


def test_scheduler_single_worker_and_bounded_stop(tmp_path):
    entered = threading.Event()
    def block(db, *, stop_event, **options):
        entered.set()
        stop_event.wait(3)
        return refresh.RefreshReport(cancelled=True)
    with patch.object(refresh, "refresh_once", side_effect=block) as call:
        worker = refresh.RefreshScheduler(tmp_path / "wiki.sqlite3").start()
        assert entered.wait(1)
        thread = worker._thread
        assert worker.start()._thread is thread
        assert worker.stop(timeout=1)
        assert call.call_count == 1 and worker.last_report.cancelled


def test_new_publisher_instance_inherits_database_pacing(tmp_path):
    from urllib.parse import urlsplit
    class Tracking(corpus.Publisher):
        def __init__(self):
            super().__init__()
            self.fixture = Publisher()
            self.previous = []
        def allowed(self, url):
            return True
        def get(self, url, limit=0):
            host = urlsplit(url).netloc
            self.previous.append(self.last.get(host))
            self.request_started(host)
            return self.fixture.get(url, limit)
        def galactapedia(self):
            return self.fixture.galactapedia()
    db = tmp_path / "wiki.sqlite3"
    refresh.refresh_once(db, publisher=Tracking(), batch_size=1, clock=lambda: 1000)
    second = Tracking()
    refresh.refresh_once(db, publisher=second, batch_size=1, clock=lambda: 1001)
    assert len(second.previous) == 1 and second.previous[0] is not None


def test_success_status_error_page_cannot_replace_good_excerpt(tmp_path):
    db = tmp_path / "wiki.sqlite3"
    source = Publisher()
    refresh.refresh_once(db, publisher=source, clock=lambda: 1000)
    source.text = ""
    source.modified = "2026-11-21"
    report = refresh.refresh_once(db, publisher=source, clock=lambda: 90000)
    assert report.failed == 2
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT count(*) FROM pages WHERE extract!=''").fetchone()[0] == 2


def test_cli_once(tmp_path):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        result = refresh.main(["--db", str(tmp_path / "wiki.sqlite3"), "--batch-size", "1"], publisher=Publisher())
    assert result == 0
    assert json.loads(output.getvalue())["indexed"] == 1


def test_due_pages_and_new_pages_both_advance_with_one_item_batches(tmp_path):
    db = tmp_path / "wiki.sqlite3"
    source = Publisher()
    refresh.refresh_once(db, publisher=source, clock=lambda: 1000, batch_size=2, page_interval=60)
    with sqlite3.connect(db) as connection:
        connection.executemany("INSERT INTO wiki_urls(url,status) VALUES(?,'pending')",
            [(corpus.COMMUNITY + f"/New{x:02}",) for x in range(10)])
    observed = []
    for now in (1061, 1062, 1063):
        source.calls.clear()
        refresh.refresh_once(db, publisher=source, clock=lambda: now, batch_size=1, page_interval=60)
        observed.extend(source.calls)
    assert observed == [corpus.COMMUNITY + "/Alpha", corpus.COMMUNITY + "/New00", corpus.COMMUNITY + "/Beta"]


def test_due_retry_without_prior_excerpt_is_not_starved_by_new_backlog(tmp_path):
    db = tmp_path / "wiki.sqlite3"
    source = Publisher()
    source.fail.add(corpus.COMMUNITY + "/Alpha")
    assert refresh.refresh_once(db, publisher=source, clock=lambda: 1000, batch_size=1).failed == 1
    with sqlite3.connect(db) as connection:
        connection.executemany("INSERT INTO wiki_urls(url,status) VALUES(?,'pending')",
            [(corpus.COMMUNITY + f"/New{x:02}",) for x in range(10)])
    source.fail.clear()
    source.calls.clear()
    assert refresh.refresh_once(db, publisher=source, clock=lambda: 1901, batch_size=1).indexed == 1
    assert source.calls == [corpus.COMMUNITY + "/Alpha"]


class Response(io.BytesIO):
    def __init__(self, body):
        super().__init__(body.encode())
        self.headers = {}


class Opener:
    def __init__(self, *, rate_header=None, timeout_robots=False):
        self.calls = []
        self.rate_header, self.timeout_robots = rate_header, timeout_robots
        self.fixture = Publisher()

    def open(self, request, timeout):
        url = request.full_url
        self.calls.append(url)
        if self.timeout_robots and url == "https://sitemap.starcitizen.tools/robots.txt":
            raise TimeoutError("PRIVATE-detail-not-for-metadata")
        if url.endswith("/robots.txt"):
            return Response("User-agent: *\nAllow: /\n")
        if url.endswith("/galactapedia/graphql"):
            return Response(json.dumps({"data": {"allArticle": {"totalCount": 1,
                "edges": [{"node": {"id": "ABC123def0", "title": "Ship", "slug": "ship"}}],
                "pageInfo": {"hasNextPage": False}}}}))
        if self.rate_header is not None and url.startswith(corpus.COMMUNITY + "/"):
            raise HTTPError(url, 429, "PRIVATE-detail-not-for-metadata", {"Retry-After": self.rate_header}, io.BytesIO(b"PRIVATE-body"))
        return Response(self.fixture.get(url))


@pytest.mark.parametrize("header", ["120", "Thu, 01 Jan 1970 00:18:41 GMT"])
def test_429_stops_host_batch_and_survives_new_instance(tmp_path, header):
    db = tmp_path / "wiki.sqlite3"
    refresh.refresh_once(db, publisher=Publisher(), clock=lambda: 1000, batch_size=1)
    with sqlite3.connect(db) as connection:
        connection.executemany("INSERT INTO wiki_urls(url,status) VALUES(?,'pending')",
            [(corpus.COMMUNITY + "/Delta",), (corpus.COMMUNITY + "/Gamma",)])
    first = corpus.Publisher()
    first.opener = Opener(rate_header=header)
    with patch.object(corpus, "robots_delay", return_value=0):
        report = refresh.refresh_once(db, publisher=first, clock=lambda: 1001, batch_size=3)
    assert report.failed == 1 and report.deferred == 2
    assert first.opener.calls == [corpus.COMMUNITY + "/robots.txt", corpus.COMMUNITY + "/Beta"]
    with sqlite3.connect(db) as connection:
        stored = connection.execute("SELECT blocked_until,last_phase,last_error,last_http_status FROM refresh_hosts WHERE host='starcitizen.tools'").fetchone()
    assert stored == (1121, "article", "HTTPError", 429)
    assert "PRIVATE" not in json.dumps(stored)
    second = corpus.Publisher()
    second.opener = Opener()
    with patch.object(corpus, "robots_delay", return_value=0):
        blocked = refresh.refresh_once(db, publisher=second, clock=lambda: 1002, batch_size=3)
        assert second.opener.calls == [] and blocked.deferred == 2
        recovered = refresh.refresh_once(db, publisher=second, clock=lambda: 1122, batch_size=3)
    assert recovered.indexed == 3 and recovered.failed == 0
    assert len(second.opener.calls) == 4  # one robots fetch, three article requests


def test_contact_phase_records_first_robots_timeout_without_private_detail(tmp_path):
    db = tmp_path / "wiki.sqlite3"
    source = corpus.Publisher()
    source.opener = Opener(timeout_robots=True)
    with patch.object(corpus, "robots_delay", return_value=0):
        report = refresh.refresh_once(db, publisher=source, clock=lambda: 1000)
    assert report.catalogs["community"]["error_type"] == "TimeoutError"
    assert report.catalogs["official"]["state"] == "refreshed"
    assert corpus.SITEMAPS[0] not in source.opener.calls
    with sqlite3.connect(db) as connection:
        stored = connection.execute("SELECT last_phase,last_error,last_http_status FROM refresh_hosts WHERE host='sitemap.starcitizen.tools'").fetchone()
    assert stored == ("robots", "TimeoutError", None)
    assert "PRIVATE" not in json.dumps(stored)


def test_old_host_metadata_schema_migrates(tmp_path):
    db = tmp_path / "wiki.sqlite3"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE refresh_hosts(host TEXT PRIMARY KEY,last_request REAL NOT NULL)")
        connection.execute("INSERT INTO refresh_hosts VALUES('starcitizen.tools',999)")
    refresh.refresh_once(db, publisher=Publisher(), clock=lambda: 1000)
    with sqlite3.connect(db) as connection:
        stored = connection.execute("SELECT last_request,blocked_until,last_phase FROM refresh_hosts WHERE host='starcitizen.tools'").fetchone()
    assert stored == (999, 0, None)
