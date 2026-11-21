"""Build a local reference index from publishers' public sitemaps and pages."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import math
import os
import re
import sqlite3
import sys
import threading
import time
from contextlib import closing
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import NamedTuple
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from xml.etree import ElementTree

AGENT = "StarCitizenComputerWiki/1.0 (+https://github.com/Fowlplaychiken/wingman-wikillm)"
COMMUNITY = "https://starcitizen.tools"
RSI = "https://robertsspaceindustries.com"
SUPPORT = "https://support.robertsspaceindustries.com"
SITEMAPS = (
    "https://sitemap.starcitizen.tools/sitemap-index-starcitizentools.xml",
    f"{SUPPORT}/hc/sitemap.xml",
    f"{RSI}/sitemap.xml",
)
GALACTAPEDIA = f"{RSI}/galactapedia"
# Maximum uncompressed size of one sitemap in the publisher protocol.
SITEMAP_LIMIT = 52_428_800
SCHEMA = """
CREATE TABLE IF NOT EXISTS wiki_urls(url TEXT PRIMARY KEY, lastmod TEXT,
    pageid INTEGER, checked_at TEXT, status TEXT NOT NULL DEFAULT 'pending');
CREATE TABLE IF NOT EXISTS catalog_titles(url TEXT PRIMARY KEY, title TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_changes(url TEXT PRIMARY KEY,
    observed_at TEXT NOT NULL, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS wiki_http_cache(url TEXT PRIMARY KEY,etag TEXT,modified TEXT,
    digest TEXT,sitemap TEXT);
CREATE TABLE IF NOT EXISTS pages(pageid INTEGER PRIMARY KEY, title TEXT, revision INTEGER,
    timestamp TEXT, wikitext TEXT, extract TEXT);
CREATE TABLE IF NOT EXISTS page_sources(pageid INTEGER PRIMARY KEY, url TEXT UNIQUE NOT NULL,
    publisher TEXT NOT NULL, retrieved_at TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS page_search USING fts5(title, extract,
    content='pages', content_rowid='pageid', tokenize='porter unicode61');
CREATE VIRTUAL TABLE IF NOT EXISTS catalog_search USING fts5(url UNINDEXED, title,
    publisher UNINDEXED, tokenize='porter unicode61');
-- Retain only a page's deduplicated vocabulary, never its full source prose.
CREATE VIRTUAL TABLE IF NOT EXISTS page_vocabulary USING fts5(
    terms, tokenize='porter unicode61');
CREATE TRIGGER IF NOT EXISTS fts_insert AFTER INSERT ON pages BEGIN
    INSERT INTO page_search(rowid, title, extract) VALUES (new.pageid, new.title, new.extract);
END;
CREATE TRIGGER IF NOT EXISTS fts_delete AFTER DELETE ON pages BEGIN
    INSERT INTO page_search(page_search,rowid,title,extract)
        VALUES ('delete',old.pageid,old.title,old.extract);
END;
CREATE TRIGGER IF NOT EXISTS fts_update AFTER UPDATE ON pages BEGIN
    INSERT INTO page_search(page_search,rowid,title,extract)
        VALUES ('delete',old.pageid,old.title,old.extract);
    INSERT INTO page_search(rowid,title,extract) VALUES (new.pageid,new.title,new.extract);
END;
"""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class RefreshCancelled(Exception):
    pass


class PublisherRateLimited(Exception):
    def __init__(self, host: str, retry_until: float):
        super().__init__("publisher cooldown")
        self.host, self.retry_until = host, retry_until


def retry_after(value: str | None, now: float) -> float:
    """Return an absolute retry deadline; malformed/absent values use 15 min."""
    try:
        if value is not None and re.fullmatch(r"\d+", value.strip()):
            seconds = float(value)
            if math.isfinite(seconds) and math.isfinite(now + seconds):
                return max(now + 5, now + seconds)
        elif value:
            stamp = parsedate_to_datetime(value)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=UTC)
            return max(now + 5, stamp.timestamp())
    except (ValueError, TypeError, OverflowError):
        pass
    return now + 900


def contact_phase(url: str) -> str:
    path = urlsplit(url).path
    if path == "/robots.txt":
        return "robots"
    if path.endswith(".xml"):
        return "sitemap"
    if path == "/galactapedia/graphql":
        return "catalog"
    return "article"


class Publisher:
    def __init__(self, stop_event: threading.Event | None = None) -> None:
        self.opener = build_opener(NoRedirect())
        self.rules: dict[str, str] = {}
        self.last: dict[str, float] = {}
        self.stop_event = stop_event
        self.request_started = None
        self.request_failed = None
        self.blocked_until: dict[str, float] = {}
        self.clock = time.time

    def check_cancelled(self) -> None:
        if self.stop_event is not None and self.stop_event.is_set():
            raise RefreshCancelled()

    def _pace(self, url: str, phase: str | None = None) -> None:
        parsed = urlsplit(url)
        host = parsed.netloc
        delay = 5 if host.endswith("starcitizen.tools") else 1
        rules = self.rules.get(f"{parsed.scheme}://{host}", "")
        self.check_cancelled()
        until = self.blocked_until.get(host, 0)
        if until > self.clock():
            raise PublisherRateLimited(host, until)
        wait = max(0, robots_delay(rules, delay) - (time.monotonic() - self.last.get(host, 0)))
        if self.stop_event is None:
            time.sleep(wait)
        elif self.stop_event.wait(wait):
            raise RefreshCancelled()
        self.last[host] = time.monotonic()
        if self.request_started is not None:
            self.request_started(host, phase or contact_phase(url))

    def _get(self, url: str, limit: int, body: bytes | None = None) -> str:
        text, _, _ = self._request(url, limit, body, {})
        return text

    def _request(self, url, limit, body, conditional):
        self._pace(url)
        headers = {"User-Agent": AGENT, **conditional}
        if body is not None:
            headers["Content-Type"] = "application/json"
        host, phase = urlsplit(url).netloc, contact_phase(url)
        try:
            with self.opener.open(Request(url, data=body, headers=headers), timeout=15) as response:
                data = response.read(limit + 1)
                etag = response.headers.get("ETag")
                modified = response.headers.get("Last-Modified")
            self.check_cancelled()
            if len(data) > limit:
                raise ValueError("publisher response too large")
            if data.startswith(b"\x1f\x8b"):
                with gzip.GzipFile(fileobj=io.BytesIO(data)) as archive:
                    data = archive.read(limit + 1)
                if len(data) > limit:
                    raise ValueError("expanded publisher response too large")
            return data.decode("utf-8-sig"), etag, modified
        except RefreshCancelled:
            raise
        except Exception as exc:
            if isinstance(exc, HTTPError) and exc.code == 304 and conditional:
                exc.close()
                return None, None, None
            self._record_failure(host, phase, exc)
            raise

    def conditional_get(self, url, limit, etag, modified):
        if type(self).get is not Publisher.get:
            # Existing publisher adapters may implement their own transport.
            return self.get(url, limit), None, None
        if not self.allowed(url):
            raise PermissionError(f"publisher robots.txt excludes {url}")
        conditional = {k: v for k, v in (("If-None-Match", etag), ("If-Modified-Since", modified)) if v}
        return self._request(url, limit, None, conditional)

    def _record_failure(self, host: str, phase: str, exc: Exception) -> None:
        status = exc.code if isinstance(exc, HTTPError) else None
        until = retry_after(exc.headers.get("Retry-After") if exc.headers else None, self.clock()) if status == 429 else 0
        if isinstance(exc, HTTPError):
            exc.close()
        if until:
            self.blocked_until[host] = until
        if self.request_failed is not None:
            self.request_failed(host, phase, type(exc).__name__, status, until)
        if status == 429:
            raise PublisherRateLimited(host, until) from None

    def allowed(self, url: str) -> bool:
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if parsed.scheme != "https" or parsed.netloc not in {
            "starcitizen.tools",
            "sitemap.starcitizen.tools",
            "robertsspaceindustries.com",
            "support.robertsspaceindustries.com",
        }:
            return False
        if origin not in self.rules:
            self.rules[origin] = self._get(origin + "/robots.txt", 256_000)
        return allowed_by_robots(
            self.rules[origin], parsed.path + ("?" + parsed.query if parsed.query else "")
        )

    def get(self, url: str, limit: int = 4_000_000) -> str:
        if not self.allowed(url):
            raise PermissionError(f"publisher robots.txt excludes {url}")
        return self._get(url, limit)

    def last_modified(self, url: str) -> str:
        if not self.allowed(url):
            raise PermissionError(f"publisher robots.txt excludes {url}")
        self._pace(url, "head")
        request = Request(
            url, headers={"User-Agent": AGENT, "Cache-Control": "no-cache"}, method="HEAD"
        )
        try:
            with self.opener.open(request, timeout=15) as response:
                modified = response.headers.get("Last-Modified")
        except Exception as exc:
            self._record_failure(urlsplit(url).netloc, "head", exc)
            raise
        if not modified:
            raise ValueError("canonical page response has no Last-Modified metadata")
        return modified

    def galactapedia(self):
        url = GALACTAPEDIA + "/graphql"
        if not self.allowed(url):
            raise PermissionError(f"publisher robots.txt excludes {url}")
        query = """query ReferenceCatalog($after: String) {
          allArticle(first: 100, after: $after) {
            totalCount edges { node { id title slug } } pageInfo { hasNextPage endCursor }
          }
        }"""
        cursor, seen, total = None, set(), None
        while True:
            body = json.dumps({"query": query, "variables": {"after": cursor}}).encode("utf-8")
            response = json.loads(self._get(url, 1_000_000, body))
            if response.get("errors"):
                raise ValueError("Galactapedia catalog query failed")
            page = response.get("data", {}).get("allArticle")
            if not isinstance(page, dict) or type(page.get("totalCount")) is not int:
                raise ValueError("Galactapedia returned no catalog metadata")
            if total is not None and total != page["totalCount"]:
                raise ValueError("Galactapedia catalog changed during pagination")
            total = page["totalCount"]
            for edge in page.get("edges", []):
                node = edge.get("node", {})
                ident, slug, title = node.get("id", ""), node.get("slug", ""), node.get("title")
                if (
                    not re.fullmatch(r"[a-zA-Z0-9]{10}", ident)
                    or not re.fullmatch(r"[a-zA-Z0-9-]+", slug)
                    or not isinstance(title, str)
                    or not title.strip()
                ):
                    raise ValueError("Galactapedia returned an invalid article identity")
                if ident not in seen:
                    seen.add(ident)
                    yield f"{GALACTAPEDIA}/article/{ident}-{slug}", title
            info = page.get("pageInfo", {})
            if not info.get("hasNextPage"):
                if len(seen) != total:
                    raise ValueError(f"incomplete Galactapedia catalog: {len(seen)}/{total}")
                break
            following = info.get("endCursor")
            if not following or following == cursor or len(seen) >= total:
                raise ValueError("Galactapedia cursor failed to advance")
            cursor = following


def robots_groups(robots: str) -> list[list[tuple[str, str]]]:
    groups: list[tuple[list[str], list[tuple[str, str]]]] = []
    names: list[str] = []
    rules: list[tuple[str, str]] = []
    for raw in robots.splitlines():
        key, _, value = raw.split("#", 1)[0].partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            if rules:
                groups.append((names, rules))
                names, rules = [], []
            names.append(value.casefold())
        elif key in {"allow", "disallow", "content-signal", "crawl-delay"} and names:
            rules.append((key, value))
    groups.append((names, rules))
    exact = [group for names, group in groups if "starcitizencomputerwiki" in names]
    return exact or [group for names, group in groups if "*" in names]


def robots_delay(robots: str, floor: int) -> float:
    delays = (
        float(value)
        for group in robots_groups(robots)
        for key, value in group
        if key == "crawl-delay" and re.fullmatch(r"\d+(?:\.\d+)?", value)
    )
    return max(float(floor), max(delays, default=0.0))


def allowed_by_robots(robots: str, path: str) -> bool:
    """Merge matching groups and apply longest-match precedence."""
    best = (0, True)
    for group in robots_groups(robots):
        for key, value in group:
            if key == "content-signal" and re.search(
                r"(?:^|,)\s*(?:search\s*=\s*no|use\s*=\s*immediate)(?:\s*,|\s*$)", value, re.I
            ):
                return False
            if key not in {"allow", "disallow"} or not value:
                continue
            pattern = re.escape(value.removesuffix("$")).replace(r"\*", ".*")
            if re.match(pattern + ("$" if value.endswith("$") else ""), path):
                best = max(best, (len(value.replace("*", "").removesuffix("$")), key == "allow"))
    return best[1]


def corpus_path() -> Path:
    setting = os.environ.get("SC_WIKI_DB")
    return (
        Path(setting).expanduser()
        if setting
        else Path.home() / ".starcitizen-wiki" / "wiki.sqlite3"
    )


def wiki_title(url: str) -> str:
    slug = unquote(urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])
    if url.startswith(GALACTAPEDIA + "/article/"):
        slug = re.sub(r"^[a-zA-Z0-9]{10}-", "", slug)
    else:
        slug = re.sub(r"^\d{5,}-", "", slug)
    return slug.replace("_", " ").replace("-", " ")


def indexable(url: str) -> bool:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.query or parsed.fragment:
        return False
    if parsed.netloc == "starcitizen.tools":
        return bool(parsed.path.strip("/")) and not unquote(parsed.path).startswith(
            ("/File:", "/Image:")
        )
    if parsed.netloc == "support.robertsspaceindustries.com":
        return parsed.path.startswith("/hc/en-us/articles/")
    return parsed.netloc == "robertsspaceindustries.com" and bool(
        re.match(
            r"^/(?:en/)?(?:galactapedia/article/|comm-link/(?:transmission|patch-notes|engineering)/)",
            parsed.path,
            re.I,
        )
    )


def sitemap_entries(source: Publisher, url: str, seen: set[str], db=None):
    if url in seen:
        return
    seen.add(url)
    cacheable = db is not None and hasattr(source, "conditional_get")
    if cacheable:
        prior = db.execute("SELECT etag,modified,sitemap FROM wiki_http_cache WHERE url=?", (url,)).fetchone()
        raw, etag, modified = source.conditional_get(url, SITEMAP_LIMIT, *(prior[:2] if prior else (None, None)))
        unchanged = raw is None
        if unchanged:
            if not prior or not prior[2]:
                raise ValueError("publisher returned 304 without a cached sitemap")
            raw = prior[2]
    else:
        raw = source.get(url, SITEMAP_LIMIT)
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)", raw, re.I):
        raise ValueError("sitemap cannot contain a DTD")
    feed = ElementTree.fromstring(raw)
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    if feed.tag.endswith("}sitemapindex"):
        for child in feed.findall("s:sitemap", ns):
            location = child.findtext("s:loc", namespaces=ns)
            if location and "/spectrum/" in location:
                continue
            if not location or not location.endswith((".xml", ".xml.gz")):
                raise ValueError("unsupported or incomplete child sitemap")
            yield from sitemap_entries(source, location, seen, db)
    elif feed.tag.endswith("}urlset"):
        for row in feed.findall("s:url", ns):
            loc = row.findtext("s:loc", namespaces=ns)
            if not loc:
                raise ValueError("sitemap entry has no source URL")
            if indexable(loc):
                if not source.allowed(loc):
                    raise PermissionError("publisher robots excludes part of the catalog")
                yield loc, row.findtext("s:lastmod", namespaces=ns)
    else:
        raise ValueError(f"unrecognized sitemap: {url}")
    if cacheable and not unchanged:
        with db:
            db.execute("""INSERT OR REPLACE INTO wiki_http_cache(url,etag,modified,sitemap)
                VALUES(?,?,?,?)""", (url, etag, modified, raw))


class ArticleElement(NamedTuple):
    tag: str
    in_article: bool
    hidden: bool
    in_table: bool
    in_paragraph: bool


class Article(HTMLParser):
    """Extract page identity, one short excerpt, and body vocabulary."""

    def __init__(self, body: str) -> None:
        super().__init__()
        self.stack: list[ArticleElement] = []
        self.paragraph: list[str] = []
        self.vocabulary: set[str] = set()
        self.excerpt = ""
        self.title = ""
        self.revision = re.search(r'"wgRevisionId"\s*:\s*(\d+)', body)
        self.updated: str | None = None
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            if a.get("property") == "og:title":
                self.title = a.get("content", "")
            if a.get("property") == "article:modified_time":
                self.updated = a.get("content")
        if tag in {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }:
            return
        parent = self.stack[-1] if self.stack else ArticleElement("", False, False, False, False)
        self.stack.append(ArticleElement(
            tag,
            parent.in_article or a.get("id") == "mw-content-text" or tag == "article",
            parent.hidden or "hidden" in a or tag in {"script", "style", "aside", "nav", "figure", "template"},
            parent.in_table or tag == "table",
            parent.in_paragraph or tag == "p",
        ))

    def handle_endtag(self, tag):
        i = next((i for i in range(len(self.stack) - 1, -1, -1) if self.stack[i].tag == tag), None)
        if i is None:
            return
        if tag == "p" and self.paragraph:
            phrase = " ".join("".join(self.paragraph).split())
            if len(phrase) >= 42 and not self.excerpt:
                self.excerpt = " ".join(phrase.split()[:25])
            self.paragraph.clear()
        del self.stack[i:]

    def handle_data(self, data):
        if not self.stack:
            return
        element = self.stack[-1]
        if not element.in_article or element.hidden:
            return
        self.vocabulary.update(re.findall(r"[a-zA-Z][a-zA-Z0-9-]{1,}|[0-9]+(?:\.[0-9]+){1,3}", data.casefold()))
        if not self.excerpt and element.in_paragraph and not element.in_table:
            self.paragraph.append(data)


def catalog_owner(url: str) -> str:
    if url.startswith(COMMUNITY + "/"):
        return "community"
    if url.startswith(SUPPORT + "/"):
        return "support"
    if url.startswith(GALACTAPEDIA + "/article/"):
        return "galactapedia"
    return "official"


def record_source_change(db, url, observed_at, reason):
    # Observation time is local evidence, never a fabricated publisher lastmod.
    db.execute("""INSERT INTO source_changes VALUES(?,?,?)
        ON CONFLICT(url) DO UPDATE SET observed_at=excluded.observed_at,reason=excluded.reason""",
        (url, observed_at, reason))


def apply_catalog(db, name, records, observed_at):
    """Apply one completely collected publisher snapshot inside a transaction."""
    if not records:
        raise ValueError("empty catalog cannot replace last good catalog")
    existing = {url: (modified, pageid, status, title) for url, modified, pageid, status, title in
        db.execute("""SELECT u.url,u.lastmod,u.pageid,u.status,t.title FROM wiki_urls u
            LEFT JOIN catalog_titles t ON t.url=u.url""") if catalog_owner(url) == name}
    has_page_queue = name == "community" and db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='refresh_pages'").fetchone()
    changed_titles = set()
    for url in existing.keys() - records.keys():
        if existing[url][2] != "removed":
            record_source_change(db, url, observed_at, "catalog_url_removed")
            changed_titles.add(url)
            db.execute("UPDATE wiki_urls SET status='removed' WHERE url=?", (url,))
    for url, value in records.items():
        old = existing.get(url)
        modified = None if name == "galactapedia" else value
        if old:
            if old[2] == "removed":
                record_source_change(db, url, observed_at, "catalog_url_restored")
            elif name == "galactapedia" and old[3] is not None and old[3] != value:
                record_source_change(db, url, observed_at, "catalog_title_changed")
            elif old[0] != modified:
                record_source_change(db, url, observed_at, "publisher_metadata_changed")
        status = "indexed" if old and old[1] is not None else ("pending" if name == "community" else "catalog")
        if not old or old[0] != modified or old[2] != status:
            db.execute("""INSERT INTO wiki_urls(url,lastmod,status) VALUES(?,?,?)
                ON CONFLICT(url) DO UPDATE SET lastmod=excluded.lastmod,status=excluded.status""", (url, modified, status))
        if not old or old[2] == "removed":
            changed_titles.add(url)
        if name == "galactapedia" and (not old or old[3] != value):
            changed_titles.add(url)
            db.execute("INSERT OR REPLACE INTO catalog_titles VALUES(?,?)", (url, value))
        if name == "community" and (old is None or old[0] != modified or old[2] == "removed"):
            # Do not require refresh.py's auxiliary schema for the standalone CLI.
            if has_page_queue:
                db.execute("""INSERT INTO refresh_pages(url,next_attempt) VALUES(?,0)
                    ON CONFLICT(url) DO UPDATE SET next_attempt=0""", (url,))
    if not db.execute("SELECT 1 FROM catalog_search LIMIT 1").fetchone():
        rebuild_catalog(db)
    elif changed_titles:
        changed = json.dumps(sorted(changed_titles))
        db.execute("DELETE FROM catalog_search WHERE url IN (SELECT value FROM json_each(?))", (changed,))
        db.executemany("INSERT INTO catalog_search VALUES(?,?,?)", (
            (url, title or wiki_title(url), publisher_name(url)) for url, title in
            db.execute("""SELECT u.url,coalesce(p.title,t.title) FROM wiki_urls u
                LEFT JOIN pages p ON p.pageid=u.pageid LEFT JOIN catalog_titles t ON t.url=u.url
                WHERE u.url IN (SELECT value FROM json_each(?)) AND u.status!='removed'""", (changed,))
        ))


def discover(db: sqlite3.Connection, publisher: Publisher) -> int:
    discovered: dict[str, str | None] = {}
    for sitemap in SITEMAPS:
        discovered.update(sitemap_entries(publisher, sitemap, set(), db))
    lore = dict(publisher.galactapedia())
    with db:
        observed_at = datetime.now(UTC).isoformat(timespec="microseconds")
        for name in ("community", "support", "official"):
            apply_catalog(db, name, {url: value for url, value in discovered.items()
                                    if catalog_owner(url) == name}, observed_at)
        apply_catalog(db, "galactapedia", lore, observed_at)
    discovered.update(lore)
    return len(discovered)


def publisher_name(url: str) -> str:
    return (
        "Star Citizen Wiki"
        if urlsplit(url).netloc == "starcitizen.tools"
        else "CIG / Roberts Space Industries"
    )


def rebuild_catalog(db: sqlite3.Connection) -> None:
    db.execute("DELETE FROM catalog_search")
    db.executemany(
        "INSERT INTO catalog_search VALUES(?,?,?)",
        (
            (url, title or wiki_title(url), publisher_name(url))
            for url, title in db.execute(
                """SELECT u.url,coalesce(p.title,t.title) FROM wiki_urls u
            LEFT JOIN pages p ON p.pageid=u.pageid
            LEFT JOIN catalog_titles t ON t.url=u.url WHERE u.status!='removed'"""
            )
        ),
    )


def save_draft(
    root: Path, slug: str, title: str, finding: str, source: str, game_build: str | None
) -> Path:
    if (
        not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug)
        or len(slug) > 64
        or not title.strip()
        or len(title) > 120
        or "\n" in title
        or "\r" in title
        or not finding.strip()
        or len(finding) > 4000
        or not re.fullmatch(r"https://[^\s<>()]+", source)
        or not urlsplit(source).hostname
    ):
        raise ValueError("a slug, one-line title, concise finding and HTTPS source are required")
    if game_build and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", game_build):
        raise ValueError("use the exact game build, or omit it")
    date = datetime.now(UTC).date().isoformat()
    scope = f"Game build: {game_build}\n\n" if game_build else ""
    draft = root / "drafts" / f"{slug}.md"
    draft.parent.mkdir(parents=True, exist_ok=True)
    with draft.open("x", encoding="utf-8") as stream:
        stream.write(
            f"# {title.strip()}\n\nDraft {date}.\n\n## Finding\n\n{scope}"
            f"{finding.strip()}\n\n[Source]({source})\n\n"
            f"## Revision history\n\n- {date}. Drafted.\n"
        )
    return draft


def publish_note(root: Path, file: Path) -> Path:
    if (
        not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*\.md", file.name)
        or file.resolve().parent != (root / "drafts").resolve()
    ):
        raise ValueError("select one Markdown file from this wiki's drafts folder")
    with file.open(encoding="utf-8") as stream:
        text = stream.read(12_001)
    lines = text.splitlines()
    if (
        len(text) > 12_000
        or len(lines) < 4
        or not lines[0].startswith("# ")
        or not re.fullmatch(r"Reviewed \d{4}-\d{2}-\d{2}\.", lines[2])
        or "## Revision history" not in lines
        or not re.search(r"\]\(https://[^)\s]+\)", text)
    ):
        raise ValueError(
            "review the claim, record 'Reviewed YYYY-MM-DD.' "
            "and keep the source and revision history"
        )
    path = root / "notes" / file.name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(text)
    return path


def crawl(
    db: sqlite3.Connection, publisher: Publisher, limit: int, urls: list[str], query: str = "",
    *, observed_at: str | None = None,
) -> int:
    if query:
        terms = re.findall(r"[^\W_]+", query)
        if not terms:
            raise ValueError("enter at least one word to search the catalog")
        match = " AND ".join(f'"{term}"' for term in terms)
        targets = [
            row[0]
            for row in db.execute(
                """SELECT u.url FROM catalog_search c
            JOIN wiki_urls u ON u.url=c.url WHERE catalog_search MATCH ?
            AND u.status='pending' AND u.url LIKE 'https://starcitizen.tools/%'
            ORDER BY rank, u.url LIMIT ?""",
                (match, limit),
            )
        ]
    else:
        targets = urls or [
            row[0]
            for row in db.execute(
                """SELECT url FROM wiki_urls WHERE status='pending'
            AND url LIKE 'https://starcitizen.tools/%'
            ORDER BY lastmod DESC, url LIMIT ?""",
                (limit,),
            )
        ]
    count = 0
    for url in targets[:limit]:
        if not indexable(url):
            raise ValueError(f"unsupported corpus page: {url}")
        if publisher_name(url) != "Star Citizen Wiki":
            raise ValueError(
                "RSI references are title-and-link entries. "
                "Use the cited source or a reviewed local note."
            )
        checked = observed_at or datetime.now(UTC).isoformat(timespec="microseconds")
        try:
            if hasattr(publisher, "conditional_get"):
                prior = db.execute("""SELECT h.etag,h.modified,h.digest FROM wiki_http_cache h
                    JOIN wiki_urls u ON u.url=h.url WHERE h.url=? AND u.status='indexed'
                    AND u.pageid IS NOT NULL""", (url,)).fetchone()
                raw, etag, modified = publisher.conditional_get(url, 4_000_000, *(prior[:2] if prior else (None, None)))
                if raw is None:
                    if prior is None:
                        raise ValueError("publisher returned 304 without a cached article")
                    continue
                digest = hashlib.sha256(raw.encode()).hexdigest()
                if prior and digest == prior[2]:
                    with db:
                        db.execute("UPDATE wiki_http_cache SET etag=?,modified=? WHERE url=?", (etag, modified, url))
                    continue
            else:
                raw = publisher.get(url)
            article = Article(raw)
        except HTTPError as exc:
            if exc.code not in {404, 410}:
                raise ValueError(f"HTTP {exc.code} at {url}") from exc
            with db:
                prior = db.execute("SELECT status FROM wiki_urls WHERE url=?", (url,)).fetchone()
                if prior and prior[0] != "removed":
                    record_source_change(db, url, checked, "page_removed")
                db.execute(
                    "DELETE FROM pages WHERE pageid=(SELECT pageid FROM page_sources WHERE url=?)",
                    (url,),
                )
                db.execute("DELETE FROM page_vocabulary WHERE rowid=(SELECT pageid FROM page_sources WHERE url=?)", (url,))
                db.execute("DELETE FROM page_sources WHERE url=?", (url,))
                db.execute(
                    "UPDATE wiki_urls SET status='removed',checked_at=?,pageid=NULL WHERE url=?", (checked, url)
                )
                db.execute("DELETE FROM catalog_search WHERE url=?", (url,))
            continue
        stamp = article.updated
        if not article.excerpt and not article.vocabulary:
            raise ValueError("publisher returned no usable reader-visible article text; last good page retained")
        revision = int(article.revision.group(1)) if article.revision else None
        title = article.title or wiki_title(url)
        origin = publisher_name(url)
        terms = " ".join(sorted(article.vocabulary))
        with db:
            if hasattr(publisher, "conditional_get"):
                db.execute("""INSERT OR REPLACE INTO wiki_http_cache(url,etag,modified,digest)
                    VALUES(?,?,?,?)""", (url, etag, modified, digest))
            row = db.execute("""SELECT p.pageid,p.title,p.revision,p.timestamp,p.extract,v.terms
                FROM page_sources s JOIN pages p ON p.pageid=s.pageid
                LEFT JOIN page_vocabulary v ON v.rowid=p.pageid WHERE s.url=?""", (url,)).fetchone()
            if row:
                pageid = row[0]
                # Even an unchanged title/sitemap can contain a new MediaWiki
                # revision. Record the observed signal without editing notes.
                observed = [
                    (row[2], revision, "page_revision_changed"),
                    (row[1], title, "page_title_changed"),
                    (row[3], stamp, "page_metadata_changed"),
                    (row[4], article.excerpt, "page_excerpt_changed"),
                ]
                if row[5] is not None:
                    observed.append((row[5], terms, "page_vocabulary_changed"))
                reason = next((label for before, after, label in observed if before != after), None)
                if reason:
                    record_source_change(db, url, checked, reason)
                db.execute(
                    "UPDATE pages SET title=?,revision=?,timestamp=?,extract=? WHERE pageid=?",
                    (title, revision, stamp, article.excerpt, pageid),
                )
                db.execute(
                    "UPDATE page_sources SET retrieved_at=? WHERE pageid=?", (checked, pageid)
                )
            else:
                pageid = db.execute(
                    "INSERT INTO pages(title,revision,timestamp,extract) VALUES (?,?,?,?)",
                    (title, revision, stamp, article.excerpt),
                ).lastrowid
                db.execute(
                    "INSERT INTO page_sources VALUES(?,?,?,?)", (pageid, url, origin, checked)
                )
            db.execute("DELETE FROM page_vocabulary WHERE rowid=?", (pageid,))
            if terms:
                db.execute("INSERT INTO page_vocabulary(rowid,terms) VALUES(?,?)", (pageid, terms))
            db.execute(
                """INSERT INTO wiki_urls(url,pageid,checked_at,status) VALUES(?,?,?,'indexed')
                ON CONFLICT(url) DO UPDATE SET pageid=excluded.pageid,
                    checked_at=excluded.checked_at,status='indexed'""",
                (url, pageid, checked),
            )
            db.execute("DELETE FROM catalog_search WHERE url=?", (url,))
            db.execute("INSERT INTO catalog_search VALUES(?,?,?)", (url, title, origin))
        count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=corpus_path())
    commands = parser.add_subparsers(dest="mode", required=True)
    commands.add_parser("discover", help="refresh public sitemap catalog, without reading articles")
    sampling = commands.add_parser("sync", help="read an incremental batch of reference pages")
    sampling.add_argument("--limit", type=int, default=30)
    targets = sampling.add_mutually_exclusive_group()
    targets.add_argument("--url", action="append", default=[])
    targets.add_argument(
        "--query", default="", help="index pending community references with matching titles"
    )
    commands.add_parser("status", help="show per-publisher catalog and sample coverage")
    commands.add_parser("links", help="emit local TSV for piping to ripgrep")
    draft = commands.add_parser("draft", help="record a structured candidate for local review")
    draft.add_argument("slug")
    draft.add_argument("--title", required=True)
    draft.add_argument("--finding", required=True)
    draft.add_argument("--source", required=True)
    draft.add_argument("--game-build")
    accept = commands.add_parser("publish-note", help="include a reviewed local note in lookup")
    accept.add_argument("file", type=Path)
    args = parser.parse_args()
    if args.mode == "sync" and args.limit < 1:
        parser.error("--limit must be positive")
    args.db.parent.mkdir(parents=True, exist_ok=True)
    try:
        if args.mode == "draft":
            print(
                save_draft(
                    args.db.parent,
                    args.slug,
                    args.title,
                    args.finding,
                    args.source,
                    args.game_build,
                )
            )
            return 0
        if args.mode == "publish-note":
            print(publish_note(args.db.parent, args.file))
            return 0
        with closing(sqlite3.connect(args.db)) as db:
            db.executescript(SCHEMA)
            if args.mode == "discover":
                print(f"Discovered {discover(db, Publisher()):,} eligible reference URLs.")
            elif args.mode == "sync":
                count = crawl(db, Publisher(), args.limit, args.url, args.query)
                print(f"Indexed {count:,} reference pages.")
            elif args.mode == "status":
                for (
                    host,
                    status,
                    count,
                ) in db.execute("""SELECT substr(url,9,instr(substr(url,9),'/')-1),status,count(*)
                        FROM wiki_urls GROUP BY 1,2 ORDER BY 1,2"""):
                    print(f"{host}\t{status}\t{count}")
            else:
                sys.stdout.reconfigure(encoding="utf-8")
                for (
                    url,
                    title,
                    excerpt,
                ) in db.execute("""SELECT u.url,coalesce(p.title,t.title),p.extract FROM wiki_urls u
                        LEFT JOIN pages p ON p.pageid=u.pageid AND u.status='indexed'
                        LEFT JOIN catalog_titles t ON t.url=u.url
                        WHERE u.status!='removed' ORDER BY u.url"""):
                    print("\t".join((title or wiki_title(url), url, excerpt or "")))
    except (OSError, URLError, ValueError, sqlite3.Error, ElementTree.ParseError) as error:
        print(f"Wiki batch stopped: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
