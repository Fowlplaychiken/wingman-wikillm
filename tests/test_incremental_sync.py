"""Sync the normal crawler against an independently enumerated publisher."""
from contextlib import closing
import gzip
import io
import sqlite3
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from test_web_sources import Fixture, html
from test_refresh import Opener, Response
from wiki_llm.dependencies.wingman_wiki import corpus, refresh, web_sources as web


@pytest.mark.parametrize("map_path", ["/sitemap", "/doku.php?do=sitemap", "/index.php?sitemap=index"])
def test_declared_maps_reach_extensionless_child_and_keep_validators(tmp_path, map_path):
    db = tmp_path / "wiki.sqlite3"
    host = Fixture()
    origin, root = "https://wiki.example", Edition.root
    index, child, article = origin + map_path, origin + "/maps/manual", root + "Unlinked"
    ns = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'
    host.robots += "Sitemap: " + index + " # public inventory\n"
    host.pages[index] = web.Response(200, {"ETag": '"index"'},
        f"<sitemapindex {ns}><sitemap><loc>{child}</loc></sitemap></sitemapindex>".encode())
    host.pages[child] = web.Response(200, {"ETag": '"child"'}, gzip.compress(
        f"<urlset {ns}><url><loc>{article}</loc><lastmod>2026-11-21</lastmod></url></urlset>".encode()))
    host.pages[root], host.pages[article] = html("Manual"), html("Zephyrite handbook")
    with patch.object(corpus, "robots_delay", return_value=0):
        for _ in range(2):
            assert host.run(db, (root,))["failed"] == 0
        assert web.read_web_page(db, article, (root,))["status"] == "reference"
        host.now += 86401
        host.calls.clear()
        host.pages[index] = host.pages[child] = web.Response(304, {}, b"")
        assert host.run(db, (root,))["failed"] == 0
    assert {url: h["If-None-Match"] for url, h in host.calls if "If-None-Match" in h} == {
        index: '"index"', child: '"child"'}
    assert not any(url in (root, article) for url, _ in host.calls)
    assert web.search_web_sources("Zephyrite", db, (root,))[0]["citation"] == article


def test_retired_or_invalid_map_is_distinct_from_missing_declared_map(tmp_path):
    db, host = tmp_path / "wiki.sqlite3", Fixture()
    root, index = Edition.root, "https://wiki.example/maps"
    missing, forbidden = "https://wiki.example/maps/gone", "https://wiki.example/secret"
    ns = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'
    host.robots += f"Disallow: /secret\nSitemap: {index}\n"
    host.pages[root] = html("Manual")
    def publish(*children):
        body = "".join(f"<sitemap><loc>{child}</loc></sitemap>" for child in children)
        host.pages[index] = web.Response(200, {}, f"<sitemapindex {ns}>{body}</sitemapindex>".encode())
    with patch.object(corpus, "robots_delay", return_value=0):
        publish(missing, forbidden)
        report = host.run(db, (root,))
        assert report["roots"][root]["discovery_error"] == "sitemap_robots_disallowed"
        assert report["roots"][root]["state"] == "partial"
        assert web.read_web_page(db, root, (root,))["status"] == "reference"
        host.run(db, (root,))
        host.now += 86401
        publish(missing)
        assert host.run(db, (root,))["coverage"][root]["last_error"] == "declared_sitemap_missing"
        host.now += 86401
        publish()
        report = host.run(db, (root,))
        assert report["roots"][root]["state"] == "ready" and report["coverage"][root]["last_error"] is None
    assert not any(url == forbidden for url, _ in host.calls)


@pytest.mark.parametrize(("declared", "error"), [
    ("http://wiki.example/sitemap", "unsupported_sitemap_url"),
    ("https://other.example/sitemap", "sitemap_outside_origin"),
    ("https://wiki.example/doku.php?do=edit", "unsupported_sitemap_url"),
    ("https://wiki.example/index.php?sitemap=index&action=delete", "unsupported_sitemap_url"),
])
def test_unsupported_explicit_sitemap_reports_partial_without_contact(tmp_path, declared, error):
    host, root = Fixture(), Edition.root
    host.robots += f"Sitemap: {declared}\n"
    host.pages[root] = html("Manual")
    with patch.object(corpus, "robots_delay", return_value=0):
        report = host.run(tmp_path / "wiki.sqlite3", (root,))
    assert report["roots"][root]["discovery_error"] == error
    assert report["coverage"][root]["last_error"] == error
    assert report["roots"][root]["state"] == "partial" and report["indexed"] == 1
    assert not any(url == declared for url, _ in host.calls)


@pytest.mark.parametrize(("response", "error"), [
    (web.Response(200, {}, b"<broken"), "ParseError"),
    (web.Response(500, {}, b""), "sitemap_http_500"),
])
def test_partial_map_inventory_persists_and_recovers_after_other_children_succeed(tmp_path, response, error):
    db, host = tmp_path / "wiki.sqlite3", Fixture()
    root, index = Edition.root, "https://wiki.example/inventory"
    children = [index + "/" + name for name in ("a", "b", "c")]
    ns = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'
    host.robots += f"Sitemap: {index}\n"
    host.pages[root] = html("Manual")
    nodes = "".join(f"<sitemap><loc>{child}</loc></sitemap>" for child in children)
    host.pages[index] = web.Response(200, {}, f"<sitemapindex {ns}>{nodes}</sitemapindex>".encode())
    host.pages[children[0]] = response
    for child in children[1:]:
        host.pages[child] = web.Response(200, {}, f"<urlset {ns}/>".encode())
    with patch.object(corpus, "robots_delay", return_value=0):
        first = host.run(db, (root,))
        assert first["roots"][root]["state"] == "partial" and first["failed"] == 0
        assert first["coverage"][root]["sitemaps_pending"] == 3
        second = host.run(db, (root,))
        assert second["coverage"][root]["sitemaps_pending"] == 1
        third = host.run(db, (root,))
        assert third["coverage"][root]["sitemaps_pending"] == 0
        assert third["coverage"][root]["last_error"] == error and third["roots"][root]["state"] == "partial"
        assert web.web_coverage(db, (root,))[root]["last_error"] == error
        host.pages[children[0]] = web.Response(200, {}, f"<urlset {ns}/>".encode())
        host.now += 901
        recovered = host.run(db, (root,))
        assert recovered["roots"][root]["state"] == "ready" and recovered["coverage"][root]["last_error"] is None


def test_declared_map_redirect_revalidates_query_and_keeps_sitemap_transport_limit(tmp_path):
    host, root = Fixture(), Edition.root
    index, target = "https://wiki.example/sitemap", "https://wiki.example/doku.php?do=sitemap"
    host.robots += f"Sitemap: {index}\n"
    host.pages[index] = web.Response(301, {"Location": target}, b"")
    host.pages[target] = web.Response(200, {}, b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"/>')
    host.pages[root] = html("Manual")
    db = tmp_path / "wiki.sqlite3"
    with patch.object(corpus, "robots_delay", return_value=0):
        assert host.run(db, (root,))["failed"] == 0
        headers = next(h for url, h in host.calls if url == target)
        bad = target.replace("do=sitemap", "do=edit")
        host.pages[index] = web.Response(302, {"Location": bad}, b"")
        host.now += 86401
        assert host.run(db, (root,))["roots"][root]["state"] == "partial"
        assert not any(url == bad for url, _ in host.calls)
    with patch.object(web.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("8.8.8.8", 443))]) as dns, \
         patch.object(web.http.client, "HTTPSConnection") as connection:
        reply = connection.return_value.getresponse.return_value
        reply.read.return_value = b""
        web._transport(target, headers)
        reply.read.assert_called_once_with(corpus.SITEMAP_LIMIT + 1)
        with pytest.raises(ValueError):
            web._transport(bad, headers)
        with pytest.raises(ValueError):
            web._transport(target, {})  # This query is never an article URL.
        assert dns.call_count == 1


class Edition(Fixture):
    root = "https://wiki.example/manual/"
    map_url = "https://wiki.example/sitemap.xml"
    branch = root + "Vehicles"
    deep = root + "Vehicles/Repair"
    orphan = root + "Engineering/Pump"

    def __init__(self):
        super().__init__()
        self.bodies = []
        self.stamps = {self.root: "2026-10-12", self.orphan: "2026-10-12"}
        self.map_tag = '"map1"'
        self.pages[self.root] = html("Home").replace(b"<article>", (
            '<nav><a href="Vehicles">Vehicles</a><a href="Secret" rel="nofollow">Hidden</a></nav><article>'
        ).encode())
        self.pages[self.branch] = html("Vehicles", links=["Vehicles/Repair"])
        self.pages[self.deep] = html("Repair manual")
        self.pages[self.orphan] = html("Pump manual")
        self.tags = {url: '"initial"' for url in self.pages}

    def __call__(self, url, headers):
        if url == self.map_url or url in self.tags:
            tag = self.map_tag if url == self.map_url else self.tags[url]
            if headers.get("If-None-Match") == tag:
                self.calls.append((url, headers))
                return web.Response(304, {"ETag": tag}, b"")
            self.bodies.append(url)
            if url == self.map_url:
                self.calls.append((url, headers))
                entries = "".join(f"<url><loc>{key}</loc><lastmod>{v}</lastmod></url>" for key,v in self.stamps.items())
                xml = f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{entries}</urlset>'
                return web.Response(200, {"ETag": tag}, xml.encode())
            self.calls.append((url, headers))
            return web.Response(200, {"ETag": tag}, self.pages[url])
        return super().__call__(url, headers)


def test_inventory_delta_pause_resume_and_disappearance(tmp_path):
    host = Edition()
    db = tmp_path / "wiki.sqlite3"
    truth = {host.root, host.branch, host.deep, host.orphan}
    with patch.object(corpus, "robots_delay", return_value=0):
        def run(roots=None):
            return host.run(db, (host.root,) if roots is None else roots, batch_size=20)
        assert run()["failed"] == 0
        with closing(sqlite3.connect(db)) as conn:
            assert {u for (u,) in conn.execute("SELECT url FROM web_pages WHERE storage_mode='full_text'")} == truth
        assert "Vehicles" not in web.read_web_page(db, host.root, (host.root,))["passages"][0]["text"]
        host.bodies.clear()
        host.calls.clear()
        host.now += 8 * 86400
        assert run()["failed"] == 0
        assert host.bodies == []
        assert set(u for u,h in host.calls if "If-None-Match" in h) == {host.map_url, host.branch, host.deep}
        host.map_tag = '"map2"'
        host.stamps[host.orphan] = "2026-11-21"
        host.tags[host.orphan] = '"revised"'
        host.pages[host.orphan] = html("Updated hydraulic pump")
        host.now += 86401
        host.calls.clear()
        assert run("# "+host.root)["coverage"] == {} and host.calls == []
        assert run()["failed"] == 0
        assert host.bodies == [host.map_url, host.orphan]
        assert web.search_web_sources("hydraulic", db, (host.root,))[0]["citation"] == host.orphan
        del host.stamps[host.orphan], host.tags[host.orphan]
        host.pages[host.orphan] = web.Response(410, {}, b"")
        host.map_tag = '"map3"'
        host.now += 86401
        assert run()["failed"] == 0
        assert web.read_web_page(db, host.orphan, (host.root,))["status"] == "unavailable"
        assert web.read_web_page(db, host.deep, (host.root,))["status"] == "reference"
        # A publisher can forget to change a sitemap date. Periodic audits
        # recover that edit and unchanged pages still return an empty 304.
        host.tags[host.root] = '"unannounced"'
        host.pages[host.root] = html("Unannounced revision")
        host.bodies.clear()
        host.now += 31*86400
        assert run()["failed"] == 0 and host.bodies == [host.root]
        assert web.search_web_sources("unannounced", db, (host.root,))[0]["citation"] == host.root


def test_native_cached_compressed_child_is_revisited_after_parent_304(tmp_path):
    root = corpus.SITEMAPS[0]
    sub = root.rsplit("/",1)[0]+"/game.xml.gz"
    alpha = corpus.COMMUNITY+"/Alpha"
    beta = corpus.COMMUNITY+"/Beta"

    class Site(Opener):
        changed = False
        def __init__(self):
            super().__init__()
            self.transfers = []
        def open(self, request, timeout):
            url = request.full_url
            if url.endswith("/robots.txt"):
                return super().open(request, timeout)
            etag = '"2"' if self.changed and url in (sub,beta) else '"1"'
            if request.get_header("If-none-match") == etag:
                raise HTTPError(url, 304, "Not Modified", {}, io.BytesIO())
            self.transfers.append(url)
            ns = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'
            if url == root:
                text = f'<sitemapindex {ns}><sitemap><loc>{sub}</loc></sitemap></sitemapindex>'
            elif url == sub:
                rows = "".join(f'<url><loc>{page}</loc><lastmod>2026-11-0{etag[1] if page == beta else "1"}</lastmod></url>' for page in (alpha,beta))
                text = f'<urlset {ns}>{rows}</urlset>'
            else:
                text = self.fixture.get(url).replace("Alpha", "Corrected" if url == beta and self.changed else "Alpha")
            reply = Response(text)
            if url == sub:
                reply = io.BytesIO(gzip.compress(text.encode()))
            reply.headers = {"ETag": etag}
            return reply

    host, db = Site(), tmp_path / "community.sqlite3"
    def run(now):
        source = corpus.Publisher()
        source.opener = host
        with patch.object(corpus, "robots_delay", return_value=0):
            result = refresh.refresh_once(db, publisher=source, clock=lambda: now,
                                          catalog_interval=60, page_interval=120)
        assert result.failed == 0 and result.catalogs["community"]["stale"] is False
    run(1000)
    assert host.transfers == [root,sub,alpha,beta]
    host.transfers.clear()
    run(1121)
    assert host.transfers == []
    host.changed = True
    run(1242)
    assert host.transfers == [sub,beta]
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT count(*) FROM page_search WHERE page_search MATCH 'Corrected'").fetchone() == (1,)
