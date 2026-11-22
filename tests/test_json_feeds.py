"""Bounded offline tests for public JSON API snapshots."""
import hashlib
import json
import sqlite3
from unittest.mock import patch
from urllib.parse import parse_qsl, urlsplit

import pytest

from wiki_llm.dependencies.wingman_wiki import json_feeds as feeds
from wiki_llm.dependencies.wingman_wiki import web_sources
from wiki_llm.dependencies.wingman_wiki.snapshots import read_snapshot_page, search_snapshots


API = "https://api.open5e.com/v2/conditions/"
ARTICLE_A = "https://www.open5e.com/conditions/cold/"
ARTICLE_B = "https://www.open5e.com/conditions/fear/"


def _body(items, *, next_url=None, total=2):
    return json.dumps({"count": total, "next": next_url, "results": items}).encode()


def _item(name, slug, description):
    return {"name": name, "slug": slug, "desc": [{"desc": description}]}


class Fixture:
    def __init__(self):
        self.calls = []
        self.policy = None
        self.pages = {
            API: ("page-1", _body([_item("Cold", "cold", "Cold imposes a slowing effect." )],
                                  next_url="?page=2")),
            API + "?page=2": ("page-2", _body([_item("Fear", "fear", "Fear can disrupt concentration.")]))
        }

    def __call__(self, url, headers, *, validate_url):
        validate_url(url)
        self.calls.append((url, dict(headers)))
        if url.endswith("/robots.txt"):
            return web_sources.Response(404, {}, b"")
        etag, body = self.pages[url]
        if headers.get("If-None-Match") == etag:
            policy = self.policy if url == API else None
            response_headers = {"ETag": etag}
            if policy:
                response_headers.update(policy)
            return web_sources.Response(304, response_headers, b"")
        return web_sources.Response(200, {"Content-Type": "application/json", "ETag": etag}, body)


@pytest.fixture(autouse=True)
def no_host_delay():
    with patch.object(web_sources.corpus, "robots_delay", return_value=0):
        yield


def mapped_feed():
    return feeds.parse_feeds(json.dumps({
        "url": API,
        "items": "results",
        "title": "name",
        "text": "desc.0.desc",
        "url_template": "https://www.open5e.com/conditions/{id}/",
        "id_field": "slug",
        "next": "next",
        "total": "count",
    }))[0]


def _install(monkeypatch, fixture):
    monkeypatch.setattr(web_sources, "_transport", fixture)


def test_mapping_supports_dotted_array_indices_and_default_aliases():
    feed = mapped_feed()
    assert feeds._normalized(_item("Cold", "cold", "A cold description."), feed) == {
        "url": ARTICLE_A, "title": "Cold", "text": "A cold description."
    }
    default = feeds.parse_feeds(API)[0]
    assert default.items == "results|items|data|records|entries"
    assert feeds._normalized({"name": "Ice", "description": ["First.", "Second."],
                              "link": ARTICLE_A}, default)["text"] == "First.\nSecond."


def test_unset_next_param_preserves_legacy_mapping_digest():
    feed = feeds.JsonFeed(API)
    legacy_fields = {"url", "items", "title", "text", "url_field", "next", "total",
                     "url_template", "id_field"}
    legacy_value = {name: getattr(feed, name) for name in legacy_fields}
    expected = hashlib.sha256(json.dumps(legacy_value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    assert feed.mapping_sha256 == expected
    assert feeds.parse_feeds(API)[0].mapping_sha256 == expected
    configured = feeds.parse_feeds(json.dumps({"url": API, "next_param": None}))[0]
    assert configured.mapping_sha256 == expected
    cursor_feed = feeds.parse_feeds(json.dumps({"url": API, "next": "next_cursor",
                                                "next_param": "cursor"}))[0]
    assert cursor_feed.mapping_sha256 != expected


def test_cursor_token_builds_same_origin_url_and_changes_only_its_parameter():
    base = API + "?document__key=a5e-ag&cursor=stale&limit=10&filter=some%2Fvalue&cursor=duplicate"
    feed = feeds.parse_feeds(json.dumps({"url": base, "next": "pageInfo.endCursor",
                                         "next_param": "cursor"}))[0]
    token = "https://evil.example/path?api_key=dummy&x=1"
    response = web_sources.Response(200, {}, b"")

    result = feeds._extract_next({"pageInfo": {"endCursor": token}}, feed, base, response, None)

    parts = urlsplit(result)
    assert parts.scheme == "https" and parts.netloc == "api.open5e.com"
    assert parts.query == (
        "document__key=a5e-ag&limit=10&filter=some%2Fvalue&cursor="
        "https%3A%2F%2Fevil.example%2Fpath%3Fapi_key%3Ddummy%26x%3D1"
    )
    assert parse_qsl(parts.query, keep_blank_values=True) == [
        ("document__key", "a5e-ag"), ("limit", "10"), ("filter", "some/value"), ("cursor", token)
    ]


def test_integer_next_page_uses_the_configured_query_parameter():
    base = API + "?document__key=a5e-ag&limit=10"
    feed = feeds.parse_feeds(json.dumps({"url": base, "next": "pagination.nextPage",
                                         "next_param": "page"}))[0]
    response = web_sources.Response(200, {}, b"")

    result = feeds._extract_next({"pagination": {"nextPage": 2}}, feed, base, response, None)

    assert result == base + "&page=2"


@pytest.mark.parametrize("value", [None, "", False])
def test_empty_cursor_is_terminal_or_uses_existing_link_header(value):
    base = API + "?limit=10"
    feed = feeds.parse_feeds(json.dumps({"url": base, "next": "next_cursor",
                                         "next_param": "cursor"}))[0]
    no_link = web_sources.Response(200, {}, b"")
    assert feeds._extract_next({"next_cursor": value}, feed, base, no_link, None) is None

    with_link = web_sources.Response(200, {"link": '<?page=1>; rel="prev", <?page=2>; rel="next"'}, b"")
    assert feeds._extract_next({"next_cursor": value}, feed, base, with_link, None) == API + "?page=2"


@pytest.mark.parametrize("value", [True, 1.5, [], {}])
def test_cursor_mapping_rejects_non_scalar_values(value):
    feed = feeds.parse_feeds(json.dumps({"url": API, "next": "next_cursor",
                                         "next_param": "cursor"}))[0]
    with pytest.raises(ValueError, match="token or integer"):
        feeds._extract_next({"next_cursor": value}, feed, API, web_sources.Response(200, {}, b""), None)


@pytest.mark.parametrize("name", ["", "cursor&api_key", "cursor=value", "cursor/value", "cursor%2F"])
def test_next_param_must_be_a_simple_query_parameter_name(name):
    with pytest.raises(ValueError, match="simple query parameter name"):
        feeds.parse_feeds(json.dumps({"url": API, "next": "next_cursor", "next_param": name}))


def test_refresh_publishes_complete_feed_and_reuses_each_page_validator(tmp_path, monkeypatch):
    fixture = Fixture()
    _install(monkeypatch, fixture)
    feed = mapped_feed()
    db = tmp_path / "wiki.sqlite3"
    clock = lambda: 1_800_000_000.0

    first = feeds.refresh_feeds(db, (feed,), clock=clock)
    assert first[feed.url]["status"] == "ready"
    assert first[feed.url]["articles"] == 2
    status = feeds.feed_status(db, feed, clock=clock)
    assert status["readable"] is True and status["articles"] == 2

    snapshot_db = feeds.feed_db_path(db, feed)
    result = read_snapshot_page(snapshot_db, ARTICLE_A)
    assert result["status"] == "snapshot" and result["text"] == "Cold imposes a slowing effect."
    assert search_snapshots("disrupt concentration", snapshot_db)[0]["url"] == ARTICLE_B

    with sqlite3.connect(db) as state_db:
        state_db.execute("UPDATE json_feed_state SET next_attempt=0 WHERE url=?", (feed.url,))
    second = feeds.refresh_feeds(db, (feed,), clock=clock)
    assert second[feed.url]["status"] == "ready"
    conditionals = [(url, headers.get("If-None-Match")) for url, headers in fixture.calls
                    if url in fixture.pages and headers.get("If-None-Match")]
    assert conditionals[-2:] == [(API, "page-1"), (API + "?page=2", "page-2")]
    assert feeds.feed_status(db, feed)["articles"] == 2


@pytest.mark.parametrize("policy", [
    {"Content-Signal": "search=yes, use=immediate"},
    {"X-Robots-Tag": "noarchive"},
    {"Cache-Control": "private, no-store"},
    {"Content-Signal": "use=reference"},
])
def test_policy_change_on_304_hides_and_purges_published_copy(tmp_path, monkeypatch, policy):
    fixture = Fixture()
    _install(monkeypatch, fixture)
    feed = mapped_feed()
    db = tmp_path / "wiki.sqlite3"
    feeds.refresh_feeds(db, (feed,), clock=lambda: 1_800_000_000.0)
    with sqlite3.connect(db) as state_db:
        state_db.execute("UPDATE json_feed_state SET next_attempt=0 WHERE url=?", (feed.url,))
    fixture.policy = policy

    result = feeds.refresh_feeds(db, (feed,), clock=lambda: 1_800_000_001.0)
    assert result[feed.url]["status"] == "blocked"
    assert feeds.feed_status(db, feed)["readable"] is False
    assert search_snapshots("slowing effect", feeds.feed_db_path(db, feed)) == []


def test_duplicate_article_urls_across_pages_do_not_publish(tmp_path, monkeypatch):
    fixture = Fixture()
    fixture.pages[API + "?page=2"] = ("page-2", _body(
        [_item("Fear", "cold", "This repeats the first article URL.")]))
    _install(monkeypatch, fixture)
    feed = mapped_feed()

    result = feeds.refresh_feeds(tmp_path / "wiki.sqlite3", (feed,), clock=lambda: 1_800_000_000.0)
    assert result[feed.url]["status"] == "failed"
    assert feeds.feed_status(tmp_path / "wiki.sqlite3", feed)["readable"] is False


def test_total_mismatch_retry_restarts_the_staged_inventory(tmp_path, monkeypatch):
    fixture = Fixture()
    fixture.pages[API] = ("page-1-v1", _body(
        [_item("Cold", "cold", "Cold imposes a slowing effect.")], next_url="?page=2", total=3))
    fixture.pages[API + "?page=2"] = ("page-2-v1", _body(
        [_item("Fear", "fear", "Fear can disrupt concentration.")], total=3))
    _install(monkeypatch, fixture)
    feed = mapped_feed()
    db = tmp_path / "wiki.sqlite3"
    first = feeds.refresh_feeds(db, (feed,), clock=lambda: 1_800_000_000.0)
    assert first[feed.url]["status"] == "failed"

    fixture.pages[API] = ("page-1-v2", _body(
        [_item("Cold", "cold", "Cold imposes a slowing effect.")], next_url="?page=2", total=2))
    fixture.pages[API + "?page=2"] = ("page-2-v2", _body(
        [_item("Fear", "fear", "Fear can disrupt concentration.")], total=2))
    with sqlite3.connect(db) as state_db:
        state_db.execute("UPDATE json_feed_state SET next_attempt=0 WHERE url=?", (feed.url,))

    retried = feeds.refresh_feeds(db, (feed,), clock=lambda: 1_800_000_001.0)
    assert retried[feed.url]["status"] == "ready"
    assert feeds.feed_status(db, feed)["articles"] == 2


def test_sidecar_identity_includes_main_database_filename(tmp_path):
    feed = mapped_feed()
    first = feeds.feed_db_path(tmp_path / "one.sqlite3", feed)
    second = feeds.feed_db_path(tmp_path / "two.sqlite3", feed)
    assert first != second


def test_empty_publisher_inventory_retires_previous_articles(tmp_path, monkeypatch):
    fixture = Fixture()
    _install(monkeypatch, fixture)
    feed = mapped_feed()
    db = tmp_path / "wiki.sqlite3"
    feeds.refresh_feeds(db, (feed,), clock=lambda: 1_800_000_000)
    fixture.pages[API] = ("empty", _body([], total=0))
    cleared = feeds.refresh_feeds(db, (feed,), clock=lambda: 1_800_100_000)
    assert cleared[feed.url]["articles"] == 0
    assert read_snapshot_page(feeds.feed_db_path(db, feed), ARTICLE_A)["status"] == "not_found"
    with sqlite3.connect(feeds.feed_db_path(db, feed)) as con:
        assert con.execute("SELECT count(*) FROM source_changes WHERE reason='snapshot_page_removed'").fetchone()[0] == 2


def test_http_link_pagination_survives_conditional_refresh(tmp_path, monkeypatch):
    first = {"name": "First", "description": "A frozen lake.", "url": ARTICLE_A}
    second = {"name": "Second", "description": "A second lake.", "url": ARTICLE_B}
    later = API + "?cursor=a,b"
    checked = []
    def transport(url, headers, *, validate_url):
        validate_url(url)
        if url.endswith("/robots.txt"):
            return web_sources.Response(404, {}, b"")
        checked.append(url)
        if "If-None-Match" in headers:
            return web_sources.Response(304, {}, b"")
        assert url in (API, later)
        meta = {"ETag": url}
        if url == API:
            meta["Link"] = '<?page=1>; title="first, page"; rel="first", <?cursor=a,b>; rel="next"'
        return web_sources.Response(200, meta, json.dumps([first if url == API else second]).encode())
    _install(monkeypatch, transport)
    feed = feeds.parse_feeds(API)[0]
    db = tmp_path / "wiki.sqlite3"
    for now in (1_800_000_000, 1_800_100_000):
        result = feeds.refresh_feeds(db, (feed,), clock=lambda: now)
        assert result[feed.url]["articles"] == 2
    assert checked == [API, later, API, later]


def test_json_cursor_pagination_survives_conditional_refresh(tmp_path, monkeypatch):
    base = API + "?document__key=a5e-ag&limit=1"
    token = "opaque+&/=?"
    later = API + "?document__key=a5e-ag&limit=1&cursor=opaque%2B%26%2F%3D%3F"
    first = {"count": 2, "next_cursor": token,
             "results": [_item("Cold", "cold", "Cold imposes a slowing effect.")]}
    second = {"count": 2, "next_cursor": "",
              "results": [_item("Fear", "fear", "Fear can disrupt concentration.")]}
    responses = {base: first, later: second}
    checked = []

    def transport(url, headers, *, validate_url):
        validate_url(url)
        if url.endswith("/robots.txt"):
            return web_sources.Response(404, {}, b"")
        assert url in responses
        checked.append((url, dict(headers)))
        validator = "etag:" + url
        if headers.get("If-None-Match") == validator:
            return web_sources.Response(304, {"ETag": validator}, b"")
        return web_sources.Response(200, {"Content-Type": "application/json", "ETag": validator},
                                    json.dumps(responses[url]).encode())

    monkeypatch.setattr(web_sources, "_transport", transport)
    feed = feeds.parse_feeds(json.dumps({
        "url": base,
        "items": "results",
        "title": "name",
        "text": "desc.0.desc",
        "url_template": "https://www.open5e.com/conditions/{id}/",
        "id_field": "slug",
        "next": "next_cursor",
        "next_param": "cursor",
        "total": "count",
    }))[0]
    db = tmp_path / "wiki.sqlite3"

    first_run = feeds.refresh_feeds(db, (feed,), clock=lambda: 1_800_000_000.0)
    assert first_run[feed.url]["status"] == "ready"
    assert first_run[feed.url]["articles"] == 2
    with sqlite3.connect(db) as state_db:
        state_db.execute("UPDATE json_feed_state SET next_attempt=0 WHERE url=?", (feed.url,))
    second_run = feeds.refresh_feeds(db, (feed,), clock=lambda: 1_800_000_001.0)

    assert second_run[feed.url]["status"] == "ready"
    assert second_run[feed.url]["articles"] == 2
    assert [url for url, _ in checked] == [base, later, base, later]
    assert [headers.get("If-None-Match") for _, headers in checked] == [
        None, None, "etag:" + base, "etag:" + later
    ]
    result = read_snapshot_page(feeds.feed_db_path(db, feed), ARTICLE_A)
    assert result["status"] == "snapshot" and result["text"] == "Cold imposes a slowing effect."
