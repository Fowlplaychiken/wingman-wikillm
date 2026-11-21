"""Opt-in public HTTPS wiki references; no credentials, providers or native feeds.

Frontiers have no depth limit. Each cycle is bounded by batch_size; discovery,
revisits and host cooldowns persist in the user's database. The caller owns the
shared refresh lease. Tests can supply _transport(url, headers) without network.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
import gzip
import hashlib
from html.parser import HTMLParser
import http.client
import ipaddress
import io
import json
from pathlib import Path
import re
import socket
import sqlite3
import time
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

from . import corpus
from .reference import _query_terms

SCHEMA = """
CREATE TABLE IF NOT EXISTS web_roots(root TEXT PRIMARY KEY,last_discovery REAL,
 last_success REAL,last_error TEXT,prefer_existing INTEGER NOT NULL DEFAULT 1,
 discovery_retry REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS web_hosts(host TEXT PRIMARY KEY,last_request REAL NOT NULL DEFAULT 0,
 blocked_until REAL NOT NULL DEFAULT 0,robots TEXT,robots_checked REAL);
CREATE TABLE IF NOT EXISTS web_pages(root TEXT NOT NULL,url TEXT NOT NULL,title TEXT,
 status TEXT NOT NULL DEFAULT 'pending',license TEXT,storage_mode TEXT,
 retrieved_at TEXT,etag TEXT,last_modified TEXT,content_sha256 TEXT,
 next_attempt REAL NOT NULL DEFAULT 0,last_error TEXT,redirect_url TEXT,PRIMARY KEY(root,url));
CREATE INDEX IF NOT EXISTS web_pages_due ON web_pages(root,next_attempt,url);
CREATE VIRTUAL TABLE IF NOT EXISTS web_passages USING fts5(
 root UNINDEXED,url UNINDEXED,ordinal UNINDEXED,title,text,tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS source_changes(url TEXT PRIMARY KEY,observed_at TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS web_cursor(id INTEGER PRIMARY KEY CHECK(id=1),last_root TEXT);
CREATE TABLE IF NOT EXISTS web_sitemaps(root TEXT NOT NULL,url TEXT NOT NULL,
 next_attempt REAL NOT NULL DEFAULT 0,last_error TEXT,PRIMARY KEY(root,url));
CREATE INDEX IF NOT EXISTS web_sitemaps_due ON web_sitemaps(root,next_attempt,url);
CREATE TABLE IF NOT EXISTS web_sitemap_entries(root TEXT NOT NULL,sitemap TEXT NOT NULL,
 url TEXT NOT NULL,stamp TEXT,PRIMARY KEY(root,sitemap,url));
CREATE INDEX IF NOT EXISTS web_sitemap_article ON web_sitemap_entries(root,url);
CREATE TABLE IF NOT EXISTS web_apis(root TEXT PRIMARY KEY,url TEXT NOT NULL,cursor TEXT,
 next_attempt REAL NOT NULL DEFAULT 0,last_success REAL,last_error TEXT);
CREATE TABLE IF NOT EXISTS web_api_cursors(root TEXT NOT NULL,cursor TEXT NOT NULL,PRIMARY KEY(root,cursor));
"""
_NATIVE = ("starcitizen.tools", "robertsspaceindustries.com")
_BLOCKED_NAMESPACE = re.compile(r"(?:^|/)(?:special|user(?:_talk)?|talk|file|image|mediawiki|template)(?::|%3a)", re.I)
_ASSET = re.compile(r"\.(?:jpg|jpeg|png|gif|webp|svg|ico|mp[34]|wav|ogg|webm|pdf|zip|gz|css|js|json|xml|woff2?|ttf)(?:$|/)", re.I)
_ADMIN = re.compile(r"(?:^|/)(?:admin|wp-admin|wp-login\.php|login|logout|api(?:\.php)?|graphql|xmlrpc\.php|edit|history|search)(?:/|$)", re.I)
_LICENSE = re.compile(r"^https?://(?:www\.)?creativecommons\.org/(?:licenses/(by|by-sa)/(\d\.\d)|publicdomain/zero/(1\.0))/?$", re.I)
_ALLPAGES = {"action": "query", "format": "json", "list": "allpages", "apnamespace": "0",
             "aplimit": "50", "apfilterredir": "nonredirects"}
_NAMESPACE_INFO = {"meta": "siteinfo", "siprop": "namespaces"}


def _url(value):
    if not isinstance(value, str) or not value or len(value) > 2000 or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError("Use a public HTTPS wiki URL without spaces")
    p = urlsplit(value)
    try:
        port = p.port
        host = (p.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Invalid public wiki host") from exc
    if p.scheme.lower() != "https" or not host or p.username or p.password or port not in (None, 443):
        raise ValueError("Wiki URLs require HTTPS, no credentials and the standard port")
    if host == "localhost" or "." not in host or host.endswith((".local", ".internal", ".lan", ".localhost")):
        raise ValueError("Private wiki hosts are not supported")
    try:
        if not ipaddress.ip_address(host).is_global:
            raise ValueError("Private wiki hosts are not supported")
    except ValueError:
        # A numeric-looking address must never be treated as a DNS hostname.
        if re.fullmatch(r"[\d.:]+", host):
            raise ValueError("Use a public DNS wiki hostname") from None
    if any(host == native or host.endswith("." + native) for native in _NATIVE):
        raise ValueError("Use the dedicated Star Citizen source settings for this publisher")
    path = p.path or "/"
    decoded = unquote(path)
    if "\\" in decoded or any(part in (".", "..") for part in decoded.split("/")) or re.search(r"%2f|%5c|%25", path, re.I):
        raise ValueError("Ambiguous wiki path")
    if any(ord(c) < 32 for c in decoded):
        raise ValueError("Invalid wiki path")
    query = ""
    if p.query:
        try:
            fields = parse_qsl(p.query, keep_blank_values=True, strict_parsing=True)
        except ValueError as exc:
            raise ValueError("Only a public MediaWiki title query is supported") from exc
        if len(fields) != 1 or fields[0][0] != "title" or not path.lower().endswith("/index.php"):
            raise ValueError("Only a public MediaWiki title query is supported")
        title = fields[0][1].strip().replace(" ", "_")
        if (not title or len(title) > 512 or any(ord(c) < 32 for c in title)
                or "\\" in title or re.search(r"%[0-9a-f]{2}", title, re.I)
                or any(part in (".", "..") for part in title.split("/"))):
            raise ValueError("Invalid MediaWiki article title")
        if _BLOCKED_NAMESPACE.search("/" + title) or _ADMIN.search("/" + title) or _ASSET.search(title):
            raise ValueError("Choose a public MediaWiki article title")
        query = urlencode({"title": title}, quote_via=quote, safe=":/")
    return urlunsplit(("https", host, quote(path, safe="/:@-._~!$&'()*+,;=%"), query, ""))


def parse_roots(text) -> tuple[str, ...]:
    """One public wiki per line. Prefix a line with # to pause it."""
    if not isinstance(text, str):
        raise ValueError("Supply a newline-separated wiki URL list")
    roots, seen = [], set()
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        url = _url(line.strip())
        if (_ADMIN.search(unquote(urlsplit(url).path)) or _ASSET.search(urlsplit(url).path)
                or _BLOCKED_NAMESPACE.search(unquote(urlsplit(url).path))):
            raise ValueError("Choose a wiki base or article page")
        if url not in seen:
            roots.append(url)
            seen.add(url)
    return tuple(roots)


def _roots(values):
    if isinstance(values, str):
        return parse_roots(values)
    return parse_roots("\n".join(values))


def _scope(root):
    path = urlsplit(root).path
    return path if path.endswith("/") else path.rsplit("/", 1)[0] + "/"


def _article(root, link):
    try:
        url = _url(urljoin(root, link))
    except ValueError:
        return None
    p = urlsplit(url)
    origin = urlsplit(root)
    scoped = p.path.startswith(_scope(root))
    if _scope(root).endswith("/wiki/") and p.query:
        scoped |= p.path in (_scope(root)[:-5] + "index.php", _scope(root)[:-5] + "w/index.php")
    if origin.query and _scope(root).endswith("/w/"):
        scoped |= p.path.startswith(_scope(root)[:-2] + "wiki/")
    if p.netloc != origin.netloc or not scoped:
        return None
    path = unquote(p.path)
    if _ASSET.search(path) or _ADMIN.search(path) or _BLOCKED_NAMESPACE.search(path):
        return None
    return url


def in_scope(url, root):
    """Whether an absolute article URL belongs to this root's exact scope."""
    try:
        root = _url(root)
        canonical = _url(url)
    except ValueError:
        return False
    return _article(root, canonical) == canonical


def _network_url(url):
    """Article queries or bounded allpages queries with optional namespace metadata."""
    try:
        return _url(url)
    except ValueError:
        parsed = urlsplit(url)
        base = _url(urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")))
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
        fields = dict(pairs)
        namespace = fields.get("apnamespace", "")
        metadata = {k: fields[k] for k in _NAMESPACE_INFO if k in fields}
        if (len(url) > 6000 or not parsed.path.endswith("/api.php") or len(fields) != len(pairs)
                or any(fields.get(key) != value for key, value in {**_ALLPAGES, "apnamespace": namespace}.items())
                or not re.fullmatch(r"0|[1-9][0-9]{0,9}", namespace) or int(namespace) > 2**31 - 1
                or metadata not in ({}, _NAMESPACE_INFO)
                or set(fields) - _ALLPAGES.keys() - _NAMESPACE_INFO.keys() - {"apcontinue", "continue"}
                or any(len(fields.get(key, "")) > 1024 for key in ("apcontinue", "continue"))):
            raise ValueError("Unsupported wiki API request") from None
        return base + "?" + urlencode(fields)


def _api_endpoint(root, edit_uri):
    """Accept only a publisher-declared MediaWiki EditURI in this wiki family."""
    try:
        parsed = urlsplit(urljoin(root, edit_uri))
        endpoint = _url(urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")))
        fields = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None
    family = _scope(root)
    if family.endswith("/wiki/"):
        family = family[:-5]
    elif family.endswith("/w/"):
        family = family[:-2]
    if (urlsplit(endpoint).netloc != urlsplit(root).netloc or not parsed.path.endswith("/api.php")
            or not parsed.path.startswith(family) or fields not in ([], [("action", "rsd")])
            or _ADMIN.search(parsed.path.rsplit("/", 1)[0])):
        return None
    return endpoint


def _register_api(db, root, edit_uri):
    endpoint = _api_endpoint(root, edit_uri)
    if endpoint:
        previous = db.execute("SELECT url FROM web_apis WHERE root=?", (root,)).fetchone()
        if previous and previous[0] != endpoint:
            db.execute("DELETE FROM web_api_cursors WHERE root=?", (root,))
            db.execute("DELETE FROM web_apis WHERE root=?", (root,))
        db.execute("INSERT OR IGNORE INTO web_apis(root,url) VALUES(?,?)", (root, endpoint))


def _title_url(root, title, endpoint=None):
    origin = urlsplit(root)
    if origin.query or (endpoint and origin.path.endswith("/")):
        path = origin.path if origin.query else urlsplit(urljoin(endpoint, "index.php")).path
        candidate = urlunsplit((origin.scheme, origin.netloc, path,
                               urlencode({"title": title.replace(" ", "_")}, quote_via=quote, safe=":/"), ""))
    else:
        # A custom namespace prefix (e.g. Item:) is part of the path, not a URI scheme.
        candidate = urljoin(root, "./" + quote(title.replace(" ", "_"), safe=":/"))
    return _article(root, candidate)


def _namespace_ids(metadata):
    if not isinstance(metadata, dict) or not metadata:
        return None
    content = set()
    for key, item in metadata.items():
        if not isinstance(item, dict):
            return None
        ident = item.get("id")
        if type(ident) is not int or str(ident) != key or abs(ident) > 2**31 - 1:
            return None
        # MediaWiki JSON format 1 encodes a true flag as an empty string.
        if ident >= 0 and (item.get("content") is True or item.get("content") == ""):
            content.add(ident)
    return sorted(content | {0}) if content else None


def _api_continuation(value):
    if value is not None and (not isinstance(value, dict) or "apcontinue" not in value
            or set(value) - {"continue", "apcontinue"}
            or any(not isinstance(v, str) or not v or len(v) > 1024 for v in value.values())):
        raise ValueError("Invalid wiki allpages continuation")
    return value


def _page_key(namespace, token):
    # Keep pre-upgrade main-namespace cycle history usable.
    return (f"{namespace}:" if namespace else "") + (json.dumps(token, sort_keys=True) if token else "")


def _enumerate_api(db, contact, root, rules):
    """One metadata-only allpages batch; publisher continuation persists across cycles.

    Protocol: https://www.mediawiki.org/wiki/API:Allpages and API:Continue.
    Article bodies still use the normal per-page robots/licence/storage gates.
    """
    row = db.execute("SELECT url,cursor,next_attempt FROM web_apis WHERE root=?", (root,)).fetchone()
    if not row or row[2] > contact.clock():
        return {"state": "not_due" if row else "not_declared"}
    endpoint, cursor, _ = row
    if _api_endpoint(root, endpoint) != endpoint:
        raise ValueError("Stored wiki API endpoint is outside this wiki family")
    saved = json.loads(cursor) if cursor else {}
    discover = "remaining" not in saved
    remaining = [0] if discover else saved["remaining"]
    reason = None if discover else saved["reason"]
    continuation = _api_continuation((saved if discover else saved["after"]) or None)
    namespace = remaining[0]
    query = {**_ALLPAGES, "apnamespace": str(namespace), **(continuation or {})}
    request = endpoint + "?" + urlencode(query)
    _network_url(request)
    if not _allowed(rules, request):
        reason = "robots_disallowed"
        # A host-wide refusal, or the last blocked namespace, can resume from
        # the same token if the publisher later permits it.
        try_next = len(remaining) > 1 and _allowed(rules, endpoint)
        next_cursor = json.dumps({"remaining": remaining[1:], "after": None, "reason": reason}) if try_next else cursor
        with db:
            db.execute("UPDATE web_apis SET last_error=?,cursor=?,next_attempt=? WHERE root=?",
                       (reason, next_cursor, 0 if try_next else contact.clock()+86_400, root))
        return {"state": "blocked", "reason": reason, "namespace": namespace, "complete": False}
    if discover:
        combined = endpoint + "?" + urlencode({**query, **_NAMESPACE_INFO})
        if _allowed(rules, combined):
            request = _network_url(combined)
        else:
            reason = "namespace_metadata_robots_disallowed"
    response = contact.get(request, rules=rules)
    if response.status != 200:
        raise ValueError("Declared wiki API unavailable")
    data = json.loads(_payload(response, 1_000_000).decode("utf-8-sig"))
    error = data.get("error")
    if isinstance(error, dict) and error.get("code") in ("ratelimited", "maxlag"):
        until = corpus.retry_after(response.headers.get("retry-after", "900"), contact.clock())
        with db:
            db.execute("UPDATE web_hosts SET blocked_until=max(blocked_until,?) WHERE host=?",
                       (until, urlsplit(endpoint).netloc))
        raise _Cooldown(until)
    pages = data.get("query", {}).get("allpages")
    if data.get("error") or not isinstance(pages, list) or len(pages) > 50:
        raise ValueError("Incomplete wiki allpages batch")
    if discover and reason is None:
        declared = _namespace_ids(data["query"].get("namespaces"))
        if declared is None:
            reason = "namespace_metadata_unavailable"
        else:
            remaining = declared
    targets = []
    for page in pages:
        if (not isinstance(page, dict) or type(page.get("ns")) is not int or page["ns"] != namespace
                or type(page.get("pageid")) is not int or page["pageid"] <= 0
                or not isinstance(page.get("title"), str) or not page["title"].strip()):
            raise ValueError("Invalid wiki allpages article identity")
        target = _title_url(root, page["title"], endpoint)
        if target:
            targets.append(target)
    following = _api_continuation(data.get("continue"))
    current_key = _page_key(namespace, continuation)
    if following and (following == continuation or db.execute(
            "SELECT 1 FROM web_api_cursors WHERE root=? AND cursor=?", (root, _page_key(namespace, following))).fetchone()):
        raise ValueError("Wiki allpages continuation failed to advance")
    if following is None:
        remaining = remaining[1:]
    next_cursor = json.dumps({"remaining": remaining, "after": following, "reason": reason}) if remaining else None
    with db:
        for target in targets:
            _queue(db, root, target)
        db.execute("INSERT OR IGNORE INTO web_api_cursors VALUES(?,?)", (root, current_key))
        db.execute("UPDATE web_apis SET cursor=?,next_attempt=?,last_success=?,last_error=? WHERE root=?",
            (next_cursor, 0 if next_cursor else contact.clock()+86_400, contact.clock(), reason, root))
        if next_cursor is None:
            db.execute("DELETE FROM web_api_cursors WHERE root=?", (root,))
    return {"state": "advanced", "namespace": namespace, "discovered": len(targets),
            "complete": next_cursor is None and reason is None, "reason": reason}


@dataclass
class Response:
    status: int
    headers: dict
    body: bytes


def _payload(response, limit):
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if encoding == "gzip" or response.body.startswith(b"\x1f\x8b"):
        with gzip.GzipFile(fileobj=io.BytesIO(response.body)) as stream:
            body = stream.read(limit + 1)
    elif encoding in ("", "identity"):
        body = response.body
    else:
        raise ValueError("Unsupported wiki content encoding")
    if len(body) > limit:
        raise ValueError("Wiki content exceeds bounded decoded size")
    return body


def _transport(url, headers):
    """Pin the TLS connection to a validated public DNS result (no proxy/redirect)."""
    parsed = urlsplit(_network_url(url))
    addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)}
    if not addresses or any(not ipaddress.ip_address(value).is_global for value in addresses):
        raise ValueError("Wiki DNS resolves to a non-public address")
    address = sorted(addresses)[0]
    connection = http.client.HTTPSConnection(parsed.hostname, timeout=15)
    connection._create_connection = lambda target, timeout, source_address: socket.create_connection(
        (address, 443), timeout, source_address)
    try:
        connection.request("GET", (parsed.path or "/") + ("?" + parsed.query if parsed.query else ""), headers=headers)
        response = connection.getresponse()
        limit = corpus.SITEMAP_LIMIT if parsed.path.lower().endswith((".xml", ".xml.gz")) else 4_000_000
        body = response.read(limit + 1)
        if len(body) > limit:
            raise ValueError("Wiki response exceeds the bounded page size")
        return Response(response.status, dict(response.getheaders()), body)
    finally:
        connection.close()


class _Cancelled(Exception):
    pass


class _Cooldown(Exception):
    def __init__(self, until):
        self.until = until


class _Denied(Exception):
    def __init__(self, reason):
        self.reason = reason


class _Contact:
    def __init__(self, db, transport, clock, stop_event):
        self.db, self.transport, self.clock, self.stop = db, transport, clock, stop_event

    def cancelled(self):
        if self.stop is not None and self.stop.is_set():
            raise _Cancelled()

    def get(self, url, *, rules="", headers=None):
        self.cancelled()
        host = urlsplit(url).netloc
        now = self.clock()
        # Persist the next turn before waiting; concurrent callers cannot cut
        # ahead of another profile's host pacing even outside the shared lease.
        self.db.execute("BEGIN IMMEDIATE")
        row = self.db.execute("SELECT last_request,blocked_until FROM web_hosts WHERE host=?", (host,)).fetchone()
        last, blocked = row if row else (0, 0)
        if blocked > now:
            self.db.rollback()
            raise _Cooldown(blocked)
        turn = max(now, last + corpus.robots_delay(rules, 2))
        self.db.execute("""INSERT INTO web_hosts(host,last_request) VALUES(?,?)
            ON CONFLICT(host) DO UPDATE SET last_request=excluded.last_request""", (host, turn))
        self.db.commit()
        wait = max(0, turn - self.clock())
        if wait:
            if self.stop is None:
                time.sleep(wait)
            elif self.stop.wait(wait):
                raise _Cancelled()
        self.cancelled()
        result = self.transport(url, {"User-Agent": corpus.AGENT, "Accept-Encoding": "identity", **(headers or {})})
        self.cancelled()
        response = Response(result.status, {k.lower(): v for k, v in result.headers.items()}, result.body)
        if response.status == 429 or response.status == 503:
            until = corpus.retry_after(response.headers.get("retry-after", "60" if response.status == 503 else "900"), self.clock())
            with self.db:
                self.db.execute("UPDATE web_hosts SET blocked_until=max(blocked_until,?) WHERE host=?", (until, host))
            raise _Cooldown(until)
        return response

    def robots(self, root):
        host = urlsplit(root).netloc
        row = self.db.execute("SELECT robots,robots_checked FROM web_hosts WHERE host=?", (host,)).fetchone()
        if row and row[0] is not None and self.clock() - (row[1] or 0) < 86_400:
            return row[0]
        result = self.follow("https://" + host + "/robots.txt")
        # RFC 9309 2.3.1.3: an absent robots file is not a robots refusal.
        # Authentication/refusal responses and server/network errors stay closed.
        if result.status in (404, 410):
            rules = ""
        elif result.status == 200:
            rules = _payload(result, 256_000).decode("utf-8-sig")
        else:
            raise ValueError("Public wiki robots unavailable")
        with self.db:
            self.db.execute("UPDATE web_hosts SET robots=?,robots_checked=? WHERE host=?", (rules, self.clock(), host))
        return rules

    def follow(self, url, *, rules="", headers=None):
        host, seen = urlsplit(url).netloc, set()
        for redirects in range(6):
            seen.add(url)
            result = self.get(url, rules=rules, headers=headers)
            if result.status not in (301, 302, 303, 307, 308):
                break
            location = result.headers.get("location")
            if redirects == 5 or not location:
                raise ValueError("Public wiki redirect limit or missing target")
            target = _url(urljoin(url, location))
            if (urlsplit(target).netloc != host or target in seen or not _allowed(rules, target)
                    or _ADMIN.search(unquote(urlsplit(target).path))):
                raise ValueError("Public wiki redirect leaves origin, cycles or is disallowed")
            url = target
            headers = None  # A validator belongs to the originally requested URI.
        return result


def _allowed(rules, url):
    denied_input = any(key == "content-signal" and re.search(r"\bai-input\s*=\s*no\b", value, re.I)
                       for group in corpus.robots_groups(rules) for key, value in group)
    parsed = urlsplit(url)
    path = parsed.path + ("?" + parsed.query if parsed.query else "")
    return corpus.allowed_by_robots(rules, path) and not denied_input


class _Page(HTMLParser):
    def __init__(self, body):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.blocks, self.current, self.links = [], [], []
        self._regions = {i: [] for i in range(5)}
        self._prose_regions = {i: [] for i in range(5)}
        self._paragraph, self._paragraph_region = [], 0
        self._current_region = 0
        self.title, self.canonical, self.license, self.denied = "", None, None, False
        self.edit_uri = None
        self.no_full_text = False
        self.complete = False
        self.incomplete = False
        self.seen_html = False
        self.follow = True
        self.feed(body)
        self._flush()
        self._flush_paragraph()
        region = max((i for i, blocks in self._regions.items() if blocks), default=0)
        self.blocks = self._regions[region]
        self.prose = self._prose_regions[region]

    def _flush(self):
        text = " ".join("".join(self.current).split())
        if text:
            self._regions[self._current_region].append(text)
        self.current.clear()

    def _flush_paragraph(self):
        text = " ".join("".join(self._paragraph).split())
        if text:
            self._prose_regions[self._paragraph_region].append(text)
        self._paragraph.clear()

    def short_excerpt(self):
        # Menus can be nested divs even inside <main>. Prefer a substantial
        # sentence in a paragraph, retaining text from inline links/emphasis.
        prose = next((text for text in self.prose if len(text.split()) >= 8
                      and re.search(r"[.!?。！？](?:\s|$)", text)), None)
        return " ".join((prose or " ".join(self.blocks)).split()[:25])

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.seen_html |= tag == "html"
        marker = ((a.get("id") or "") + " " + (a.get("class") or "")).lower()
        parent_blocked = self.stack[-1][1] if self.stack else False
        region = self.stack[-1][3] if self.stack else 0
        if tag == "main":
            region = max(region, 1)
        if tag == "article":
            region = max(region, 2)
        if a.get("id") == "mw-content-text":
            region = max(region, 3)
        if "mw-parser-output" in marker.split():
            region = max(region, 4)
        blocked = parent_blocked or tag in {"script", "style", "nav", "header", "footer", "aside", "form", "noscript"} or any(
            name in marker.split() for name in ("toc", "mw-editsection", "navbox"))
        if tag == "p":
            self._flush_paragraph()
            self._paragraph_region = region
        if tag == "meta":
            if a.get("property") == "og:title":
                self.title = a.get("content", "")[:256]
            if a.get("name", "").lower() in {"robots", "googlebot"}:
                signals = a.get("content", "")
                if re.search(r"\b(?:noindex|none|noai|nosnippet)\b|max-snippet\s*:\s*0\b", signals, re.I):
                    self.denied = True
                self.no_full_text |= bool(re.search(r"\bnoarchive\b", signals, re.I))
                self.follow &= not bool(re.search(r"\bnofollow\b", signals, re.I))
        href = a.get("href", "")
        if tag in {"link", "a"}:
            rel = a.get("rel", "").lower().split()
            if "canonical" in rel:
                self.canonical = href
            if tag == "link" and "edituri" in rel:
                self.edit_uri = href
            # A publisher footer or explicit license relation is a declaration,
            # unlike an arbitrary Creative Commons link quoted in an article.
            license_context = "license" in rel or "license" in marker or "copyright" in marker or any(
                "footer" in item[2] or "copyright" in item[2] or "license" in item[2] for item in self.stack)
            match = _LICENSE.fullmatch(href)
            if license_context and match:
                self.license = ("CC0-1.0" if match[3] else "CC-" + match[1].upper() + "-" + match[2])
            if (tag == "a" and href and "nofollow" not in rel
                    and not any(item[0] in {"form", "script", "style", "noscript"} for item in self.stack)):
                # Navigation can be the only path to a section. Its text stays
                # out of the article; outgoing links still pass normal scope checks.
                self.links.append(href)
        if tag in {"p", "li", "h1", "h2", "h3", "h4", "dt", "dd", "pre", "td", "br"}:
            self._flush()
        if tag not in {"meta", "link", "br", "hr", "img", "input", "source", "wbr", "area", "base", "embed", "param"}:
            self.stack.append((tag, blocked, tag + " " + marker, region))

    def handle_endtag(self, tag):
        if tag == "p":
            self._flush_paragraph()
        if tag in {"p", "li", "h1", "h2", "h3", "h4", "dt", "dd", "pre", "td", "article", "main"}:
            self._flush()
        if tag in {"body", "html"}:
            # Do not let a closing document tag hide unclosed article/main
            # containers that indicate the response was truncated.
            self.incomplete |= any(item[0] in {"article", "main"} for item in self.stack)
            if tag == "html":
                self.complete = not self.incomplete
        index = next((i for i in range(len(self.stack)-1, -1, -1) if self.stack[i][0] == tag), None)
        if index is not None:
            del self.stack[index:]
        if not self.seen_html and tag in {"article", "main"}:
            self.complete = not self.incomplete and not any(item[0] in {"article", "main"} for item in self.stack)

    def handle_data(self, data):
        if self.stack and not self.stack[-1][1]:
            if self.stack[-1][0] == "title":
                if not self.title:
                    self.title = data.strip()[:256]
            else:
                region = self.stack[-1][3]
                if region != self._current_region:
                    self._flush()
                    self._current_region = region
                self.current.append(data + " ")
                if any(item[0] == "p" for item in self.stack):
                    self._paragraph.append(data + " ")


def _queue(db, root, url):
    db.execute("INSERT OR IGNORE INTO web_pages(root,url) VALUES(?,?)", (root, url))


def _conditional(etag, modified):
    return {name: value for name, value in (
        ("If-None-Match", etag), ("If-Modified-Since", modified)) if value}


def _sitemap_stamp(entry, ns):
    value = (entry.findtext("s:lastmod", namespaces=ns) or "").strip()
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value


def _discover(db, contact, root, rules):
    """Sitemaps are discovery hints. Their absence never removes known pages."""
    from xml.etree import ElementTree
    sitemap_urls = re.findall(r"^\s*Sitemap:\s*(https://\S+)\s*$", rules, re.I | re.M)
    if not sitemap_urls:
        sitemap_urls = [urljoin(root, "/sitemap.xml")]
    def enqueue(url):
        try:
            url = _url(url)
        except ValueError:
            return
        if (urlsplit(url).netloc == urlsplit(root).netloc and _allowed(rules, url)
                and re.search(r"\.xml(?:\.gz)?$", urlsplit(url).path, re.I)
                and not _ADMIN.search(unquote(urlsplit(url).path))):
            db.execute("INSERT OR IGNORE INTO web_sitemaps(root,url) VALUES(?,?)", (root, url))
    with db:
        for url in sitemap_urls:
            enqueue(url)
    due = db.execute("""SELECT url,etag,modified FROM web_sitemaps WHERE root=? AND next_attempt<=?
        ORDER BY next_attempt,url LIMIT 2""", (root, contact.clock())).fetchall()
    fetched = 0
    for url, etag, modified in due:
        with db:
            db.execute("UPDATE web_sitemaps SET next_attempt=? WHERE root=? AND url=?", (contact.clock()+900, root, url))
        conditional = _conditional(etag, modified)
        response = contact.follow(url, rules=rules, headers=conditional)
        fetched += 1
        if response.status == 304 and conditional:
            with db:
                db.execute("""UPDATE web_sitemaps SET checked=?,next_attempt=?,last_error=NULL
                    WHERE root=? AND url=?""", (contact.clock(), contact.clock()+86_400, root, url))
            continue
        if response.status in (404, 410):
            with db:
                db.execute("""UPDATE web_sitemaps SET next_attempt=?,last_error=NULL,
                    checked=NULL,etag=NULL,modified=NULL WHERE root=? AND url=?""", (contact.clock()+86_400, root, url))
            continue
        if response.status != 200:
            raise ValueError("Incomplete public wiki sitemap")
        body = _payload(response, corpus.SITEMAP_LIMIT)
        if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)", body, re.I):
            raise ValueError("Incomplete public wiki sitemap")
        feed = ElementTree.fromstring(body)
        ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        with db:
            if feed.tag.endswith("}sitemapindex"):
                for entry in feed.findall("s:sitemap", ns):
                    location = entry.findtext("s:loc", namespaces=ns)
                    if not location:
                        raise ValueError("Incomplete public wiki sitemap")
                    enqueue(location)
            elif feed.tag.endswith("}urlset"):
                previous = dict(db.execute("""SELECT url,stamp FROM web_sitemap_entries
                    WHERE root=? AND sitemap=?""", (root, url)))
                entries = {}
                for entry in feed.findall("s:url", ns):
                    location = entry.findtext("s:loc", namespaces=ns)
                    if not location:
                        raise ValueError("Incomplete public wiki sitemap")
                    target = _article(root, location)
                    if target:
                        entries[target] = _sitemap_stamp(entry, ns)
                for target, stamp in entries.items():
                    if target not in previous or previous[target] != stamp:
                        _queue(db, root, target)
                        db.execute("""INSERT INTO web_sitemap_entries VALUES(?,?,?,?)
                            ON CONFLICT(root,sitemap,url) DO UPDATE SET stamp=excluded.stamp""",
                            (root, url, target, stamp))
                        db.execute("UPDATE web_pages SET next_attempt=0 WHERE root=? AND url=?", (root, target))
                for target in previous.keys() - entries.keys():
                    db.execute("DELETE FROM web_sitemap_entries WHERE root=? AND sitemap=? AND url=?",
                               (root, url, target))
                    # Missing from one map is a reason to verify, never proof of deletion.
                    db.execute("UPDATE web_pages SET next_attempt=0 WHERE root=? AND url=?", (root, target))
            else:
                raise ValueError("Unrecognized public wiki sitemap")
            db.execute("""UPDATE web_sitemaps SET next_attempt=?,last_error=NULL,checked=?,etag=?,modified=?
                WHERE root=? AND url=?""", (contact.clock()+86_400, contact.clock(),
                response.headers.get("etag"), response.headers.get("last-modified"), root, url))
    return fetched


def _purge(db, root, url, status, observed_at=None):
    prior = db.execute("SELECT status FROM web_pages WHERE root=? AND url=?", (root, url)).fetchone()
    if status == "blocked" and prior and prior[0] != "blocked" and observed_at:
        corpus.record_source_change(db, url, observed_at, "web_storage_denied")
    db.execute("DELETE FROM web_passages WHERE root=? AND url=?", (root, url))
    db.execute("UPDATE web_pages SET status=?,storage_mode=NULL,content_sha256=NULL WHERE root=? AND url=?", (status, root, url))


def refresh_web_sources(db_path, roots, *, batch_size=5, stop_event=None,
                        _transport=None, _clock=time.time):
    """Refresh enabled roots only; root selection never deletes personal notes."""
    roots = _roots(roots)
    if type(batch_size) is not int or not 1 <= batch_size <= 100:
        raise ValueError("batch_size must be 1..100")
    report = {"indexed": 0, "failed": 0, "deferred": 0, "cancelled": False, "roots": {}}
    if not roots:
        report["coverage"] = {}
        return report
    path = Path(db_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=2)) as db:
        db.executescript(SCHEMA)
        map_columns = {r[1] for r in db.execute("PRAGMA table_info(web_sitemaps)")}
        for name, kind in (("etag", "TEXT"), ("modified", "TEXT"), ("checked", "REAL")):
            if name not in map_columns:
                db.execute(f"ALTER TABLE web_sitemaps ADD COLUMN {name} {kind}")
        db.commit()
        if "discovery_retry" not in {r[1] for r in db.execute("PRAGMA table_info(web_roots)")}:
            db.execute("ALTER TABLE web_roots ADD COLUMN discovery_retry REAL NOT NULL DEFAULT 0")
            db.commit()
        if "redirect_url" not in {r[1] for r in db.execute("PRAGMA table_info(web_pages)")}:
            db.execute("ALTER TABLE web_pages ADD COLUMN redirect_url TEXT")
            db.commit()
        contact = _Contact(db, _transport or globals()["_transport"], _clock, stop_event)
        last = db.execute("SELECT last_root FROM web_cursor WHERE id=1").fetchone()
        start = (roots.index(last[0])+1) % len(roots) if last and last[0] in roots else 0
        ordered = roots[start:] + roots[:start]
        active_roots = ordered[:batch_size]
        report["roots"] = {root: {"state": "not_selected"} for root in roots}
        for root in active_roots:
            with db:
                db.execute("INSERT OR REPLACE INTO web_cursor VALUES(1,?)", (root,))
                db.execute("INSERT OR IGNORE INTO web_roots(root) VALUES(?)", (root,))
                _queue(db, root, root)
            try:
                rules = contact.robots(root)
                observed = datetime.fromtimestamp(_clock(), UTC).isoformat(timespec="microseconds")
                with db:
                    for (url,) in db.execute("SELECT url FROM web_pages WHERE root=? AND status='indexed'", (root,)).fetchall():
                        if not _allowed(rules, url):
                            _purge(db, root, url, "blocked", observed)
                if not _allowed(rules, root):
                    with db:
                        for (url,) in db.execute("SELECT url FROM web_pages WHERE root=?", (root,)).fetchall():
                            _purge(db, root, url, "blocked", observed)
                    raise _Denied("robots_disallowed")
                report["roots"][root] = {"state": "ready"}
                row = db.execute("SELECT last_discovery,discovery_retry FROM web_roots WHERE root=?", (root,)).fetchone()
                if _clock() >= row[1]:
                    try:
                        fetched = _discover(db, contact, root, rules)
                        with db:
                            if fetched:
                                db.execute("UPDATE web_roots SET last_discovery=?,discovery_retry=0,last_error=NULL WHERE root=?", (_clock(), root))
                    except (_Cancelled, _Cooldown):
                        raise
                    except Exception as exc:
                        # A broken sitemap cannot prevent the directly supplied
                        # home page or already discovered articles from working.
                        with db:
                            db.execute("UPDATE web_roots SET discovery_retry=?,last_error=? WHERE root=?", (_clock()+900, type(exc).__name__, root))
                        report["roots"][root]["discovery_error"] = type(exc).__name__
                        report["failed"] += 1
                try:
                    report["roots"][root]["api"] = _enumerate_api(db, contact, root, rules)
                except _Cancelled:
                    raise
                except Exception as exc:
                    with db:
                        db.execute("UPDATE web_apis SET next_attempt=?,last_error=? WHERE root=?",
                            (exc.until if isinstance(exc, _Cooldown) else _clock()+900, type(exc).__name__, root))
                    report["roots"][root]["api"] = {"state": "failed", "error_type": type(exc).__name__}
                    report["deferred"] += isinstance(exc, _Cooldown)
                    report["failed"] += not isinstance(exc, _Cooldown)
            except _Cancelled:
                report["cancelled"] = True
                break
            except Exception as exc:
                with db:
                    db.execute("UPDATE web_roots SET last_error=? WHERE root=?", (type(exc).__name__, root))
                report["roots"][root] = {"state": "deferred" if isinstance(exc, _Cooldown) else "failed", "error_type": type(exc).__name__}
                if isinstance(exc, _Denied):
                    report["roots"][root]["reason"] = exc.reason
                report["failed"] += not isinstance(exc, _Cooldown)
                report["deferred"] += isinstance(exc, _Cooldown)
                continue
        # Round-robin roots and alternate new/revisited pages. No depth cutoff;
        # existing pages (including 404s) are periodically checked for recovery.
        selected = 0
        while not report["cancelled"] and selected < batch_size:
            progressed = False
            for root in active_roots:
                if selected >= batch_size:
                    break
                if report["roots"].get(root, {}).get("state") != "ready":
                    continue
                prefer = db.execute("SELECT prefer_existing FROM web_roots WHERE root=?", (root,)).fetchone()[0]
                row = None
                for group in (prefer, 1-prefer):
                    row = db.execute("""SELECT url,etag,last_modified,content_sha256,status,redirect_url FROM web_pages
                        WHERE root=? AND (retrieved_at IS NOT NULL)=? AND next_attempt<=?
                        AND NOT (status='indexed' AND next_attempt>0 AND next_attempt>?
                            AND last_error IS NULL AND EXISTS (
                            SELECT 1 FROM web_sitemap_entries e JOIN web_sitemaps s
                              ON s.root=e.root AND s.url=e.sitemap
                            WHERE e.root=web_pages.root AND e.url=web_pages.url AND e.stamp IS NOT NULL
                              AND s.checked>=?))
                        ORDER BY next_attempt,url LIMIT 1""",
                        # A fresh sitemap date can defer the weekly recheck, but
                        # audit after 30 days in case a publisher missed an edit.
                        (root, group, _clock(), _clock()-23*86_400, _clock()-86_400)).fetchone()
                    if row:
                        break
                if not row:
                    continue
                url, etag, modified, old_hash, old_status, old_redirect = row
                now = _clock()
                observed = datetime.fromtimestamp(now, UTC).isoformat(timespec="microseconds")
                with db:
                    db.execute("UPDATE web_roots SET prefer_existing=? WHERE root=?", (1-group, root))
                    db.execute("""UPDATE web_pages SET next_attempt=?,last_error='verification_pending'
                        WHERE root=? AND url=?""", (now+900, root, url))
                progressed = True
                selected += 1
                try:
                    contact.cancelled()
                    rules = contact.robots(root)
                    if not _allowed(rules, url):
                        with db:
                            _purge(db, root, url, "blocked", observed)
                        raise _Denied("robots_disallowed")
                    conditional = _conditional(etag, modified) if old_status == "indexed" else {}
                    response = contact.get(url, rules=rules, headers=conditional)
                    header_signals = response.headers.get("x-robots-tag", "")
                    if re.search(r"\b(?:noindex|none|noai|nosnippet)\b|max-snippet\s*:\s*0\b", header_signals, re.I):
                        raise _Denied("publisher_storage_denied")
                    if response.status in (301, 302, 303, 307, 308):
                        target = _article(root, urljoin(url, response.headers.get("location", "")))
                        if not target or target == url:
                            raise ValueError("Unvetted wiki redirect")
                        with db:
                            _queue(db, root, target)
                            _purge(db, root, url, "redirected")
                            if old_status != "redirected" or old_redirect != target:
                                corpus.record_source_change(db, url, observed, "web_url_redirected")
                            db.execute("UPDATE web_pages SET retrieved_at=?,next_attempt=?,redirect_url=?,last_error=NULL WHERE root=? AND url=?", (observed, now+604_800, target, root, url))
                        continue
                    if response.status in (404, 410):
                        with db:
                            if old_status != "removed":
                                corpus.record_source_change(db, url, observed, "web_page_removed")
                            _purge(db, root, url, "removed")
                            db.execute("UPDATE web_pages SET status='removed',retrieved_at=?,next_attempt=?,last_error=NULL WHERE root=? AND url=?", (observed, now+604_800, root, url))
                        continue
                    if response.status == 304 and conditional and old_hash and old_status == "indexed":
                        if re.search(r"\bnoarchive\b", header_signals, re.I):
                            raise _Denied("publisher_full_text_denied")
                        with db:
                            db.execute("UPDATE web_pages SET next_attempt=?,last_error=NULL WHERE root=? AND url=?", (now+604_800, root, url))
                        continue
                    if response.status != 200:
                        raise ValueError("Public wiki page request failed")
                    content_type = response.headers.get("content-type", "text/html").split(";", 1)[0].lower()
                    if content_type not in {"text/html", "application/xhtml+xml"}:
                        raise ValueError("Wiki link is not an HTML article")
                    page = _Page(_payload(response, 4_000_000).decode("utf-8-sig"))
                    if page.canonical and _article(root, urljoin(url, page.canonical)) is None:
                        raise _Denied("canonical_out_of_scope")
                    signals = response.headers.get("x-robots-tag", "")
                    if page.denied or re.search(r"\b(?:noindex|none|noai|nosnippet)\b|max-snippet\s*:\s*0\b", signals, re.I):
                        raise _Denied("publisher_storage_denied")
                    canonical = _article(root, urljoin(url, page.canonical)) if page.canonical else url
                    if canonical != url:
                        with db:
                            _queue(db, root, canonical)
                            _purge(db, root, url, "redirected")
                            if old_status != "redirected" or old_redirect != canonical:
                                corpus.record_source_change(db, url, observed, "web_url_redirected")
                            db.execute("UPDATE web_pages SET retrieved_at=?,next_attempt=?,redirect_url=? WHERE root=? AND url=?", (observed, now+604_800, canonical, root, url))
                        continue
                    if not page.complete or re.search(
                            r"^(?:just a moment|access denied|attention required|page not found|404|403)\b", page.title, re.I):
                        raise ValueError("Public wiki page has no usable article text")
                    # Even a sparse home page can declare useful discovery
                    # metadata. It cannot become evidence without usable prose.
                    with db:
                        can_follow = page.follow and not re.search(r"\bnofollow\b", signals, re.I)
                        if page.edit_uri and can_follow:
                            _register_api(db, root, urljoin(url, page.edit_uri))
                        for link in (page.links if can_follow else ()):
                            target = _article(root, urljoin(url, link))
                            if target:
                                _queue(db, root, target)
                    if not page.blocks or sum(map(len, page.blocks)) < 42:
                        raise ValueError("Public wiki page has no usable article text")
                    title = page.title or corpus.wiki_title(url)
                    full_text = page.license and not page.no_full_text and not re.search(r"\bnoarchive\b", signals, re.I)
                    mode = "full_text" if full_text else "short_excerpt"
                    passages = ([block[start:start+1600] for block in page.blocks for start in range(0, len(block), 1600)]
                                if full_text else [page.short_excerpt()])
                    content_hash = hashlib.sha256(json.dumps([title, page.license, passages], ensure_ascii=False).encode()).hexdigest()
                    with db:
                        if old_hash and old_hash != content_hash or old_status in {"removed", "redirected", "blocked"}:
                            corpus.record_source_change(db, url, observed, "web_page_changed")
                        if old_hash != content_hash or old_status != "indexed":
                            db.execute("DELETE FROM web_passages WHERE root=? AND url=?", (root, url))
                            db.executemany("INSERT INTO web_passages(root,url,ordinal,title,text) VALUES(?,?,?,?,?)",
                                [(root, url, i, title, text) for i, text in enumerate(passages)])
                        db.execute("""UPDATE web_pages SET title=?,status='indexed',license=?,storage_mode=?,
                            retrieved_at=?,etag=?,last_modified=?,content_sha256=?,next_attempt=?,last_error=NULL,redirect_url=NULL
                            WHERE root=? AND url=?""", (title, page.license, mode, observed,
                            response.headers.get("etag"), response.headers.get("last-modified"), content_hash, now+604_800, root, url))
                        db.execute("UPDATE web_roots SET last_success=? WHERE root=?", (now, root))
                    report["indexed"] += 1
                except _Cancelled:
                    report["cancelled"] = True
                    break
                except Exception as exc:
                    until = exc.until if isinstance(exc, _Cooldown) else now + 900
                    with db:
                        if isinstance(exc, _Denied):
                            _purge(db, root, url, "blocked", observed)
                        db.execute("UPDATE web_pages SET next_attempt=?,last_error=? WHERE root=? AND url=?", (until, exc.reason if isinstance(exc, _Denied) else type(exc).__name__, root, url))
                    report["deferred"] += isinstance(exc, _Cooldown)
                    report["failed"] += not isinstance(exc, _Cooldown)
            if not progressed:
                break
    report["coverage"] = web_coverage(db_path, roots)
    return report


def _read(db_path, sql, args):
    path = Path(db_path).expanduser()
    if not path.is_file():
        return []
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(sql, args)]
    except sqlite3.Error:
        return []


def search_web_sources(query, db_path, roots, limit=3):
    roots = _roots(roots)
    terms = _query_terms(query)
    if not roots or not terms or type(limit) is not int or not 1 <= limit <= 10:
        return []
    rows = _read(db_path, """SELECT w.root,w.url,w.ordinal,w.title,w.text,p.license,p.storage_mode,
        p.retrieved_at,p.last_error FROM web_passages w JOIN web_pages p ON p.root=w.root AND p.url=w.url
        WHERE web_passages MATCH ? AND p.status='indexed' AND w.root IN (SELECT value FROM json_each(?))
        ORDER BY bm25(web_passages),w.url,w.ordinal LIMIT ?""",
        (' OR '.join('"' + term + '"' for term in terms), json.dumps(roots), limit * 5))
    found, seen = [], set()
    for row in rows:
        if row["url"] in seen:
            continue
        seen.add(row["url"])
        found.append({"title": row["title"], "excerpt": row["text"][:1600], "citation": row["url"],
            "source_kind": "external_wiki_reference", "license": row["license"],
            "attribution": urlsplit(row["url"]).hostname + (" · " + row["license"] if row["license"] else ""),
            "storage_mode": row["storage_mode"], "retrieved_at": row["retrieved_at"],
            "passage": row["ordinal"], "needs_review": bool(row["last_error"]), "live_state": False})
        if len(found) == limit:
            break
    return found


def read_web_page(db_path, url, roots, offset=0):
    roots = _roots(roots)
    if type(offset) is not int or not 0 <= offset <= 9_223_372_036_854_775_807 or not any(_article(root, url) == url for root in roots):
        return {"status": "unavailable", "citation": url, "passages": [], "next_offset": None, "live_state": False}
    rows = _read(db_path, """SELECT w.ordinal,w.title,w.text,p.url,p.license,p.storage_mode,p.retrieved_at,p.last_error
        FROM web_passages w JOIN web_pages p ON p.root=w.root AND p.url=w.url
        WHERE p.url=? AND p.status='indexed' AND w.root IN (SELECT value FROM json_each(?)) AND w.ordinal>=?
        ORDER BY w.ordinal LIMIT 11""", (url, json.dumps(roots), offset))
    return {"status": "reference" if rows else "unavailable", "citation": url,
            "passages": rows[:10], "next_offset": rows[9]["ordinal"]+1 if len(rows) > 10 else None,
            "live_state": False}


def web_coverage(db_path, roots):
    roots = _roots(roots)
    if not roots:
        return {}
    result = {root: {"pages": {}, "storage_modes": {}, "indexed_storage_modes": {}} for root in roots}
    encoded = json.dumps(roots)
    for row in _read(db_path, """SELECT root,status,storage_mode,count(*) AS pages FROM web_pages
            WHERE root IN (SELECT value FROM json_each(?)) GROUP BY root,status,storage_mode""", (encoded,)):
        item = result[row["root"]]
        item["pages"][row["status"]] = item["pages"].get(row["status"], 0) + row["pages"]
        if row["storage_mode"]:
            modes = item["storage_modes"]
            modes[row["storage_mode"]] = modes.get(row["storage_mode"], 0) + row["pages"]
            if row["status"] == "indexed":
                item["indexed_storage_modes"][row["storage_mode"]] = row["pages"]
    for row in _read(db_path, """SELECT root,last_discovery,last_success,last_error FROM web_roots
            WHERE root IN (SELECT value FROM json_each(?))""", (encoded,)):
        result[row.pop("root")].update(row)
    for root in roots:
        result[root]["first_gaps"] = _read(db_path, """SELECT url,status,storage_mode,last_error
            FROM web_pages WHERE root=? AND (status IN ('pending','blocked') OR
              (status='indexed' AND storage_mode='short_excerpt'))
            ORDER BY last_error IS NOT NULL DESC,status,url LIMIT 5""", (root,))
    for row in _read(db_path, """SELECT root,url,last_success,last_error,cursor IS NOT NULL AS enumeration_pending
            FROM web_apis WHERE root IN (SELECT value FROM json_each(?))""", (encoded,)):
        row["enumeration_pending"] = bool(row["enumeration_pending"])
        result[row.pop("root")]["api"] = row
    return result
