"""Local publisher originals require a declaration and a complete, permitted license."""
from html import escape
from pathlib import Path
import sqlite3
from urllib.parse import quote
from unittest.mock import patch

import pytest

from test_web_sources import Fixture
from wiki_llm.dependencies.wingman_wiki import web_sources as web

ROOT = "https://wiki.example/en/Manual/Tutorial/"
LICENSE = "https://wiki.example/License"
NAMES = ("", "Basic Network", "Buses", "Conclusion", "In-game tutorial", "Planes", "Trains",
         "Two Platforms", "Two Tracks")
URLS = tuple(ROOT + quote(name) for name in NAMES)
DECL = '<footer><div id="content-bottom-copyright">Content is available under <a href="/License">GNU Free Documentation License</a></div></footer>'
GFDL = Path(__file__).with_name("fixtures").joinpath("gfdl-1.3.txt").read_text(encoding="utf-8")
TERMS = ("<html><body><main><pre>" + escape(GFDL) + "</pre></main></body></html>").encode()


@pytest.fixture(autouse=True)
def no_fixture_delay():
    with patch.object(web.corpus, "robots_delay", return_value=0):
        yield


def article(heading="Buses", *, footer=DECL, head="", body_link=""):
    body = "<p>" + ("Original publisher tutorial about buying buses and assigning orders. " * 30) + "</p>"
    return (f"<html><head>{head}<title>{heading}</title></head><body><main>{body_link}{body}"
            f"<p>Endmarker for {heading}.</p></main>{footer}"
            "<a href='/history'>View Source</a></body></html>").encode()


def fixture():
    f = Fixture()
    f.pages[LICENSE] = web.Response(200, {"ETag": '"terms-1"'}, TERMS)
    for url in URLS:
        f.pages[url] = web.Response(200, {"ETag": '"page-1"'}, article(url.rsplit("/", 1)[-1]))
    links = "".join(f'<a href="{url}">Article</a>' for url in URLS[1:])
    f.pages[ROOT] = web.Response(200, {"ETag": '"page-1"'}, article(body_link=links))
    return f


def cached(path, table):
    with sqlite3.connect(path) as db:
        return db.execute("SELECT count(*) FROM " + table).fetchone()[0]


def test_nine_pages_keep_exact_html_notices_and_one_license_offline(tmp_path):
    db = tmp_path / "private.sqlite"
    f = fixture()
    assert f.run(db, (ROOT,), batch_size=15)["indexed"] == 9
    assert [u for u, _ in f.calls].count(LICENSE) == 1
    with patch("socket.getaddrinfo", side_effect=AssertionError("offline")):
        for url in URLS:
            result = web.read_web_page(db, url, (ROOT,))
            assert result["passages"][0]["storage_mode"] == "full_text"
            assert result["publisher_license_url"] == LICENSE
            copy = web.read_publisher_copy(db, url, (ROOT,))
            assert copy["article_html"] == f.pages[url].body
            assert copy["license_document"] == TERMS
            assert copy["citation"] == url and copy["license"] == "GFDL-1.3"
        assert web.search_web_sources("Endmarker", db, (ROOT,))
        assert web.read_publisher_copy(db, URLS[2], ()) is None
    assert cached(db, "web_publisher_licenses") == 1
    assert cached(db, "web_publisher_copies") == 9


@pytest.mark.parametrize(("head","footer","body_link","full"), [
    ('<link rel="license" href="/License">', "", "", True),
    ("", DECL.replace("GNU Free Documentation License", "<span>GNU Free</span> Documentation License"), "", True),
    ("", "", '<a href="/License">GNU Free Documentation License</a>', False),
    ("", "", '<a rel="license" href="/License">GNU Free Documentation License</a>', False),
    ("", "", DECL, False),
    ("", DECL.replace("/License", "https://other.example/License"), "", False),
])
def test_declarations_and_body_citations(tmp_path, head, footer, body_link, full):
    db = tmp_path / "library.sqlite"; f = fixture()
    f.pages[ROOT] = article(head=head, footer=footer, body_link=body_link)
    f.run(db, (ROOT,), batch_size=1)
    assert bool(web.read_publisher_copy(db, ROOT, (ROOT,))) is full
    assert (LICENSE in [u for u, _ in f.calls]) is full


def test_http_relation_keeps_plain_text_then_revokes_on_changed_304(tmp_path):
    db = tmp_path / "library.sqlite"
    f = fixture()
    raw_terms = GFDL.encode()
    f.pages[LICENSE] = web.Response(200, {"Content-Type": "text/plain; charset=utf-8"}, raw_terms)
    f.pages[ROOT] = web.Response(200, {"Link": '</License>; rel="license"', "ETag": "article"},
                                 article(footer=""))
    f.run(db, (ROOT,), batch_size=1)
    assert web.read_publisher_copy(db, ROOT, (ROOT,))["license_document"] == raw_terms
    f.now += 604801
    f.pages[ROOT] = web.Response(304, {"Link": '</terms>; rel="license"'}, b"")
    f.run(db, (ROOT,), batch_size=1)
    assert web.read_publisher_copy(db, ROOT, (ROOT,)) is None
    assert not web.search_web_sources("Endmarker", db, (ROOT,))


@pytest.mark.parametrize("response", [
    web.Response(200, {}, TERMS.replace(b"VERBATIM COPYING", b"OTHER COPYING", 1)),
    web.Response(200, {}, b"<html><article><p>GNU Free Documentation License Version 1.3</p></article></html>"),
    web.Response(200, {"Cache-Control": "no-store"}, TERMS),
    web.Response(200, {"X-Robots-Tag": "noarchive"}, TERMS),
    web.Response(200, {"Content-Signal": "use=reference"}, TERMS),
    web.Response(304, {"Cache-Control": "no-store"}, b""),
    web.Response(304, {"Content-Signal": "use=immediate"}, b""),
])
def test_changed_or_restricted_terms_withdraw_all_active_copies_even_before_articles_due(tmp_path, response):
    db = tmp_path / "library.sqlite"; f = fixture()
    f.run(db, (ROOT,), batch_size=15)
    f.calls.clear(); f.now += 86401
    f.pages[LICENSE] = response
    f.run(db, (ROOT,), batch_size=1)
    license_calls = [h for u,h in f.calls if u == LICENSE]
    assert len(license_calls) == 1 and license_calls[0]["If-None-Match"] == '"terms-1"'
    assert cached(db, "web_publisher_copies") == 0
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM web_publisher_licenses WHERE body IS NOT NULL").fetchone()[0] == 0
    assert not web.search_web_sources("Endmarker", db, (ROOT,))


@pytest.mark.parametrize(("status","headers","unavailable"), [
    (304, {"Content-Signal": "search=yes, use=immediate"}, True),
    (200, {"Content-Signal": "use=immediate"}, True),
    (304, {"Cache-Control": "no-store"}, True),
    (304, {"X-Robots-Tag": "noarchive"}, True),
    (200, {"Cache-Control": "no-store"}, True),
    (200, {"Content-Signal": "use=reference"}, False),
])
def test_article_response_revokes_previous_raw_copy_and_search_text(tmp_path, status, headers, unavailable):
    db = tmp_path / "library.sqlite"; f = fixture()
    f.pages[ROOT] = web.Response(200, {"ETag": '"page-1"'}, article())
    f.run(db, (ROOT,)); f.now += 604801
    f.pages[ROOT] = web.Response(status, headers, b"" if status == 304 else article())
    f.run(db, (ROOT,), batch_size=1)
    assert web.read_publisher_copy(db, ROOT, (ROOT,)) is None
    assert not web.search_web_sources("Endmarker", db, (ROOT,))
    assert (web.read_web_page(db, ROOT, (ROOT,))["status"] == "unavailable") is unavailable
    assert cached(db, "web_publisher_licenses") == 0


def test_robots_withdraws_shared_license_without_fetching_it_and_errors_retain_verified_pages(tmp_path):
    db = tmp_path / "library.sqlite"; f = fixture()
    f.pages[LICENSE] = OSError("temporarily offline")
    f.run(db, (ROOT,), batch_size=15)
    assert [u for u, _ in f.calls].count(LICENSE) == 1
    assert all(web.read_web_page(db, u, (ROOT,))["passages"][0]["storage_mode"] == "short_excerpt" for u in URLS)
    f.now += 1000; f.pages[LICENSE] = web.Response(200, {"ETag": "license"}, TERMS)
    f.run(db, (ROOT,), batch_size=15)
    assert cached(db, "web_publisher_copies") == 9
    f.now += 86401; f.pages[LICENSE] = OSError("ordinary network failure")
    f.run(db, (ROOT,), batch_size=1)
    assert cached(db, "web_publisher_copies") == 9
    f.now += 1000;f.pages[LICENSE] = web.Response(304, {}, b"")
    f.run(db, (ROOT,), batch_size=1)
    assert web.read_publisher_copy(db, ROOT, (ROOT,))["license_document"] == TERMS
    f.now += 86401; f.calls.clear(); f.robots += "Disallow: /License\n"
    f.run(db, (ROOT,), batch_size=1)
    assert not [u for u,_ in f.calls if u == LICENSE]
    assert cached(db, "web_publisher_copies") == 0
    assert not web.search_web_sources("Endmarker", db, (ROOT,))
