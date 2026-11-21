"""Opt-in, bounded readers for public JSON game reference collections.

Each configured collection has an isolated local snapshot database. Network
content is published there only after a complete feed pass; failed or partial
passes leave the last published snapshot intact. Policy denials make the feed
unreadable until a later successful permitted pass.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import zlib
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from . import corpus, web_sources
from .snapshots import _public_url, import_snapshot

MAX_RESPONSE_BYTES = 4_000_000
MAX_ARTICLES = 2_000_000
MAX_PAGES_PER_PASS = 100
REFRESH_INTERVAL = 24 * 60 * 60
FAILURE_BACKOFF = 15 * 60
MAX_FAILURE_BACKOFF = 24 * 60 * 60

_FIELDS = {"url", "items", "title", "text", "url_field", "next", "total",
           "url_template", "id_field"}
_DEFAULT_ITEMS = ("results", "items", "data", "records", "entries")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS json_feed_state(
    url TEXT PRIMARY KEY,
    mapping_sha256 TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'not_started',
    readable INTEGER NOT NULL DEFAULT 0 CHECK(readable IN (0,1)),
    last_attempt REAL,
    last_success REAL,
    next_attempt REAL NOT NULL DEFAULT 0,
    next_url TEXT,
    page_count INTEGER NOT NULL DEFAULT 0,
    article_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    last_http_status INTEGER
);
CREATE TABLE IF NOT EXISTS json_feed_stage(
    feed_url TEXT NOT NULL,
    page_url TEXT NOT NULL,
    article_url TEXT NOT NULL,
    article_json TEXT NOT NULL,
    PRIMARY KEY(feed_url, page_url, article_url)
);
CREATE TABLE IF NOT EXISTS json_feed_runs(
    feed_url TEXT PRIMARY KEY,
    page_count INTEGER NOT NULL,
    declared_total INTEGER,
    started_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS json_feed_stage_article ON json_feed_stage(feed_url,article_url);
CREATE TABLE IF NOT EXISTS json_feed_visited(
    feed_url TEXT NOT NULL, page_url TEXT NOT NULL, PRIMARY KEY(feed_url,page_url)
);
CREATE TABLE IF NOT EXISTS json_feed_pages(
    feed_url TEXT NOT NULL,
    page_url TEXT NOT NULL,
    body_zlib BLOB NOT NULL,
    body_sha256 TEXT NOT NULL,
    etag TEXT,
    last_modified TEXT,
    next_url TEXT,
    total INTEGER,
    policy TEXT NOT NULL DEFAULT '',
    fetched_at REAL NOT NULL,
    checked_at REAL NOT NULL,
    PRIMARY KEY(feed_url,page_url)
);
"""


@dataclass(frozen=True)
class JsonFeed:
    """One public JSON collection and its dotted-field mapping."""

    url: str
    items: str = "results|items|data|records|entries"
    title: str = "title|name|attributes.title|attributes.name"
    text: str = "text|description|content|summary|body|attributes.description|attributes.text"
    url_field: str = "canonical_url|html_url|url|link|links.self"
    next: str = "next|links.next"
    total: str = "total|count|meta.total"
    url_template: str | None = None
    id_field: str | None = None

    @property
    def mapping_sha256(self) -> str:
        value = {name: getattr(self, name) for name in _FIELDS}
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _feed_url(value: object) -> str:
    """Validate a public HTTPS URL while allowing ordinary API query strings."""
    if not isinstance(value, str) or not value or len(value) > 4000:
        raise ValueError("Use a public HTTPS JSON collection URL")
    if any(ord(char) <= 32 or ord(char) == 127 for char in value) or "\\" in value:
        raise ValueError("JSON collection URL contains whitespace or unsafe characters")
    parts = urlsplit(value)
    if parts.scheme.lower() != "https" or not parts.hostname or parts.username or parts.password or parts.fragment:
        raise ValueError("JSON collection URLs require HTTPS without credentials or fragments")
    host = parts.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    try:
        port = parts.port
    except ValueError as error:
        raise ValueError("Invalid JSON collection URL port") from error
    if port not in (None, 443):
        raise ValueError("JSON collection URLs must use the standard HTTPS port")
    path = parts.path or "/"
    # Reuse the snapshot public-address checks while allowing a query-based API.
    validated, _ = _public_url(urlunsplit(("https", host, path if path != "/" else "/robots.txt", "", "")),
                               https_only=True)
    decoded = unquote(path)
    if ("\\" in decoded or any(part in (".", "..") for part in decoded.split("/"))
            or re.search(r"%(?:2f|5c|25)", path, re.I)):
        raise ValueError("Ambiguous JSON collection path")
    if len(parts.query) > 2000 or re.search(r"(?:^|&)(?:api[_-]?key|access[_-]?token|authorization|bearer)(?:=|&|$)", parts.query, re.I):
        raise ValueError("JSON collection URLs cannot contain credentials")
    return urlunsplit(("https", urlsplit(validated).netloc, path, parts.query, ""))


def _mapping_value(value: object, name: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise ValueError(f"JSON field mapping {name!r} must be a nonempty dotted path")
    result = value.strip()
    alternatives = result.split("|")
    if any(not path or path.startswith("$") for path in alternatives):
        raise ValueError(f"JSON field mapping {name!r} is not a supported dotted path")
    for path in alternatives:
        segments = path.split(".")
        if (any(not segment or segment in (".", "..") for segment in segments)
                or any(not re.fullmatch(r"[A-Za-z0-9_*-]{1,100}", segment) for segment in segments)):
            raise ValueError(f"JSON field mapping {name!r} contains an unsupported path segment")
    return result


def _normalize_feed(raw: object) -> JsonFeed:
    if isinstance(raw, JsonFeed):
        raw = {field: getattr(raw, field) for field in _FIELDS}
    if isinstance(raw, str):
        raw = {"url": raw}
    if not isinstance(raw, dict) or set(raw) - _FIELDS:
        raise ValueError("Each JSON feed must be a URL or a JSON object with supported mapping fields")
    url = _feed_url(raw.get("url"))
    values = {}
    for key in ("items", "title", "text", "url_field", "next", "total", "url_template", "id_field"):
        default = getattr(JsonFeed(url), key)
        value = raw.get(key, default)
        if key == "url_template" and value is not None:
            if not isinstance(value, str) or not value.strip() or len(value) > 4000:
                raise ValueError("url_template must be a public article URL containing {id}")
            value = value.strip()
        elif key == "url_template":
            value = None
        else:
            value = _mapping_value(value, key, optional=key == "id_field")
        values[key] = value
    template = values["url_template"]
    ident = values["id_field"]
    if (template is None) != (ident is None):
        raise ValueError("url_template and id_field must be supplied together")
    if template is not None:
        if template.count("{id}") != 1 or re.search(r"\{(?!id\})[^{}]+\}", template):
            raise ValueError("url_template must contain exactly one {id} placeholder")
        _feed_url(template.replace("{id}", "sample-id"))
    return JsonFeed(url=url, **values)


def parse_feeds(text: object) -> tuple[JsonFeed, ...]:
    """Parse one public endpoint or JSON field mapping per line; # pauses a line."""
    if not isinstance(text, str):
        raise ValueError("Supply JSON collection URLs or mappings as text")
    feeds, seen = [], set()
    for line_number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            raw = json.loads(line) if line.startswith("{") else line
            feed = _normalize_feed(raw)
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            raise ValueError(f"Invalid JSON feed on line {line_number}: {error}") from error
        if feed.url not in seen:
            feeds.append(feed)
            seen.add(feed.url)
    return tuple(feeds)


def feed_db_path(db_path: Path | str, feed: JsonFeed) -> Path:
    """Return the isolated snapshot database for a configured feed."""
    base = Path(db_path).expanduser().resolve()
    database_token = hashlib.sha256(os.path.normcase(str(base)).casefold().encode("utf-8")).hexdigest()
    feed_token = hashlib.sha256(feed.url.encode("utf-8")).hexdigest()
    return base.parent / "api-feeds" / database_token / f"{feed_token}.sqlite3"


def _connect(db_path: Path | str, *, create: bool) -> sqlite3.Connection:
    path = Path(db_path).expanduser()
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(corpus.SCHEMA + web_sources.SCHEMA + _SCHEMA)
        columns = {row[1] for row in db.execute("PRAGMA table_info(json_feed_pages)")}
        if "policy" not in columns:
            db.execute("ALTER TABLE json_feed_pages ADD COLUMN policy TEXT NOT NULL DEFAULT ''")
        db.commit()
        return db
    if not path.is_file():
        raise FileNotFoundError(path)
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
    db.row_factory = sqlite3.Row
    return db


def feed_status(db_path: Path | str, feed: JsonFeed, *, clock=time.time) -> dict:
    """Return local state without creating files; only permitted published data is readable."""
    empty = {"url": feed.url, "status": "not_started", "readable": False, "articles": 0,
             "pages": 0, "last_attempt": None, "last_success": None, "next_attempt": None,
             "last_error": None, "last_http_status": None, "failure_count": 0}
    try:
        with closing(_connect(db_path, create=False)) as db:
            row = db.execute("SELECT * FROM json_feed_state WHERE url=?", (feed.url,)).fetchone()
    except (OSError, sqlite3.Error):
        return empty
    if row is None or row["mapping_sha256"] != feed.mapping_sha256:
        return empty
    data = dict(row)
    path = feed_db_path(db_path, feed)
    readable = bool(data["readable"])
    article_count = 0
    if readable and path.is_file():
        try:
            with closing(_connect(path, create=False)) as db:
                exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='snapshot_pages'").fetchone()
                if exists:
                    article_count = db.execute("SELECT count(*) FROM snapshot_pages").fetchone()[0]
                else:
                    readable = False
        except (OSError, sqlite3.Error):
            readable = False
    else:
        readable = False
    data["readable"] = readable
    data["articles"] = article_count if data["readable"] else 0
    data.pop("mapping_sha256", None)
    data.pop("next_url", None)
    data["pages"] = data.pop("page_count")
    return data


def _select(value: object, mapping: str | None):
    if mapping is None:
        return None
    for alternative in mapping.split("|"):
        current = value
        for segment in alternative.split("."):
            if segment == "*":
                current = current if isinstance(current, list) else None
                break
            if isinstance(current, dict):
                current = current.get(segment)
            elif isinstance(current, list) and segment.isdecimal():
                index = int(segment)
                current = current[index] if index < len(current) else None
            else:
                current = None
                break
        if current is not None:
            return current
    return None


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(_strings(item))
        return result
    return []


def _text(value: object, field: str, max_chars: int) -> str:
    selected = _select(value, field)
    strings = _strings(selected)
    text = "\n".join(part.strip() for part in strings if part.strip())
    if not text or len(text) > max_chars:
        raise ValueError("JSON item has missing or oversized article text")
    return text


def _items(payload: object, feed: JsonFeed) -> list:
    if feed.items == JsonFeed.items and isinstance(payload, list):
        selected = payload
    elif feed.items == JsonFeed.items and isinstance(payload, dict):
        matches = [payload[key] for key in _DEFAULT_ITEMS if isinstance(payload.get(key), list)]
        if len(matches) > 1:
            raise ValueError("Multiple collections found. Choose one with the items field.")
        selected = matches[0] if matches else None
    else:
        selected = _select(payload, feed.items)
    if not isinstance(selected, list) or len(selected) > MAX_ARTICLES:
        raise ValueError("JSON collection has no bounded item array")
    return selected


def _same_origin(url: str, origin: str) -> str:
    target = _feed_url(urljoin(origin, url))
    if urlsplit(target).netloc != urlsplit(origin).netloc:
        raise ValueError("JSON feed pagination or article URL left its publisher origin")
    return target


def _article_url(item: dict, feed: JsonFeed) -> str:
    raw = _select(item, feed.url_field)
    if feed.url_template and feed.id_field:
        ident = _select(item, feed.id_field)
        if type(ident) not in (str, int) or not str(ident) or len(str(ident)) > 300:
            raise ValueError("JSON item has no usable mapped identifier")
        from urllib.parse import quote
        raw = feed.url_template.replace("{id}", quote(str(ident), safe=""))
    if not isinstance(raw, str):
        raise ValueError("JSON item has no mapped article URL")
    if not urlsplit(raw).scheme:
        raw = urljoin(feed.url, raw)
    return _public_url(raw, https_only=True)[0]


def _normalized(item: object, feed: JsonFeed) -> dict:
    if not isinstance(item, dict):
        raise ValueError("JSON feed items must be objects")
    title = _text(item, feed.title, 500)
    body = _text(item, feed.text, 2_000_000)
    result = {"url": _article_url(item, feed), "title": title, "text": body}
    revision = _select(item, "revision")
    if type(revision) in (str, int) and len(str(revision)) <= 100:
        result["revision"] = str(revision)
    timestamp = _select(item, "revision_timestamp|updated_at|modified|last_modified")
    if isinstance(timestamp, str) and len(timestamp) <= 100:
        result["revision_timestamp"] = timestamp
    build = _select(item, "game_build|version")
    if isinstance(build, str) and len(build) <= 100:
        result["game_build"] = build
    return result


def _feed_json(response, cached_body: bytes | None = None) -> tuple[object, bytes]:
    if response.status == 304:
        if cached_body is None:
            raise ValueError("JSON collection returned 304 without a validated cached page")
        body = cached_body
    elif response.status == 200:
        body = web_sources._payload(response, MAX_RESPONSE_BYTES)
    elif response.status in (401, 403):
        raise _PolicyDenied(f"http_{response.status}")
    else:
        raise ValueError(f"JSON collection returned HTTP {response.status}")
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if response.status != 304 and content_type and content_type != "application/json" and not content_type.endswith("+json"):
        raise ValueError("JSON collection did not return a JSON content type")
    try:
        return json.loads(body.decode("utf-8-sig")), body
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("JSON collection response is not valid UTF-8 JSON") from error


def _policy_denial(headers, cached_policy: str = "") -> str | None:
    """Return a cache/input restriction reported by response headers or a 304 cache."""
    policy = " ".join((cached_policy or "", headers.get("content-signal", ""),
                       headers.get("x-robots-tag", ""), headers.get("use", "")))
    if re.search(r"\b(?:noarchive|noai)\b", policy, re.I):
        return "noarchive_or_noai"
    if headers.get("use", "").lower().strip() == "reference" or re.search(r"(?:^|[,;\s])use\s*=\s*reference\b", policy, re.I):
        return "reference_only"
    if re.search(r"\bai-input\s*=\s*no\b", policy, re.I):
        return "ai_input_denied"
    if re.search(r"(?:^|[,;\s])no-store(?:$|[,;\s])", headers.get("cache-control", ""), re.I):
        return "cache_control_no_store"
    return None


def _get(contact, url: str, rules: str, headers=None):
    seen = set()
    for _ in range(6):
        url = _feed_url(url)
        if url in seen or not web_sources._allowed(rules, url):
            raise _PolicyDenied("robots_or_content_signal")
        seen.add(url)
        request_headers = {"Accept": "application/json"}
        request_headers.update(headers or {})
        response = contact.get(url, rules=rules, headers=request_headers)
        if response.status not in (301, 302, 303, 307, 308):
            return response
        location = response.headers.get("location")
        if not location:
            raise ValueError("JSON collection redirect omitted its location")
        url = _same_origin(location, url)
    raise ValueError("JSON collection exceeded the redirect limit")


class _PolicyDenied(Exception):
    pass


def _state_upsert(db, feed: JsonFeed, now: float):
    prior = db.execute("SELECT mapping_sha256 FROM json_feed_state WHERE url=?", (feed.url,)).fetchone()
    changed = prior is not None and prior[0] != feed.mapping_sha256
    db.execute("""INSERT INTO json_feed_state(url,mapping_sha256,last_attempt)
        VALUES(?,?,?) ON CONFLICT(url) DO UPDATE SET mapping_sha256=excluded.mapping_sha256,
        last_attempt=excluded.last_attempt""", (feed.url, feed.mapping_sha256, now))
    if changed:
        db.execute("""UPDATE json_feed_state SET status='not_started',readable=0,next_attempt=0,
            next_url=NULL,page_count=0,article_count=0,last_success=NULL,last_error=NULL WHERE url=?""",
                   (feed.url,))
        db.execute("DELETE FROM json_feed_stage WHERE feed_url=?", (feed.url,))
        db.execute("DELETE FROM json_feed_runs WHERE feed_url=?", (feed.url,))


def _fail(db, feed: JsonFeed, now: float, error: Exception, *, policy: bool = False,
          http_status: int | None = None):
    row = db.execute("SELECT failure_count FROM json_feed_state WHERE url=?", (feed.url,)).fetchone()
    failures = (row[0] if row else 0) + 1
    delay = min(MAX_FAILURE_BACKOFF, FAILURE_BACKOFF * (2 ** min(failures - 1, 8)))
    db.execute("""UPDATE json_feed_state SET status=?,readable=CASE WHEN ? THEN 0 ELSE readable END,
        next_attempt=?,failure_count=?,last_error=?,last_http_status=? WHERE url=?""",
        ("blocked" if policy else "failed", int(policy), now + delay, failures,
         "policy_denied" if policy else type(error).__name__, http_status, feed.url))
    return {"status": "blocked" if policy else "failed", "error_type":
            "PolicyDenied" if policy else type(error).__name__}


def _extract_next(payload: object, feed: JsonFeed, base_url: str, response, cached) -> str | None:
    value = _select(payload, feed.next)
    if value not in (None, "", False):
        if not isinstance(value, str):
            raise ValueError("JSON pagination field must be a URL string or null")
        return _same_origin(value, base_url)
    links = response.headers.get("link")
    if links is None:
        return cached[4] if response.status == 304 and cached else None
    # RFC 8288. A comma in a URI or quoted parameter is part of the link.
    pos = 0
    while pos < len(links):
        target = re.match(r"\s*<([^<>]*)>\s*", links[pos:])
        if not target:
            raise ValueError("Invalid API pagination Link header")
        pos += target.end()
        params = {}
        while pos < len(links) and links[pos] == ";":
            param = re.match(r';\s*([\w*.-]+)\s*(?:=\s*("(?:\\.|[^"\\])*"|[^,;\s]+))?\s*', links[pos:])
            if not param:
                raise ValueError("Invalid API pagination link parameter")
            params.setdefault(param[1].lower(), (param[2] or "").strip('"'))
            pos += param.end()
        if "anchor" not in params and "next" in params.get("rel", "").lower().split():
            return _same_origin(target[1], base_url)
        if pos < len(links) and links[pos] != ",":
            raise ValueError("Invalid API pagination link separator")
        pos += 1
    return None


def _extract_total(payload: object, feed: JsonFeed, max_articles: int | None = MAX_ARTICLES) -> int | None:
    value = _select(payload, feed.total)
    if value is None:
        return None
    if type(value) is not int or value < 0 or (max_articles is not None and value > max_articles):
        raise ValueError("JSON total field must be a bounded nonnegative integer")
    return value


def _robots(contact, origin: str, now: float) -> str:
    """Refresh a cached robots policy with the shared host pacer."""
    host = urlsplit(origin).netloc
    row = contact.db.execute("SELECT robots,robots_checked FROM web_hosts WHERE host=?", (host,)).fetchone()
    if row and row[0] is not None and now - (row[1] or 0) < 86_400:
        if _policy_denial({}, row[0]):
            raise _PolicyDenied("robots_policy_denied")
        return row[0]
    response = _get(contact, urljoin(origin, "/robots.txt"), "", {"Accept": "text/plain,*/*;q=0.1"})
    if response.status in (404, 410):
        rules = ""
    elif response.status in (401, 403):
        raise _PolicyDenied(f"robots_http_{response.status}")
    elif response.status == 200:
        rules = web_sources._payload(response, 256_000).decode("utf-8-sig")
    else:
        raise ValueError("JSON feed robots.txt is unavailable")
    with contact.db:
        contact.db.execute("UPDATE web_hosts SET robots=?,robots_checked=? WHERE host=?",
                           (rules, now, host))
    if _policy_denial({}, rules):
        raise _PolicyDenied("robots_policy_denied")
    return rules


def _cached_page(db, feed: JsonFeed, page_url: str):
    return db.execute("""SELECT body_zlib,body_sha256,etag,last_modified,next_url,total,policy
        FROM json_feed_pages WHERE feed_url=? AND page_url=?""", (feed.url, page_url)).fetchone()


def _cached_body(row) -> bytes | None:
    if row is None:
        return None
    try:
        decoder = zlib.decompressobj()
        body = decoder.decompress(row[0], MAX_RESPONSE_BYTES + 1)
        if decoder.unconsumed_tail or not decoder.eof:
            return None
    except (zlib.error, TypeError):
        return None
    if not body or len(body) > MAX_RESPONSE_BYTES or hashlib.sha256(body).hexdigest() != row[1]:
        return None
    return body


def _store_page(db, feed: JsonFeed, page_url: str, body: bytes, response, next_url: str | None,
                total: int | None, policy: str, now: float):
    # A 200 response without a validator revokes the previous validator.
    etag = response.headers.get("etag")
    last_modified = response.headers.get("last-modified")
    digest = hashlib.sha256(body).hexdigest()
    with db:
        db.execute("""INSERT INTO json_feed_pages(feed_url,page_url,body_zlib,body_sha256,etag,
            last_modified,next_url,total,policy,fetched_at,checked_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(feed_url,page_url) DO UPDATE SET body_zlib=excluded.body_zlib,
            body_sha256=excluded.body_sha256,etag=excluded.etag,last_modified=excluded.last_modified,
            next_url=excluded.next_url,total=excluded.total,policy=excluded.policy,
            fetched_at=CASE WHEN excluded.body_sha256!=json_feed_pages.body_sha256
                            THEN excluded.fetched_at ELSE json_feed_pages.fetched_at END,
            checked_at=excluded.checked_at""",
            (feed.url, page_url, zlib.compress(body, 3), digest, etag, last_modified,
             next_url, total, policy, now, now))


def _touch_page(db, feed: JsonFeed, page_url: str, response, next_url: str | None,
                total: int | None, policy: str, now: float):
    """Refresh metadata after 304 without rewriting or recompressing the cached body."""
    old = _cached_page(db, feed, page_url)
    if old is None:
        raise ValueError("JSON collection returned 304 without a cached page")
    with db:
        db.execute("""UPDATE json_feed_pages SET etag=?,last_modified=?,next_url=?,total=?,policy=?,checked_at=?
            WHERE feed_url=? AND page_url=?""",
            (response.headers.get("etag") or old[2], response.headers.get("last-modified") or old[3],
             next_url, total, policy, now, feed.url, page_url))


def _replace_stage_page(db, feed: JsonFeed, page_url: str, articles: list[dict]):
    page_seen = set()
    for article in articles:
        url = article["url"]
        if url in page_seen:
            raise ValueError("JSON page contains a duplicate article URL")
        page_seen.add(url)
        other = db.execute("SELECT 1 FROM json_feed_stage WHERE feed_url=? AND article_url=? AND page_url!=?",
                           (feed.url, url, page_url)).fetchone()
        if other:
            raise ValueError("JSON collection repeats an article URL across pages")
    with db:
        db.execute("DELETE FROM json_feed_stage WHERE feed_url=? AND page_url=?", (feed.url, page_url))
        db.executemany("INSERT INTO json_feed_stage VALUES(?,?,?,?)",
            [(feed.url, page_url, row["url"], json.dumps(row, ensure_ascii=False, separators=(",", ":")))
             for row in articles])


def _publish(db_path: Path, feed: JsonFeed, db: sqlite3.Connection, limits: dict) -> int:
    count = db.execute("SELECT count(*) FROM json_feed_stage WHERE feed_url=?", (feed.url,)).fetchone()[0]
    if not count:
        raise ValueError("Complete JSON collection has no article records")
    max_articles = limits.get("max_pages", MAX_ARTICLES)
    if max_articles is not None and count > max_articles:
        raise ValueError("JSON collection exceeds the article limit")
    target = feed_db_path(db_path, feed)
    target.parent.mkdir(parents=True, exist_ok=True)
    # A stable staging filename gives import_snapshot a stable source identity;
    # remove it after the transaction so the published SQLite snapshot is the
    # only durable copy of article bodies.
    staging = target.with_suffix(".jsonl")
    temporary = staging.with_suffix(".jsonl.tmp")
    max_compressed = limits.get("max_compressed_bytes", 512 * 1024 * 1024)
    max_expanded = limits.get("max_expanded_bytes", 2 * 1024 * 1024 * 1024)
    written = 0
    try:
        with temporary.open("wb") as output:
            for row in db.execute("SELECT article_json FROM json_feed_stage WHERE feed_url=? ORDER BY article_url", (feed.url,)):
                line = (row[0] + "\n").encode("utf-8")
                written += len(line)
                if ((max_compressed is not None and written > max_compressed)
                        or (max_expanded is not None and written > max_expanded)):
                    raise ValueError("JSON collection exceeds configured snapshot size limits")
                output.write(line)
        os.replace(temporary, staging)
        import_snapshot(target, staging, max_compressed_bytes=max_compressed,
                        max_expanded_bytes=max_expanded, max_pages=max_articles)
    finally:
        temporary.unlink(missing_ok=True)
        staging.unlink(missing_ok=True)
    return count


def _purge_published(db_path: Path, feed: JsonFeed, *, removed=False) -> None:
    """Clear the published edition and flag editorial removals in the user's review queue."""
    target = feed_db_path(db_path, feed)
    if not target.is_file():
        return
    with closing(sqlite3.connect(target, timeout=5)) as db, db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if removed and {"snapshot_pages", "source_changes"} <= tables:
            db.execute("""INSERT OR REPLACE INTO source_changes
                SELECT url,strftime('%Y-%m-%dT%H:%M:%fZ','now'),'snapshot_page_removed' FROM snapshot_pages""")
        for table in ("snapshot_search", "snapshot_pages", "snapshot_sources"):
            if table in tables:
                db.execute(f'DELETE FROM "{table}"')
        if not removed and "source_changes" in tables:
            db.execute("DELETE FROM source_changes")


def refresh_feeds(db_path: Path | str, feeds, *, stop_event=None, clock=time.time, interval=REFRESH_INTERVAL) -> dict[str, dict]:
    """Refresh configured feeds independently; one bad endpoint does not stop the others."""
    path = Path(db_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    now = clock()
    if type(now) not in (int, float) or not math.isfinite(now):
        raise ValueError("clock must return a finite timestamp")
    if type(interval) not in (int, float) or not math.isfinite(interval) or interval <= 0:
        raise ValueError("Use a positive API update interval")
    feed_list = tuple(_normalize_feed(feed) for feed in feeds)
    results: dict[str, dict] = {}
    from .refresh import snapshot_limits
    limits = snapshot_limits()
    max_articles = limits.get("max_pages", MAX_ARTICLES)
    transport = lambda url, headers: web_sources._transport(url, headers, validate_url=_feed_url)
    with closing(_connect(path, create=True)) as db:
        contact = web_sources._Contact(db, transport, clock, stop_event)
        for feed in feed_list:
            if stop_event is not None and stop_event.is_set():
                results[feed.url] = {"status": "cancelled"}
                continue
            with db:
                _state_upsert(db, feed, now)
            state = db.execute("SELECT * FROM json_feed_state WHERE url=?", (feed.url,)).fetchone()
            if state["next_attempt"] > now:
                results[feed.url] = {"status": "not_due", "readable": bool(state["readable"])}
                continue
            try:
                origin_parts = urlsplit(feed.url)
                origin = urlunsplit(("https", origin_parts.netloc, "/", "", ""))
                rules = _robots(contact, origin, now)
                previous_run = db.execute("SELECT page_count,declared_total FROM json_feed_runs WHERE feed_url=?",
                                          (feed.url,)).fetchone()
                if previous_run and state["next_url"]:
                    pages, total = previous_run[0], previous_run[1]
                    current = state["next_url"]
                else:
                    pages, total, current = 0, None, feed.url
                    with db:
                        db.execute("DELETE FROM json_feed_stage WHERE feed_url=?", (feed.url,))
                        db.execute("DELETE FROM json_feed_visited WHERE feed_url=?", (feed.url,))
                        db.execute("INSERT OR REPLACE INTO json_feed_runs VALUES(?,?,?,?)",
                                   (feed.url, 0, None, now))
                visited: set[str] = set()
                pass_pages = 0
                last_status = None
                while current and pass_pages < MAX_PAGES_PER_PASS:
                    if stop_event is not None and stop_event.is_set():
                        raise corpus.RefreshCancelled()
                    if current in visited:
                        raise ValueError("JSON collection pagination contains a cycle")
                    if db.execute("SELECT 1 FROM json_feed_visited WHERE feed_url=? AND page_url=?",
                                  (feed.url, current)).fetchone():
                        raise ValueError("JSON collection continuation revisits a previously collected page")
                    if max_articles is not None and pages >= max_articles:
                        raise ValueError("Pagination exceeds the configured collection size")
                    visited.add(current)
                    cached = _cached_page(db, feed, current)
                    cached_body = _cached_body(cached)
                    request_headers = {}
                    if cached_body is not None:
                        if cached[2]:
                            request_headers["If-None-Match"] = cached[2]
                        if cached[3]:
                            request_headers["If-Modified-Since"] = cached[3]
                    response = _get(contact, current, rules, request_headers)
                    cached_policy = cached[6] if cached else ""
                    if _policy_denial(response.headers, cached_policy):
                        raise _PolicyDenied("response_policy_denied")
                    payload, raw_body = _feed_json(response, cached_body)
                    batch = [_normalized(item, feed) for item in _items(payload, feed)]
                    page_total = _extract_total(payload, feed, max_articles)
                    if total is not None and page_total is not None and total != page_total:
                        raise ValueError("JSON collection total changed during pagination")
                    if total is None:
                        total = page_total
                    next_url = _extract_next(payload, feed, current, response, cached)
                    _replace_stage_page(db, feed, current, batch)
                    policy_state = " ".join(response.headers.get(k, "") for k in
                                            ("content-signal", "x-robots-tag", "use")).strip()
                    if response.status == 304 and not policy_state:
                        policy_state = cached_policy
                    if response.status == 304:
                        _touch_page(db, feed, current, response, next_url, page_total, policy_state, now)
                    else:
                        _store_page(db, feed, current, raw_body, response, next_url, page_total,
                                    policy_state, now)
                    pages += 1
                    pass_pages += 1
                    last_status = response.status
                    with db:
                        db.execute("INSERT INTO json_feed_visited VALUES(?,?)", (feed.url, current))
                        db.execute("UPDATE json_feed_runs SET page_count=?,declared_total=? WHERE feed_url=?",
                                   (pages, total, feed.url))
                        db.execute("UPDATE json_feed_state SET status='building',next_url=?,next_attempt=0 WHERE url=?",
                                   (next_url, feed.url))
                    current = next_url
                if current:
                    # Partial progress is explicit and durable; it is never
                    # exposed as a completed replacement of the saved edition.
                    staged = db.execute("SELECT count(*) FROM json_feed_stage WHERE feed_url=?",
                                        (feed.url,)).fetchone()[0]
                    results[feed.url] = {"status": "building", "pages": pages, "articles": staged,
                                         "readable": bool(state["readable"])}
                    continue
                staged_count = db.execute("SELECT count(*) FROM json_feed_stage WHERE feed_url=?",
                                          (feed.url,)).fetchone()[0]
                if not staged_count and total != 0:
                    raise ValueError("Empty collection without a publisher-confirmed zero total")
                if max_articles is not None and staged_count > max_articles:
                    raise ValueError("JSON collection exceeds the configured article-count limit")
                if total is not None and total != staged_count:
                    raise ValueError("JSON collection total does not match its complete article set")
                articles = staged_count
                if articles:
                    _publish(path, feed, db, limits)
                else:
                    _purge_published(path, feed, removed=True)
                with db:
                    db.execute("""DELETE FROM json_feed_pages WHERE feed_url=? AND page_url NOT IN
                               (SELECT page_url FROM json_feed_visited WHERE feed_url=?)""", (feed.url, feed.url))
                    db.execute("DELETE FROM json_feed_visited WHERE feed_url=?", (feed.url,))
                    db.execute("DELETE FROM json_feed_stage WHERE feed_url=?", (feed.url,))
                    db.execute("DELETE FROM json_feed_runs WHERE feed_url=?", (feed.url,))
                    db.execute("""UPDATE json_feed_state SET status='ready',readable=1,last_success=?,
                        next_attempt=?,next_url=NULL,page_count=?,article_count=?,failure_count=0,
                        last_error=NULL,last_http_status=? WHERE url=?""",
                        (now, now + interval, pages, articles, last_status, feed.url))
                results[feed.url] = {"status": "ready", "articles": articles, "pages": pages,
                                     "readable": True}
            except _PolicyDenied as error:
                purge_error = None
                try:
                    _purge_published(path, feed)
                except Exception as purge_exception:
                    purge_error = type(purge_exception).__name__
                with db:
                    db.execute("DELETE FROM json_feed_stage WHERE feed_url=?", (feed.url,))
                    db.execute("DELETE FROM json_feed_runs WHERE feed_url=?", (feed.url,))
                    db.execute("DELETE FROM json_feed_pages WHERE feed_url=?", (feed.url,))
                    db.execute("UPDATE json_feed_state SET next_url=NULL WHERE url=?", (feed.url,))
                    result = _fail(db, feed, now, error, policy=True)
                if purge_error:
                    result["cache_purge_error_type"] = purge_error
                results[feed.url] = result
            except (corpus.RefreshCancelled, web_sources._Cancelled):
                results[feed.url] = {"status": "cancelled"}
            except Exception as error:
                with db:
                    if isinstance(error, ValueError):
                        # The publisher may have changed its collection between pages.
                        # Revalidate the first page instead of retrying a stale cursor.
                        db.execute("UPDATE json_feed_state SET next_url=NULL WHERE url=?", (feed.url,))
                    result = _fail(db, feed, now, error,
                                   http_status=getattr(error, "http_status", None))
                results[feed.url] = result
    return results
