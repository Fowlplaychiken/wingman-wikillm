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
    assert first.catalogs == {
        "community": {"state": "refreshed", "discovered": 2, "stale": False},
        "support": {"state": "disabled"},
        "official": {"state": "disabled"},
        "galactapedia": {"state": "disabled"},
    }
    assert source.calls == [corpus.SITEMAPS[0], corpus.COMMUNITY + "/Alpha"]
    assert first.coverage["community"] == {"catalog_urls": 2, "indexed_pages": 1,
        "last_catalog_success": 1000.0, "last_catalog_error": None}
    assert first.coverage["official"]["catalog_urls"] == 0
    assert first.coverage["support"]["indexed_pages"] == 0
    assert first.coverage["galactapedia"]["catalog_urls"] == 0
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
    refresh.refresh_once(db, publisher=source, clock=lambda: 1000, enabled_catalogs=refresh.CATALOGS)
    source.fail = {corpus.SITEMAPS[0], "galactapedia"}
    report = refresh.refresh_once(db, publisher=source, clock=lambda: 90_000, enabled_catalogs=refresh.CATALOGS)
    assert report.catalogs["community"]["state"] == "failed"
    assert report.catalogs["galactapedia"]["stale"]
    assert report.catalogs["support"]["state"] == "refreshed"
    assert report.coverage["community"]["indexed_pages"] == 2
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT count(*) FROM page_search WHERE page_search MATCH 'source'").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM wiki_urls WHERE status='removed'").fetchone()[0] == 0
    # Backoff applies to a new caller too; failures do not cause a request storm.
    source.calls.clear()
    again = refresh.refresh_once(db, publisher=source, clock=lambda: 90_001, enabled_catalogs=refresh.CATALOGS)
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


def test_body_vocabulary_is_discovery_only_and_replaced_on_refresh(tmp_path):
    from wiki_llm.dependencies.wingman_wiki.library import lookup
    from wiki_llm.dependencies.wingman_wiki.reference import WikiReference
    db = tmp_path / "wiki.sqlite3"
    source = Publisher()
    original_get = source.get
    body = ["Calibrated xenon baffle supports the aft radiator."]

    def page(url, limit=0):
        if url in corpus.SITEMAPS:
            return original_get(url, limit)
        return ('<meta property="og:title" content="Alpha">'
                '<article><p>' + source.text + '</p><p>' + body[0] +
                '</p><aside>hidden obelisk term</aside><script>privatequasar term</script></article>')

    source.get = page
    refresh.refresh_once(db, publisher=source, clock=lambda: 1000, page_interval=60)
    result = lookup(Path(__file__).resolve().parents[1] / "wiki_llm" / "guides",
                    "xenon baffle", db_path=db)
    assert result["references"] == []
    assert result["discovery_links"][0]["title"] == "Alpha"
    assert result["discovery_links"][0]["citation"] == corpus.COMMUNITY + "/Alpha"
    assert result["discovery_links"][0]["matched_on"] == "body_vocabulary"
    assert result["discovery_links"][0]["insufficient_evidence"] is True
    assert "excerpt" not in result["discovery_links"][0]
    with sqlite3.connect(db) as connection:
        terms, = connection.execute("SELECT terms FROM page_vocabulary").fetchone()
        assert "xenon" in terms and "baffle" in terms
        assert "hidden" not in terms and "privatequasar" not in terms
        assert len(terms.split()) == len(set(terms.split()))
        assert "Calibrated" not in terms

    body[0] = "Encrypted iridium valve regulates coolant flow."
    refresh.refresh_once(db, publisher=source, clock=lambda: 1061, page_interval=60)
    assert WikiReference.search_catalog("xenon baffle", db) == []
    changed = WikiReference.search_catalog("iridium valve", db)
    assert changed and changed[0]["matched_on"] == "body_vocabulary"
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT reason FROM source_changes WHERE url=?",
                                  (corpus.COMMUNITY + "/Alpha",)).fetchone() == ("page_vocabulary_changed",)


def test_structured_page_without_excerpt_is_retained_for_discovery(tmp_path):
    from wiki_llm.dependencies.wingman_wiki.library import library_status, lookup, read_page
    db = tmp_path / "wiki.sqlite3"
    source = Publisher()
    original_get = source.get

    def page(url, limit=0):
        if url in corpus.SITEMAPS:
            return original_get(url, limit)
        return ('<meta property="og:title" content="Alpha"><div id="mw-content-text">'
                '<ul><li>Distinct tenders</li></ul><table><tr><td><p>'
                'Recovered xenon cargo returns aboard returning xenon tenders.'
                '</p></td></tr></table><span hidden>hiddenword</span><nav>navword</nav></div>')

    source.get = page
    report = refresh.refresh_once(db, publisher=source, batch_size=1, clock=lambda: 1000)
    assert report.indexed == 1 and report.failed == 0
    result = lookup(tmp_path, "xenon tenders", db_path=db)
    link, = result["discovery_links"]
    assert result["status"] == "no_matching_evidence" and result["references"] == []
    assert link["citation"] == corpus.COMMUNITY + "/Alpha" and "excerpt" not in link
    assert link["matched_on"] == "body_vocabulary" and link["insufficient_evidence"] is True
    assert read_page(db, link["citation"])["status"] == "unavailable"
    totals = library_status(db)["coverage_totals"]["star_citizen_preset"]
    assert totals["unique_known_title_urls"] == 2 and totals["saved_community_excerpt_urls"] == 0
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT extract FROM pages").fetchone() == ("",)
        terms, = connection.execute("SELECT terms FROM page_vocabulary").fetchone()
        assert terms == "aboard cargo distinct recovered returning returns tenders xenon"


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


def test_offline_refresh_picks_up_new_and_replaced_exports_without_contacting_publishers(tmp_path):
    from wiki_llm.dependencies.wingman_wiki.library import read_page
    path = tmp_path / "wiki.sqlite3"
    root = "https://example.org/game/"
    url = root + "Engine"
    options = {"enabled_catalogs": (), "roots": (), "import_snapshots": True}
    with patch("socket.socket", side_effect=AssertionError("network access")):
        assert not refresh.refresh_once(path, **options).failed
        assert not path.exists()
        source = tmp_path / "snapshots" / "game.jsonl"
        source.parent.mkdir()
        for revision, text in enumerate(("First description", "Revised description"), 1):
            source.write_text(json.dumps(dict(url=url, title="Engine", revision=revision, text=text)),
                              encoding="utf-8")
            report = refresh.refresh_once(path, **options)
            assert not report.failed and report.sources["imports"]["sources"]
            result = read_page(path, url, include_star_citizen=False, roots=(root,))
            assert result["text"] == text


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
    source = Publisher()
    with contextlib.redirect_stdout(output):
        result = refresh.main(["--db", str(tmp_path / "wiki.sqlite3"), "--batch-size", "1"], publisher=source)
    assert result == 0
    assert json.loads(output.getvalue())["indexed"] == 1
    assert source.calls == [corpus.SITEMAPS[0], corpus.COMMUNITY + "/Alpha"]


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
        report = refresh.refresh_once(db, publisher=source, clock=lambda: 1000, enabled_catalogs=refresh.CATALOGS)
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


class ChangingCatalogPublisher:
    """Independent two-publisher snapshots without any lastmod metadata."""
    def __init__(self):
        self.changed = False
        self.calls = []
        self.partial = False
        self.community = [corpus.COMMUNITY + "/Orion", corpus.COMMUNITY + "/Retired"]
        self.lore = [(corpus.GALACTAPEDIA + "/article/ABC123def0-orion", "Orion catalog before"),
                     (corpus.GALACTAPEDIA + "/article/XYZ123abc0-old-slug", "Old slug catalog"),
                     (corpus.GALACTAPEDIA + "/article/REM123abc0-retired", "Retired lore catalog")]

    def allowed(self, url):
        return True

    def get(self, url, limit=0):
        self.calls.append(url)
        if url in corpus.SITEMAPS:
            links = {corpus.SITEMAPS[0]: self.community,
                     corpus.SITEMAPS[1]: [corpus.SUPPORT + "/hc/en-us/articles/12345-Help"],
                     corpus.SITEMAPS[2]: [corpus.RSI + "/comm-link/engineering/12345-Ship"]}[url]
            return '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + ''.join(
                '<url><loc>' + link + '</loc></url>' for link in links) + '</urlset>'
        assert url.startswith(corpus.COMMUNITY + "/"), "Official article bodies must not be requested"
        revision = 202 if self.changed else 101
        return ('<meta property="og:title" content="Orion unchanged title">'
                '<script>var config={"wgRevisionId":' + str(revision) + '};</script>'
                '<article><p>A stable short public excerpt is enough for the reference cache '
                'while a later edit elsewhere in this page changes its revision identity.</p></article>')

    def galactapedia(self):
        for i, row in enumerate(self.lore):
            yield row
            if self.partial and i == 0:
                raise ValueError("incomplete fixture catalog")

    def advance(self):
        self.changed = True
        self.community = [corpus.COMMUNITY + "/Orion", corpus.COMMUNITY + "/New"]
        self.lore = [(corpus.GALACTAPEDIA + "/article/ABC123def0-orion", "Orion catalog renamed"),
                     (corpus.GALACTAPEDIA + "/article/XYZ123abc0-new-slug", "New slug catalog")]


def test_no_lastmod_catalog_changes_and_same_title_revision_preserve_all_notes(tmp_path):
    from wiki_llm.dependencies.wingman_wiki.library import note_history, record_note
    from wiki_llm.dependencies.wingman_wiki.reference import WikiReference

    db = tmp_path / "wiki.sqlite3"
    source = ChangingCatalogPublisher()
    options = dict(db_path=db, source=source.lore[0][0])
    record_note("orion-review", "Personal Orion", "Private reviewed observation", status="reviewed", **options)
    record_note("orion-review", "Personal Orion", "Private pending correction", status="draft", previous_revision=1, **options)
    record_note("orion-draft", "Personal draft", "Private draft only", status="draft", **options)
    record_note("orion-withdrawn", "Personal withdrawn", "Private former observation", status="reviewed", **options)
    record_note("orion-withdrawn", "Personal withdrawn", "Private withdrawal", status="withdrawn", previous_revision=1, **options)
    note_dir = tmp_path / "notes"
    note_dir.mkdir()
    note_file = note_dir / "personal.md"
    note_file.write_text("Private Markdown note must survive unchanged", encoding="utf-8")

    def note_snapshot():
        with sqlite3.connect(db) as conn:
            return (conn.execute("SELECT * FROM note_revisions ORDER BY slug,revision").fetchall(),
                    conn.execute("SELECT rowid,* FROM current_notes ORDER BY rowid").fetchall(),
                    note_file.read_bytes())

    before = note_snapshot()
    first = refresh.refresh_once(db, publisher=source, clock=lambda: 1000, catalog_interval=60,
                                 page_interval=120, enabled_catalogs=refresh.CATALOGS)
    assert first.indexed == 2
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM source_changes").fetchone() == (0,)
    source.advance()
    source.calls.clear()
    refresh.refresh_once(db, publisher=source, clock=lambda: 1061, catalog_interval=60,
                         page_interval=120, enabled_catalogs=refresh.CATALOGS)
    assert corpus.COMMUNITY + "/Orion" not in source.calls  # bounded periodic revalidation, not every catalog poll
    assert WikiReference.search_catalog("renamed", db)[0]["title"] == "Orion catalog renamed"
    assert WikiReference.search_catalog("new slug", db)[0]["citation"].endswith("new-slug")
    assert not WikiReference.search_catalog("retired", db)
    refresh.refresh_once(db, publisher=source, clock=lambda: 1121, catalog_interval=60,
                         page_interval=120, enabled_catalogs=refresh.CATALOGS)
    with sqlite3.connect(db) as conn:
        changes = dict(conn.execute("SELECT url,reason FROM source_changes"))
        assert changes[corpus.GALACTAPEDIA + "/article/ABC123def0-orion"] == "catalog_title_changed"
        assert changes[corpus.GALACTAPEDIA + "/article/XYZ123abc0-old-slug"] == "catalog_url_removed"
        assert changes[corpus.GALACTAPEDIA + "/article/REM123abc0-retired"] == "catalog_url_removed"
        assert changes[corpus.COMMUNITY + "/Retired"] == "catalog_url_removed"
        assert changes[corpus.COMMUNITY + "/Orion"] == "page_revision_changed"
        assert conn.execute("SELECT count(*) FROM wiki_urls WHERE lastmod IS NOT NULL").fetchone() == (0,)
        observation = conn.execute("SELECT * FROM source_changes ORDER BY url").fetchall()
        assert all(row[1].endswith("+00:00") for row in observation)
        assert all("/galactapedia/article/" not in url and "/comm-link/" not in url
                   for url in source.calls)
    refresh.refresh_once(db, publisher=source, clock=lambda: 1242, catalog_interval=60,
                         page_interval=120, enabled_catalogs=refresh.CATALOGS)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT * FROM source_changes ORDER BY url").fetchall() == observation
    assert note_snapshot() == before
    assert len(note_history("orion-review", db)) == 2
    assert [row["status"] for row in note_history("orion-withdrawn", db)] == ["withdrawn", "reviewed"]
    assert len(before[0]) == 5 and len(before[1]) == 1


def test_partial_catalog_does_not_remove_or_rename_last_good_snapshot(tmp_path):
    db = tmp_path / "wiki.sqlite3"
    source = ChangingCatalogPublisher()
    refresh.refresh_once(db, publisher=source, clock=lambda: 1000, enabled_catalogs=refresh.CATALOGS)
    with sqlite3.connect(db) as conn:
        before = conn.execute("SELECT url,title FROM catalog_search WHERE publisher!='Star Citizen Wiki' ORDER BY url").fetchall()
    source.advance()
    source.partial = True
    failed = refresh.refresh_once(db, publisher=source, clock=lambda: 90_000, enabled_catalogs=refresh.CATALOGS)
    assert failed.catalogs["galactapedia"]["state"] == "failed"
    assert failed.catalogs["community"]["state"] == "refreshed"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT url,title FROM catalog_search WHERE publisher!='Star Citizen Wiki' ORDER BY url").fetchall() == before
        assert conn.execute("SELECT count(*) FROM source_changes WHERE url LIKE ?", (corpus.GALACTAPEDIA + "%",)).fetchone() == (0,)


def test_blocked_or_malformed_sitemap_row_cannot_publish_partial_removal(tmp_path):
    db = tmp_path / "wiki.sqlite3"
    source = ChangingCatalogPublisher()
    refresh.refresh_once(db, publisher=source, clock=lambda: 1000)
    source.allowed = lambda url: not url.endswith("/Retired")
    failed = refresh.refresh_once(db, publisher=source, clock=lambda: 90_000)
    assert failed.catalogs["community"]["state"] == "failed"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT status FROM wiki_urls WHERE url=?", (corpus.COMMUNITY + "/Retired",)).fetchone() == ("indexed",)
    source.allowed = lambda url: True
    source.get = lambda url, limit=0: '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url/></urlset>'
    failed = refresh.refresh_once(db, publisher=source, clock=lambda: 91_000)
    assert failed.catalogs["community"]["state"] == "failed"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM wiki_urls WHERE status='removed'").fetchone() == (0,)


def test_standalone_discover_tracks_removals_without_lastmod(tmp_path):
    source = ChangingCatalogPublisher()
    with sqlite3.connect(tmp_path / "wiki.sqlite3") as conn:
        conn.executescript(corpus.SCHEMA)
        corpus.discover(conn, source)
        source.advance()
        corpus.discover(conn, source)
        assert conn.execute("SELECT status FROM wiki_urls WHERE url=?", (corpus.COMMUNITY + "/Retired",)).fetchone() == ("removed",)
        prior = conn.execute("SELECT * FROM source_changes ORDER BY url").fetchall()
        assert prior
        corpus.discover(conn, source)
        assert conn.execute("SELECT * FROM source_changes ORDER BY url").fetchall() == prior


def test_enabled_catalogs_disable_network_but_preserve_sources_and_notes(tmp_path):
    from wiki_llm.dependencies.wingman_wiki.library import record_note
    db = tmp_path / "wiki.sqlite3"
    class Selected(ChangingCatalogPublisher):
        def galactapedia(self):
            self.calls.append("galactapedia")
            yield from super().galactapedia()
    source = Selected()
    refresh.refresh_once(db, publisher=source, clock=lambda: 1000, page_interval=60)
    record_note("local-note", "Local note", "Personal persistent observation", status="reviewed", db_path=db)
    with sqlite3.connect(db) as conn:
        def snapshot():
            return [conn.execute("SELECT * FROM " + table + " ORDER BY rowid").fetchall()
                    for table in ("wiki_urls", "pages", "page_sources", "catalog_titles", "note_revisions", "current_notes")]
        before = snapshot()
        source.calls.clear()
        disabled = refresh.refresh_once(db, publisher=source, clock=lambda: 90_000, enabled_catalogs=[])
        assert source.calls == [] and disabled.indexed == 0
        assert all(value == {"state": "disabled"} for value in disabled.catalogs.values())
        assert snapshot() == before
    only_lore = refresh.refresh_once(db, publisher=source, clock=lambda: 90_001,
                                   enabled_catalogs=(name for name in ["galactapedia"]))
    assert source.calls == ["galactapedia"] and only_lore.indexed == 0
    assert only_lore.coverage["community"]["indexed_pages"] == 2
    with sqlite3.connect(db) as conn:
        cached_lore = conn.execute("SELECT url,title FROM catalog_titles WHERE url LIKE ?",
                                   (corpus.GALACTAPEDIA + "%",)).fetchall()
    source.calls.clear()
    automatic = refresh.refresh_once(db, publisher=source, clock=lambda: 90_002)
    assert automatic.catalogs["galactapedia"] == {"state": "disabled"}
    assert source.calls[0] == corpus.SITEMAPS[0]
    assert all("robertsspaceindustries.com" not in url and "support.robertsspaceindustries.com" not in url
               for url in source.calls)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT url,title FROM catalog_titles WHERE url LIKE ?",
                            (corpus.GALACTAPEDIA + "%",)).fetchall() == cached_lore
    source.calls.clear()
    only_community = refresh.refresh_once(db, publisher=source, clock=lambda: 180_003,
                                        page_interval=60, enabled_catalogs=["community"])
    assert only_community.catalogs["community"]["state"] in {"refreshed", "not_due"}
    assert all(url == corpus.SITEMAPS[0] or url.startswith(corpus.COMMUNITY + "/")
               for url in source.calls)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM note_revisions").fetchone() == (1,)


@pytest.mark.parametrize("selected", ["community", ["unknown"], [None], 1])
def test_invalid_catalog_selection_refuses_before_database_or_network(tmp_path, selected):
    path = tmp_path / "not-created.sqlite3"
    source = Publisher()
    with pytest.raises(ValueError):
        refresh.refresh_once(path, publisher=source, enabled_catalogs=selected)
    assert not path.exists() and source.calls == []


def test_missing_child_sitemap_keeps_previous_catalog(tmp_path):
    db = tmp_path / "wiki.sqlite3"
    source = ChangingCatalogPublisher()
    refresh.refresh_once(db, publisher=source, clock=lambda: 1000)
    normal_get = source.get
    def incomplete(url, limit=0):
        if url == corpus.SITEMAPS[0]:
            return ('<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    '<sitemap><loc>https://sitemap.starcitizen.tools/valid.xml</loc></sitemap>'
                    '<sitemap/></sitemapindex>')
        if url.endswith("valid.xml"):
            return ('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    '<url><loc>https://starcitizen.tools/Orion</loc></url></urlset>')
        return normal_get(url, limit)
    source.get = incomplete
    report = refresh.refresh_once(db, publisher=source, clock=lambda: 90_000)
    assert report.catalogs["community"]["state"] == "failed"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM wiki_urls WHERE status='removed'").fetchone() == (0,)


def gala_page(total, ids, cursor=None):
    return json.dumps({"data": {"allArticle": {"totalCount": total,
        "edges": [{"node": {"id": f"{n:010d}", "title": f"Ship {n}", "slug": f"ship-{n}"}}
                  for n in ids],
        "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor}}}})


@pytest.mark.parametrize("remaining", [False, True])
def test_galactapedia_accepts_stable_or_remaining_count(remaining):
    source = corpus.Publisher()
    source.allowed = lambda url: True
    total = 1479
    pages = [gala_page(total - i if remaining else total, range(i, min(i + 100, total)),
                       f"after-{i + 100}" if i + 100 < total else None)
             for i in range(0, total, 100)]
    with patch.object(source, "_get", side_effect=pages) as get:
        result = refresh._catalog(source, "galactapedia", None)
    assert list(result.values()) == [f"Ship {n}" for n in range(total)]
    assert [json.loads(call.args[2])["variables"]["after"] for call in get.call_args_list] == [
        None, *(f"after-{n}" for n in range(100, total, 100))]


@pytest.mark.parametrize(("pages", "error"), [
    ([gala_page(3, [0], "a"), gala_page(1, [1, 2])], "changed during pagination"),
    ([gala_page(4, [0], "a"), gala_page(4, [1], "b"), gala_page(2, [2, 3])], "changed during pagination"),
    ([gala_page(4, [0], "a"), gala_page(3, [1], "b"), gala_page(4, [2, 3])], "changed during pagination"),
    ([gala_page(2, [0], "a"), gala_page(1, [0, 1])], "duplicate article identity"),
    ([gala_page(2, [0, 0, 1])], "duplicate article identity"),
    ([gala_page(5, [0], "a"), gala_page(4, [1], "b"), gala_page(3, [2], "a")], "cursor failed"),
    ([gala_page(2, [], "a")], "cursor failed"),
    ([gala_page(5, [0], "a"), gala_page(4, [1, 2])], "incomplete Galactapedia catalog"),
    ([gala_page(1, [0], "a")], "cursor failed"),
    ([gala_page(1, [0, 1])], "incomplete Galactapedia catalog"),
    ([json.dumps({"data": {"allArticle": {"totalCount": 1, "edges": [], "pageInfo": {}}}})], "invalid pagination"),
])
def test_galactapedia_rejects_inconsistent_or_partial_catalog(pages, error):
    source = corpus.Publisher()
    source.allowed = lambda url: True
    with patch.object(source, "_get", side_effect=pages):
        with pytest.raises(ValueError, match=error):
            refresh._catalog(source, "galactapedia", None)


def test_removed_page_cannot_resurrect_another_pages_reused_id(tmp_path):
    db = tmp_path / "wiki.sqlite3"
    source = ChangingCatalogPublisher()
    target = corpus.COMMUNITY + "/Retired"
    refresh.refresh_once(db, publisher=source, clock=lambda: 1000, page_interval=60)
    normal_get = source.get
    def missing(url, limit=0):
        if url == target:
            raise HTTPError(url, 410, "Gone", {}, io.BytesIO())
        return normal_get(url, limit)
    source.get = missing
    refresh.refresh_once(db, publisher=source, clock=lambda: 1061, page_interval=60)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT status,pageid FROM wiki_urls WHERE url=?", (target,)).fetchone() == ("removed", None)
        assert conn.execute("SELECT reason FROM source_changes WHERE url=?", (target,)).fetchone() == ("page_removed",)
        assert conn.execute("SELECT count(*) FROM page_vocabulary").fetchone() == (1,)
        # A newly indexed page may reuse the deleted highest SQLite row ID.
        corpus.crawl(conn, source, 1, [corpus.COMMUNITY + "/New"])
    source.get = normal_get
    refresh.refresh_once(db, publisher=source, clock=lambda: 90_000, page_interval=60)
    with sqlite3.connect(db) as conn:
        pageid = conn.execute("SELECT pageid FROM wiki_urls WHERE url=?", (target,)).fetchone()[0]
        assert conn.execute("SELECT url FROM page_sources WHERE pageid=?", (pageid,)).fetchone() == (target,)
