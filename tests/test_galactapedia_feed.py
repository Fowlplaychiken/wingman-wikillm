import email.message
import json
import math
import sqlite3
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from io import BytesIO
from contextlib import closing

import pytest

from wiki_llm.dependencies.wingman_wiki import galactapedia_feed as feed
from wiki_llm.dependencies.wingman_wiki.library import note_history, record_note
from wiki_llm.dependencies.wingman_wiki.snapshots import import_snapshot, read_snapshot_page


class Clock:
    def __init__(self, value=1_800_000_000):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class Response:
    def __init__(self, url, status, headers=None, body=b""):
        self.url = url
        self.status = status
        self.headers = headers or {}
        self.body = body
        self.bytes_read = 0

    def geturl(self):
        return self.url

    def read(self, limit=-1):
        value = self.body if limit < 0 else self.body[:limit]
        self.bytes_read += len(value)
        return value

    def close(self):
        pass


def article(ident, slug, title, text):
    return {
        "id": ident,
        "slug": slug,
        "title": title,
        "rsi_url": f"/galactapedia/article/{ident}-{slug}",
        "translations": {} if text is None else {"en_EN": text},
    }


class FakeAPI:
    def __init__(self, rows, *, page_size=2):
        self.rows = list(rows)
        self.page_size = page_size
        self.modified = {}
        self.requests = []
        self.page_bodies = 0
        self.page_headers = {}
        self.catalog_responses = []
        self.robots_content_signal = None
        self.failures = {}
        self.disallow_pages = False
        self.set_revision(0)

    def set_revision(self, revision, page=None):
        value = f"Wed, 0{revision + 1} Jan 2026 00:00:00 GMT"
        if page is None:
            last = math.ceil(len(self.rows) / self.page_size) or 1
            self.modified.update({number: value for number in range(1, last + 1)})
        else:
            self.modified[page] = value

    def replace(self, rows, *, revision, page=None):
        self.rows = list(rows)
        self.set_revision(revision, page)

    def open(self, request, timeout):
        url = request.full_url
        parsed = urlsplit(url)
        self.requests.append({"url": url, "timeout": timeout,
                              "if_modified_since": request.get_header("If-modified-since")})
        if parsed.path == "/robots.txt":
            rules = "User-agent: *\nDisallow: /api/galactapedia\n" if self.disallow_pages else (
                "User-agent: *\nAllow: /\nCrawl-delay: 0\n")
            if self.robots_content_signal:
                rules += f"Content-Signal: {self.robots_content_signal}\n"
            return Response(url, 200, {"Content-Type": "text/plain"}, rules.encode())
        page = int(parse_qs(parsed.query)["page[number]"][0])
        if page in self.failures:
            status, retry = self.failures[page]
            headers = email.message.Message()
            if retry is not None:
                headers["Retry-After"] = str(retry)
            raise HTTPError(url, status, "fixture failure", headers, BytesIO(b""))
        modified = self.modified[page]
        headers = {"Last-Modified": modified, **self.page_headers}
        if request.get_header("If-modified-since") == modified:
            response = Response(url, 304, headers)
            self.catalog_responses.append(response)
            return response
        start = (page - 1) * self.page_size
        rows = self.rows[start:start + self.page_size]
        body = json.dumps({
            "data": rows,
            "links": {},
            "meta": {"current_page": page, "last_page": math.ceil(len(self.rows) / self.page_size),
                     "per_page": self.page_size, "total": len(self.rows)},
        }).encode()
        self.page_bodies += 1
        response = Response(url, 200, headers, body)
        self.catalog_responses.append(response)
        return response


def setup(monkeypatch, rows, tmp_path):
    monkeypatch.setattr(feed, "PAGE_SIZE", 2)
    monkeypatch.setattr(feed, "MIN_REQUEST_INTERVAL", 0)
    path = tmp_path / "wiki.sqlite3"
    return path, FakeAPI(rows), Clock()


def rows_for_catalog(db_path):
    with sqlite3.connect(db_path) as db:
        return {url: title for url, title in db.execute(
            "SELECT u.url,t.title FROM wiki_urls u JOIN catalog_titles t USING(url) "
            "WHERE u.url LIKE 'https://robertsspaceindustries.com/galactapedia/article/%' "
            "AND u.status!='removed'")}


def snapshot_rows(db_path):
    with sqlite3.connect(db_path) as db:
        return db.execute("""SELECT p.url,p.title,p.body,s.file_format,p.source_id
            FROM snapshot_pages p JOIN snapshot_sources s USING(source_id)
            WHERE s.file_format='galactapedia-community-api' ORDER BY p.url""").fetchall()


def test_stable_second_pass_uses_304_without_article_bodies_or_rewrites(monkeypatch, tmp_path):
    rows = [
        article("A000000001", "alpha-one", "Alpha One", "Alpha lore body."),
        article("B000000002", "beta-two", "Beta Two", "Pending review by the Ark research team..."),
        article("C000000003", "gamma-three", "Gamma Three", "Gamma lore body."),
    ]
    db_path, api, clock = setup(monkeypatch, rows, tmp_path)

    first = feed.refresh_galactapedia(db_path, opener=api, clock=clock,
                                      interval=60, failure_backoff=60)
    assert first["status"] == "success"
    assert (first["titles"], first["article_bodies"], first["placeholders"]) == (3, 2, 1)
    assert api.page_bodies == 2
    prior_pages = snapshot_rows(db_path)
    prior_body_hashes = None
    with sqlite3.connect(db_path) as db:
        prior_body_hashes = db.execute(
            "SELECT page_number,body_sha256 FROM galactapedia_feed_pages ORDER BY page_number").fetchall()
        notes_schema = db.execute("SELECT 1 FROM sqlite_master WHERE name='note_revisions'").fetchone()
    assert notes_schema is None

    clock.advance(61)
    second = feed.refresh_galactapedia(db_path, opener=api, clock=clock,
                                       interval=60, failure_backoff=60)
    assert second["status"] == "success"
    assert second["bodies_downloaded"] == 0
    assert second["response_bytes"] == 0
    assert api.page_bodies == 2
    assert len([r for r in api.requests if "/api/galactapedia?" in r["url"]]) == 4
    assert snapshot_rows(db_path) == prior_pages
    with sqlite3.connect(db_path) as db:
        assert db.execute(
            "SELECT page_number,body_sha256 FROM galactapedia_feed_pages ORDER BY page_number").fetchall() == prior_body_hashes
    status = feed.feed_status(db_path, clock=clock)
    assert status["titles"] == 3
    assert status["article_bodies"] == 2
    assert status["placeholders"] == 1
    assert status["retained_prior_bodies"] == 0
    assert status["gaps"] == {"placeholder": 1, "missing": 0}


def test_updated_shard_and_permanent_removal_update_only_verified_feed_data(monkeypatch, tmp_path):
    rows = [
        article("A000000001", "alpha-one", "Alpha One", "Alpha lore body v1."),
        article("B000000002", "beta-two", "Beta Two", "Beta lore body."),
        article("C000000003", "gamma-three", "Gamma Three", "Gamma lore body."),
    ]
    db_path, api, clock = setup(monkeypatch, rows, tmp_path)
    first = feed.refresh_galactapedia(db_path, opener=api, clock=clock,
                                      interval=60, failure_backoff=60)
    source_id = first["source_id"]
    clock.advance(61)

    rows[0] = article("A000000001", "alpha-one", "Alpha One Updated", "Alpha lore body v2.")
    api.replace(rows, revision=1, page=1)
    updated = feed.refresh_galactapedia(db_path, opener=api, clock=clock,
                                        interval=60, failure_backoff=60)
    assert updated["status"] == "success"
    assert updated["bodies_downloaded"] == 1
    assert updated["source_id"] == source_id
    current = {row[0]: row[1:3] for row in snapshot_rows(db_path)}
    alpha = "https://robertsspaceindustries.com/galactapedia/article/A000000001-alpha-one"
    assert current[alpha][:2] == ("Alpha One Updated", "Alpha lore body v2.")
    assert rows_for_catalog(db_path)[alpha] == "Alpha One Updated"

    clock.advance(61)
    api.replace(rows[:2], revision=2, page=1)
    removed = feed.refresh_galactapedia(db_path, opener=api, clock=clock,
                                        interval=60, failure_backoff=60)
    assert removed["status"] == "success"
    assert removed["titles"] == 2
    gamma = "https://robertsspaceindustries.com/galactapedia/article/C000000003-gamma-three"
    assert gamma not in rows_for_catalog(db_path)
    assert gamma not in {row[0] for row in snapshot_rows(db_path)}
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT status FROM wiki_urls WHERE url=?", (gamma,)).fetchone()[0] == "removed"
        assert db.execute("SELECT count(*) FROM galactapedia_feed_pages").fetchone()[0] == 1


def test_placeholder_becomes_gap_and_retains_prior_body_separately(monkeypatch, tmp_path):
    rows = [
        article("A000000001", "alpha-one", "Alpha One", "Verified Alpha body."),
        article("B000000002", "beta-two", "Beta Two", "Pending review by the Ark research team..."),
    ]
    db_path, api, clock = setup(monkeypatch, rows, tmp_path)
    first = feed.refresh_galactapedia(db_path, opener=api, clock=clock,
                                      interval=60, failure_backoff=60)
    assert first["article_bodies"] == 1
    old_rows = snapshot_rows(db_path)
    alpha = "https://robertsspaceindustries.com/galactapedia/article/A000000001-alpha-one"

    clock.advance(61)
    rows[0] = article("A000000001", "alpha-one", "Alpha One", "Pending review by the Ark research team...")
    api.replace(rows, revision=1, page=1)
    refreshed = feed.refresh_galactapedia(db_path, opener=api, clock=clock,
                                          interval=60, failure_backoff=60)
    assert refreshed["article_bodies"] == 0
    assert refreshed["retained_prior_bodies"] == 1
    assert refreshed["placeholders"] == 2
    stored = {row[0]: row for row in snapshot_rows(db_path)}
    assert stored[alpha][2] == "Verified Alpha body."
    assert stored[alpha][1] == "Alpha One"
    assert stored[alpha][3] == "galactapedia-community-api"
    status = feed.feed_status(db_path, clock=clock)
    assert status["article_bodies"] == 0
    assert status["retained_prior_bodies"] == 1
    assert status["placeholders"] == 2
    with sqlite3.connect(db_path) as db:
        gap = db.execute("""SELECT reason,retained_prior_body,discovered_at
            FROM galactapedia_feed_gaps WHERE url=?""", (alpha,)).fetchone()
    assert gap[0:2] == ("placeholder", 1)
    assert gap[2] == status["discovery_time"]
    assert old_rows[0][4] == stored[alpha][4]


def test_partial_page_failure_preserves_catalog_snapshot_and_cache_with_backoff(monkeypatch, tmp_path):
    rows = [
        article("A000000001", "alpha-one", "Alpha One", "Alpha lore v1."),
        article("B000000002", "beta-two", "Beta Two", "Beta lore."),
        article("C000000003", "gamma-three", "Gamma Three", "Gamma lore."),
    ]
    db_path, api, clock = setup(monkeypatch, rows, tmp_path)
    feed.refresh_galactapedia(db_path, opener=api, clock=clock,
                              interval=60, failure_backoff=60)
    old_snapshot = snapshot_rows(db_path)
    old_catalog = rows_for_catalog(db_path)
    with sqlite3.connect(db_path) as db:
        old_cache = db.execute(
            "SELECT page_number,body_sha256,etag,last_modified FROM galactapedia_feed_pages ORDER BY page_number").fetchall()
        last_success = db.execute("SELECT last_success FROM galactapedia_feed_state").fetchone()[0]

    clock.advance(61)
    rows[0] = article("A000000001", "alpha-one", "Changed Alpha", "Alpha lore v2.")
    api.replace(rows, revision=1, page=1)
    api.failures[2] = (500, None)
    failed = feed.refresh_galactapedia(db_path, opener=api, clock=clock,
                                       interval=60, failure_backoff=60)
    assert failed["status"] == "failed"
    assert failed["http_status"] == 500
    assert failed["failure_count"] == 1
    assert failed["next_attempt"] == clock() + 60
    assert snapshot_rows(db_path) == old_snapshot
    assert rows_for_catalog(db_path) == old_catalog
    with sqlite3.connect(db_path) as db:
        assert db.execute(
            "SELECT page_number,body_sha256,etag,last_modified FROM galactapedia_feed_pages ORDER BY page_number").fetchall() == old_cache
        assert db.execute("SELECT last_success FROM galactapedia_feed_state").fetchone()[0] == last_success
    requests_before = len(api.requests)
    backoff = feed.refresh_galactapedia(db_path, opener=api, clock=clock,
                                        interval=60, failure_backoff=60)
    assert backoff["status"] == "backoff"
    assert len(api.requests) == requests_before
    assert feed.feed_status(db_path, clock=clock)["titles"] == 3


def test_storage_denial_on_first_200_does_not_read_catalog_body(monkeypatch, tmp_path):
    db, api, clock = setup(monkeypatch, [
        article("A000000001", "alpha-one", "Alpha One", "Alpha lore body."),
    ], tmp_path)
    api.page_headers = {"Cache-Control": "max-age=60, no-store"}

    result = feed.refresh_galactapedia(db, opener=api, clock=clock, interval=60, failure_backoff=60)

    assert result["status"] == "blocked"
    response, = api.catalog_responses
    assert response.status == 200
    assert response.bytes_read == 0
    status = feed.feed_status(db, clock=clock)
    assert status["status"] == "blocked"
    assert status["article_bodies"] == status["retained_prior_bodies"] == 0
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT count(*) FROM galactapedia_feed_pages").fetchone() == (0,)


def test_storage_denial_on_304_withdraws_only_feed_and_recovers(monkeypatch, tmp_path):
    db, api, clock = setup(monkeypatch, [
        article("A000000001", "alpha-one", "Alpha One", "Alpha lore body."),
    ], tmp_path)
    options = dict(opener=api, clock=clock, interval=60, failure_backoff=60)
    assert feed.refresh_galactapedia(db, **options)["article_bodies"] == 1
    url = "https://robertsspaceindustries.com/galactapedia/article/A000000001-alpha-one"
    independent = tmp_path / "independent.jsonl"
    independent.write_text(json.dumps({
        "url": url, "title": "Authorized independent Alpha", "text": "Independent body.",
    }) + "\n", encoding="utf-8")
    imported = import_snapshot(db, independent)
    record_note("private-alpha", "Private Alpha", "My own Alpha observations.",
                status="reviewed", source=url, db_path=db)
    notes = note_history("private-alpha", db)

    clock.advance(61)
    api.page_headers = {"X-Robots-Tag": "noarchive"}
    result = feed.refresh_galactapedia(db, **options)

    assert result["status"] == "blocked"
    assert api.catalog_responses[-1].status == 304
    assert api.catalog_responses[-1].bytes_read == 0
    status = feed.feed_status(db, clock=clock)
    assert status["status"] == "blocked"
    assert status["article_bodies"] == status["retained_prior_bodies"] == 0
    assert snapshot_rows(db) == []
    assert read_snapshot_page(db, url)["source_id"] == imported["source_id"]
    assert note_history("private-alpha", db) == notes
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT count(*) FROM galactapedia_feed_pages").fetchone() == (0,)

    clock.advance(61)
    api.page_headers.clear()
    restored = feed.refresh_galactapedia(db, **options)

    assert restored["status"] == "success"
    assert restored["bodies_downloaded"] == 1
    assert feed.feed_status(db, clock=clock)["article_bodies"] == 1
    assert snapshot_rows(db)[0][2] == "Alpha lore body."


@pytest.mark.parametrize("signal", ["search=no", "ai-input=no"])
def test_matching_robots_storage_signal_blocks_before_catalog_contact(monkeypatch, tmp_path, signal):
    db, api, clock = setup(monkeypatch, [
        article("A000000001", "alpha-one", "Alpha One", "Alpha lore body."),
    ], tmp_path)
    options = dict(opener=api, clock=clock, interval=60, failure_backoff=60)
    assert feed.refresh_galactapedia(db, **options)["article_bodies"] == 1

    clock.advance(feed.ROBOTS_INTERVAL + 1)
    api.robots_content_signal = signal
    api.requests.clear()
    result = feed.refresh_galactapedia(db, **options)

    assert result["status"] == "blocked"
    assert [urlsplit(request["url"]).path for request in api.requests] == ["/robots.txt"]
    assert feed.feed_status(db, clock=clock)["article_bodies"] == 0
    assert snapshot_rows(db) == []


def test_retry_after_extends_feed_backoff_and_cross_origin_redirect_is_rejected(monkeypatch, tmp_path):
    assert feed.MIN_REQUEST_INTERVAL >= 1
    rows = [article("A000000001", "alpha-one", "Alpha One", "Alpha body.")]
    db_path, api, clock = setup(monkeypatch, rows, tmp_path)
    api.failures[1] = (429, 3600)
    failed = feed.refresh_galactapedia(db_path, opener=api, clock=clock,
                                       interval=60, failure_backoff=60)
    assert failed["status"] == "failed"
    assert failed["http_status"] == 429
    assert failed["next_attempt"] == clock() + 3600
    request = __import__("urllib.request").request.Request(feed.API_URL)
    with __import__("pytest").raises(feed.GalactapediaFeedError, match="redirect"):
        feed._SameOriginRedirect().redirect_request(
            request, None, 302, "Found", {}, "https://example.org/redirected")
