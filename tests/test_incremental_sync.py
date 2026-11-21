"""Sync the normal crawler against an independently enumerated publisher."""
from contextlib import closing
import gzip
import io
import sqlite3
from unittest.mock import patch
from urllib.error import HTTPError

from test_web_sources import Fixture, html
from test_refresh import Opener, Response
from wiki_llm.dependencies.wingman_wiki import corpus, refresh, web_sources as web


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
