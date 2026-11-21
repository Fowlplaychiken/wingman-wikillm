"""Incremental imports from the public Star Citizen Wiki Galactapedia API.

The API's original RSI links are a title inventory. Only non-placeholder
English translations are imported as article bodies. Page validators and
response data stay in the user's local wiki database.
"""

from __future__ import annotations

import hashlib
import gzip
import io
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from contextlib import closing
from datetime import UTC, datetime
from typing import Any

from . import corpus
from .snapshots import MAX_PAGE_CHARS, _source_id, import_snapshot

API_URL = "https://api.star-citizen.wiki/api/galactapedia"
API_HOST = "api.star-citizen.wiki"
RSI_ORIGIN = "https://robertsspaceindustries.com"
PAGE_SIZE = 100
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_RESPONSE_BYTES = 64 * 1024 * 1024
DEFAULT_INTERVAL = 24 * 60 * 60
DEFAULT_FAILURE_BACKOFF = 5 * 60
MAX_FAILURE_BACKOFF = 24 * 60 * 60
REQUEST_TIMEOUT = 20
MIN_REQUEST_INTERVAL = 1.0
ROBOTS_INTERVAL = 24 * 60 * 60
IMPORT_FILENAME = ".galactapedia-community-api.jsonl"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS galactapedia_feed_state (
    id INTEGER PRIMARY KEY CHECK (id=1),
    last_attempt REAL,
    last_success REAL,
    next_attempt REAL NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    last_http_status INTEGER,
    page_count INTEGER NOT NULL DEFAULT 0,
    total_titles INTEGER NOT NULL DEFAULT 0,
    article_bodies INTEGER NOT NULL DEFAULT 0,
    retained_prior_bodies INTEGER NOT NULL DEFAULT 0,
    placeholders INTEGER NOT NULL DEFAULT 0,
    missing INTEGER NOT NULL DEFAULT 0,
    discovery_time TEXT,
    source_id TEXT,
    content_sha256 TEXT
);
CREATE TABLE IF NOT EXISTS galactapedia_feed_pages (
    page_number INTEGER PRIMARY KEY,
    etag TEXT,
    last_modified TEXT,
    body_zlib BLOB NOT NULL,
    body_sha256 TEXT NOT NULL,
    item_count INTEGER NOT NULL,
    fetched_at REAL NOT NULL,
    checked_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS galactapedia_feed_gaps (
    url TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    reason TEXT NOT NULL CHECK (reason IN ('placeholder','missing')),
    retained_prior_body INTEGER NOT NULL CHECK (retained_prior_body IN (0,1)),
    discovered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS galactapedia_feed_robots (
    id INTEGER PRIMARY KEY CHECK (id=1),
    body TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    crawl_delay REAL NOT NULL
);
"""


class GalactapediaFeedError(ValueError):
    """A malformed, incomplete, or unavailable feed pass."""

    def __init__(self, message: str, *, http_status: int | None = None,
                 retry_at: float | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.retry_at = retry_at


class _SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    """Reject a redirect before urllib sends the request to a new origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = urllib.parse.urlsplit(newurl)
        if (parts.scheme != "https" or parts.hostname != API_HOST
                or parts.port not in (None, 443)):
            raise GalactapediaFeedError("API redirect left the configured HTTPS origin")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _RequestPacer:
    def __init__(self, interval: float, stop_event=None) -> None:
        self.interval, self.stop_event = interval, stop_event
        self.last_started: float | None = None

    def before_request(self) -> None:
        _cancelled(self.stop_event)
        if self.last_started is None:
            return
        delay = max(0, self.interval - (time.monotonic() - self.last_started))
        if delay:
            if self.stop_event is not None:
                if self.stop_event.wait(delay):
                    raise corpus.RefreshCancelled()
            else:
                time.sleep(delay)
        _cancelled(self.stop_event)

    def started(self) -> None:
        self.last_started = time.monotonic()


def _now_iso(clock) -> str:
    stamp = clock()
    if type(stamp) not in (int, float) or not math.isfinite(stamp):
        raise ValueError("clock must return a finite Unix timestamp")
    return datetime.fromtimestamp(stamp, UTC).isoformat(timespec="seconds")


def _connect(db_path: Path | str, *, create: bool = True) -> sqlite3.Connection:
    path = Path(db_path).expanduser()
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path, timeout=10)
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(corpus.SCHEMA + _SCHEMA)
        db.commit()
        return db
    uri = path.resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=3)


def _has_state(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        with closing(_connect(path, create=False)) as db:
            return db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='galactapedia_feed_state'"
            ).fetchone() is not None
    except sqlite3.Error:
        return False


def feed_status(db_path: Path | str, *, clock=time.time) -> dict[str, Any]:
    """Return offline feed and gap counts without creating or changing a DB."""
    path = Path(db_path).expanduser()
    empty = {
        "status": "not_started", "titles": 0, "article_bodies": 0,
        "retained_prior_bodies": 0, "placeholders": 0, "missing": 0,
        "gaps": {"placeholder": 0, "missing": 0}, "discovery_time": None,
        "last_attempt": None, "last_success": None, "next_attempt": None,
        "last_error": None, "last_http_status": None, "failure_count": 0,
        "pages": 0, "source_id": None,
    }
    if not _has_state(path):
        return empty
    try:
        with closing(_connect(path, create=False)) as db:
            row = db.execute("""SELECT last_attempt,last_success,next_attempt,failure_count,
                last_error,last_http_status,page_count,total_titles,article_bodies,
                retained_prior_bodies,placeholders,missing,discovery_time,source_id
                FROM galactapedia_feed_state WHERE id=1""").fetchone()
            if row is None:
                return empty
            gap_counts = {reason: count for reason, count in db.execute(
                "SELECT reason,count(*) FROM galactapedia_feed_gaps GROUP BY reason")}
    except sqlite3.Error:
        return {**empty, "status": "unavailable"}
    now = clock()
    (attempt, success, next_attempt, failures, error, http_status, pages,
     titles, bodies, retained, placeholders, missing, discovered, source_id) = row
    if error:
        state = "error"
    elif success is None:
        state = "not_started"
    elif next_attempt and next_attempt > now:
        state = "backoff"
    elif now >= success + DEFAULT_INTERVAL:
        state = "due"
    else:
        state = "current"
    return {
        "status": state, "titles": titles, "article_bodies": bodies,
        "retained_prior_bodies": retained, "placeholders": placeholders,
        "missing": missing, "gaps": {"placeholder": gap_counts.get("placeholder", 0),
                                       "missing": gap_counts.get("missing", 0)},
        "discovery_time": discovered, "last_attempt": attempt, "last_success": success,
        "next_attempt": next_attempt, "last_error": error,
        "last_http_status": http_status, "failure_count": failures,
        "pages": pages, "source_id": source_id,
    }


def _cancelled(stop_event) -> None:
    if stop_event is not None and stop_event.is_set():
        raise corpus.RefreshCancelled()


def _decompress_cached(value: bytes) -> bytes:
    decoder = zlib.decompressobj()
    body = decoder.decompress(value, MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES or decoder.unconsumed_tail or not decoder.eof:
        raise GalactapediaFeedError("cached API page is corrupt or exceeds its limit")
    body += decoder.flush(MAX_RESPONSE_BYTES + 1 - len(body))
    if len(body) > MAX_RESPONSE_BYTES:
        raise GalactapediaFeedError("cached API page exceeds its limit")
    return body


def _cached_pages(db: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    result = {}
    for number, etag, modified, compressed, digest, item_count, fetched, checked in db.execute(
        "SELECT page_number,etag,last_modified,body_zlib,body_sha256,item_count,fetched_at,checked_at "
        "FROM galactapedia_feed_pages"):
        try:
            raw = _decompress_cached(compressed)
            if hashlib.sha256(raw).hexdigest() != digest:
                continue
            result[number] = {"etag": etag, "last_modified": modified, "raw": raw,
                              "item_count": item_count, "fetched_at": fetched, "checked_at": checked}
        except (GalactapediaFeedError, zlib.error, TypeError):
            continue
    return result


def _page_url(page: int) -> str:
    query = urllib.parse.urlencode({"page[size]": PAGE_SIZE, "page[number]": page})
    return API_URL + "?" + query


def _read_response(response) -> tuple[int, dict[str, str], bytes, int]:
    status = getattr(response, "status", None) or getattr(response, "code", 200)
    headers = {str(k).casefold(): str(v) for k, v in response.headers.items()}
    final = urllib.parse.urlsplit(response.geturl())
    if final.scheme != "https" or final.hostname != API_HOST:
        raise GalactapediaFeedError("API redirected outside its public origin")
    if status == 304:
        return status, headers, b"", 0
    raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise GalactapediaFeedError("API page exceeds the response size limit")
    wire_bytes = len(raw)
    if headers.get("content-encoding", "").casefold() == "gzip":
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as archive:
                raw = archive.read(MAX_RESPONSE_BYTES + 1)
        except OSError as error:
            raise GalactapediaFeedError("API page gzip body is invalid") from error
        if len(raw) > MAX_RESPONSE_BYTES:
            raise GalactapediaFeedError("expanded API page exceeds the response size limit")
    return status, headers, raw, wire_bytes


def _request_page(page: int, cached: dict[str, Any] | None, *, opener, stop_event,
                  clock, robots: str, pacer: _RequestPacer
                  ) -> tuple[bytes, str | None, str | None, bool, float, int]:
    _cancelled(stop_event)
    url = _page_url(page)
    parsed = urllib.parse.urlsplit(url)
    path = parsed.path + ("?" + parsed.query if parsed.query else "")
    if not corpus.allowed_by_robots(robots, path):
        raise GalactapediaFeedError("API robots.txt does not allow the requested page")
    headers = {"User-Agent": corpus.AGENT, "Accept": "application/json"}
    if cached:
        if cached.get("etag"):
            headers["If-None-Match"] = cached["etag"]
        if cached.get("last_modified"):
            headers["If-Modified-Since"] = cached["last_modified"]
    request = urllib.request.Request(url, headers=headers)
    try:
        pacer.before_request()
        pacer.started()
        response = opener.open(request, timeout=REQUEST_TIMEOUT)
    except urllib.error.HTTPError as error:
        if error.code != 304:
            retry_at = None
            if error.code == 429:
                retry_after = error.headers.get("Retry-After") if error.headers else None
                retry_at = corpus.retry_after(retry_after, clock())
            error.close()
            raise GalactapediaFeedError("API page request failed", http_status=error.code,
                                       retry_at=retry_at) from None
        response = error
    try:
        status, response_headers, raw, wire_bytes = _read_response(response)
    finally:
        response.close()
    _cancelled(stop_event)
    if status == 304:
        if not cached:
            raise GalactapediaFeedError("API returned 304 without a validated cached page")
        return (cached["raw"], response_headers.get("etag") or cached.get("etag"),
                response_headers.get("last-modified") or cached.get("last_modified"), False,
                cached.get("fetched_at") or clock(), 0)
    if status != 200:
        raise GalactapediaFeedError("API returned an unexpected HTTP status", http_status=status)
    if not raw:
        raise GalactapediaFeedError("API returned an empty page body")
    return raw, response_headers.get("etag"), response_headers.get("last-modified"), True, clock(), wire_bytes


def _get_robots(db_path: Path, *, opener, stop_event, clock,
                pacer: _RequestPacer) -> tuple[str, float, int]:
    now = clock()
    with closing(_connect(db_path)) as db:
        row = db.execute(
            "SELECT body,fetched_at,crawl_delay FROM galactapedia_feed_robots WHERE id=1"
        ).fetchone()
    if row and now - row[1] < ROBOTS_INTERVAL:
        return row[0], max(MIN_REQUEST_INTERVAL, row[2]), 0
    url = f"https://{API_HOST}/robots.txt"
    request = urllib.request.Request(url, headers={"User-Agent": corpus.AGENT, "Accept": "text/plain"})
    _cancelled(stop_event)
    try:
        pacer.before_request()
        pacer.started()
        response = opener.open(request, timeout=REQUEST_TIMEOUT)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            body, wire_bytes = "", 0
            error.close()
        else:
            retry_after = error.headers.get("Retry-After") if error.headers else None
            retry_at = corpus.retry_after(retry_after, clock()) if error.code == 429 else None
            error.close()
            raise GalactapediaFeedError("API robots.txt request failed", http_status=error.code,
                                       retry_at=retry_at) from None
    else:
        try:
            status, _, raw, wire_bytes = _read_response(response)
        finally:
            response.close()
        if status != 200:
            raise GalactapediaFeedError("API robots.txt returned an unexpected status", http_status=status)
        if len(raw) > 256_000:
            raise GalactapediaFeedError("API robots.txt exceeds its size limit")
        try:
            body = raw.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise GalactapediaFeedError("API robots.txt is not UTF-8") from error
    _cancelled(stop_event)
    delay = max(MIN_REQUEST_INTERVAL, corpus.robots_delay(body, floor=MIN_REQUEST_INTERVAL))
    # Check this fixed public feed URL before any catalog request is sent.
    page = urllib.parse.urlsplit(_page_url(1))
    page_path = page.path + ("?" + page.query if page.query else "")
    if not corpus.allowed_by_robots(body, page_path):
        raise GalactapediaFeedError("API robots.txt disallows the Galactapedia feed")
    with closing(_connect(db_path)) as db, db:
        db.execute("""INSERT INTO galactapedia_feed_robots(id,body,fetched_at,crawl_delay)
            VALUES(1,?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body,
            fetched_at=excluded.fetched_at,crawl_delay=excluded.crawl_delay""",
            (body, now, delay))
    return body, delay, wire_bytes


def _payload(raw: bytes, page: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GalactapediaFeedError(f"API page {page} is not valid UTF-8 JSON") from error
    if not isinstance(value, dict) or not isinstance(value.get("data"), list):
        raise GalactapediaFeedError(f"API page {page} has no data list")
    meta = value.get("meta")
    if not isinstance(meta, dict):
        raise GalactapediaFeedError(f"API page {page} has no pagination metadata")
    return value["data"], meta


def _identity(item: object) -> tuple[str, str, str]:
    if not isinstance(item, dict):
        raise GalactapediaFeedError("API article row is not an object")
    ident, title, slug, link = item.get("id"), item.get("title"), item.get("slug"), item.get("rsi_url")
    if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9]{10}", ident):
        raise GalactapediaFeedError("API article has an invalid ID")
    if not isinstance(slug, str) or not re.fullmatch(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", slug):
        raise GalactapediaFeedError("API article has an invalid slug")
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 500:
        raise GalactapediaFeedError("API article has an invalid title")
    if not isinstance(link, str) or len(link) > 1024 or not link.startswith("/galactapedia/article/"):
        raise GalactapediaFeedError("API article has no original RSI article link")
    parsed = urllib.parse.urlsplit(link)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or "\\" in link:
        raise GalactapediaFeedError("API article link must be an original RSI path")
    tail = parsed.path.rsplit("/", 1)[-1]
    if not tail.startswith(ident + "-") or not corpus.indexable(RSI_ORIGIN + parsed.path):
        raise GalactapediaFeedError("API article link does not match its RSI ID")
    return ident, title.strip(), RSI_ORIGIN + parsed.path


def _translation(item: dict[str, Any]) -> tuple[str | None, str | None]:
    translations = item.get("translations")
    if translations is None:
        return None, "missing"
    if not isinstance(translations, dict):
        raise GalactapediaFeedError("API article translations are malformed")
    text = translations.get("en_EN")
    if text is None or text == "" or (isinstance(text, str) and not text.strip()):
        return None, "missing"
    if not isinstance(text, str) or len(text) > MAX_PAGE_CHARS:
        raise GalactapediaFeedError("API English translation is malformed or oversized")
    stripped = text.strip()
    if re.match(r"(?is)^(?:pending review\b|under review\b|this (?:article|entry) is (?:pending|under review)\b)", stripped):
        return None, "placeholder"
    return text, None


def _validate_pages(pages: dict[int, dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, str], list[dict[str, str]], int, int]:
    if not pages:
        raise GalactapediaFeedError("API returned no pages")
    first_rows, first_meta = _payload(pages[1]["raw"], 1)
    total, last_page, per_page = first_meta.get("total"), first_meta.get("last_page"), first_meta.get("per_page")
    if type(total) is not int or total < 1:
        raise GalactapediaFeedError("API total must be a positive integer")
    if type(last_page) is not int or last_page < 1:
        raise GalactapediaFeedError("API last_page is invalid")
    if type(per_page) is not int or per_page != PAGE_SIZE:
        raise GalactapediaFeedError("API did not honor the requested page size")
    if last_page != math.ceil(total / PAGE_SIZE) or set(pages) != set(range(1, last_page + 1)):
        raise GalactapediaFeedError("API pagination is incomplete or inconsistent")
    records: dict[str, str] = {}
    articles: list[dict[str, Any]] = []
    gaps: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    body_count = 0
    for page_number in range(1, last_page + 1):
        rows, meta = _payload(pages[page_number]["raw"], page_number)
        if (meta.get("total") != total or meta.get("last_page") != last_page
                or type(meta.get("last_page")) is not int
                or type(meta.get("per_page")) is not int
                or type(meta.get("current_page")) is not int
                or meta.get("per_page") != per_page or meta.get("current_page") != page_number):
            raise GalactapediaFeedError("API pagination changed during the pass")
        expected = min(PAGE_SIZE, total - (page_number - 1) * PAGE_SIZE)
        if len(rows) != expected:
            raise GalactapediaFeedError("API page is short or exceeds its declared total")
        for item in rows:
            ident, title, url = _identity(item)
            if url in records or ident in seen_ids:
                raise GalactapediaFeedError("API returned a duplicate original article link")
            records[url] = title
            seen_ids.add(ident)
            text, gap_reason = _translation(item)
            if gap_reason:
                gaps.append({"url": url, "title": title, "reason": gap_reason})
            else:
                articles.append({"url": url, "title": title, "text": text,
                                 "revision": None, "revision_timestamp": None})
                body_count += 1
    if len(records) != total:
        raise GalactapediaFeedError("API distinct original-link count did not match total")
    return articles, records, gaps, last_page, body_count


def _prior_bodies(db_path: Path, source_id: str, gaps: list[dict[str, str]]) -> list[dict[str, Any]]:
    if not db_path.is_file() or not gaps:
        return []
    urls = [row["url"] for row in gaps]
    placeholders = ",".join("?" for _ in urls)
    try:
        with closing(_connect(db_path, create=False)) as db:
            rows = db.execute(f"""SELECT url,title,body,revision,revision_timestamp,game_build
                FROM snapshot_pages WHERE source_id=? AND url IN ({placeholders})""",
                (source_id, *urls)).fetchall()
    except sqlite3.Error:
        return []
    old = {row[0]: row for row in rows}
    retained = []
    for gap in gaps:
        row = old.get(gap["url"])
        if row and isinstance(row[2], str) and row[2].strip():
            retained.append({"url": row[0], "title": gap["title"], "text": row[2],
                             "revision": row[3], "revision_timestamp": row[4],
                             "game_build": row[5]})
            gap["retained_prior_body"] = True
        else:
            gap["retained_prior_body"] = False
    return retained


def _corpus_verified(db_path: Path, source_id: str, expected_pages: int) -> bool:
    if not db_path.is_file():
        return expected_pages == 0
    try:
        with closing(_connect(db_path, create=False)) as db:
            source = db.execute(
                "SELECT file_format,page_count FROM snapshot_sources WHERE source_id=?",
                (source_id,),
            ).fetchone()
            pages = db.execute(
                "SELECT count(*) FROM snapshot_pages WHERE source_id=?", (source_id,)
            ).fetchone()[0]
            indexed = db.execute(
                "SELECT count(*) FROM snapshot_search WHERE source_id=?", (source_id,)
            ).fetchone()[0]
    except sqlite3.Error:
        return False
    if expected_pages == 0:
        return source is None and pages == 0 and indexed == 0
    return bool(source and source[0] == "galactapedia-community-api"
                and source[1] == expected_pages and pages == expected_pages
                and indexed == expected_pages)


def _publish_pass(db: sqlite3.Connection, source_id: str,
                  pages: dict[int, dict[str, Any]], records: dict[str, str],
                  gaps: list[dict[str, str]], *, now: float, observed_at: str,
                  article_bodies: int, retained_count: int, digest: str) -> None:
    """Commit feed metadata, catalog inventory, gaps and provenance with pages."""
    db.execute("UPDATE snapshot_sources SET file_format='galactapedia-community-api' WHERE source_id=?",
               (source_id,))
    corpus.apply_catalog(db, "galactapedia", records, observed_at)
    page_count = len(pages)
    db.execute("DELETE FROM galactapedia_feed_pages WHERE page_number>?", (page_count,))
    for number, page in pages.items():
        raw = page["raw"]
        if page.get("changed"):
            db.execute("""INSERT INTO galactapedia_feed_pages(page_number,etag,last_modified,
                body_zlib,body_sha256,item_count,fetched_at,checked_at) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(page_number) DO UPDATE SET etag=excluded.etag,
                  last_modified=excluded.last_modified,body_zlib=excluded.body_zlib,
                  body_sha256=excluded.body_sha256,item_count=excluded.item_count,
                  fetched_at=excluded.fetched_at,checked_at=excluded.checked_at""",
                (number, page.get("etag"), page.get("last_modified"), zlib.compress(raw, 6),
                 hashlib.sha256(raw).hexdigest(), page["item_count"], page["fetched_at"], now))
        else:
            db.execute("""UPDATE galactapedia_feed_pages SET etag=coalesce(?,etag),
                last_modified=coalesce(?,last_modified),checked_at=? WHERE page_number=?""",
                (page.get("etag"), page.get("last_modified"), now, number))
    db.execute("DELETE FROM galactapedia_feed_gaps")
    db.executemany("""INSERT INTO galactapedia_feed_gaps(url,title,reason,retained_prior_body,discovered_at)
        VALUES(?,?,?,?,?)""", ((gap["url"], gap["title"], gap["reason"],
            int(gap.get("retained_prior_body", False)), observed_at) for gap in gaps))
    placeholders = sum(gap["reason"] == "placeholder" for gap in gaps)
    missing = sum(gap["reason"] == "missing" for gap in gaps)
    db.execute("""INSERT INTO galactapedia_feed_state(id,last_attempt,last_success,next_attempt,
        failure_count,last_error,last_http_status,page_count,total_titles,article_bodies,
        retained_prior_bodies,placeholders,missing,discovery_time,source_id,content_sha256)
        VALUES(1,?,?,0,0,NULL,NULL,?,?,?,?,?,?,?, ?,?)
        ON CONFLICT(id) DO UPDATE SET last_attempt=excluded.last_attempt,
          last_success=excluded.last_success,next_attempt=0,failure_count=0,last_error=NULL,
          last_http_status=NULL,page_count=excluded.page_count,total_titles=excluded.total_titles,
          article_bodies=excluded.article_bodies,retained_prior_bodies=excluded.retained_prior_bodies,
          placeholders=excluded.placeholders,missing=excluded.missing,
          discovery_time=excluded.discovery_time,source_id=excluded.source_id,
          content_sha256=excluded.content_sha256""",
        (now, now, page_count, len(records), article_bodies, retained_count,
         placeholders, missing, observed_at, source_id, digest))


def _finish_unchanged(db_path: Path, *, now: float, page_count: int,
                      discovery_time: str) -> None:
    """Advance freshness after a fully verified all-304 pass without FTS/blob writes."""
    with closing(_connect(db_path)) as db, db:
        db.execute("UPDATE galactapedia_feed_pages SET checked_at=? WHERE page_number<=?", (now, page_count))
        db.execute("""UPDATE galactapedia_feed_state SET last_attempt=?,last_success=?,next_attempt=0,
            failure_count=0,last_error=NULL,last_http_status=NULL,discovery_time=? WHERE id=1""",
            (now, now, discovery_time))


def _write_import_file(db_path: Path, articles: list[dict[str, Any]]) -> Path:
    target = db_path.parent / IMPORT_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".galactapedia-", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            for article in articles:
                stream.write(json.dumps(article, ensure_ascii=False, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, target)
    except Exception:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise
    return target


def _clear_snapshot_source(db: sqlite3.Connection, source_id: str, observed_at: str) -> None:
    """Atomically remove a formerly populated source after a complete all-gap feed."""
    from . import snapshots
    snapshots._ensure_schema(db)
    urls = [row[0] for row in db.execute(
        "SELECT url FROM snapshot_pages WHERE source_id=?", (source_id,))]
    for url in urls:
        corpus.record_source_change(db, url, observed_at, "snapshot_page_removed")
    db.execute("DELETE FROM snapshot_search WHERE source_id=?", (source_id,))
    db.execute("DELETE FROM snapshot_pages WHERE source_id=?", (source_id,))
    db.execute("DELETE FROM snapshot_sources WHERE source_id=?", (source_id,))


def _record_failure(db_path: Path, *, now: float, error: Exception,
                    base_backoff: float) -> dict[str, Any]:
    with closing(_connect(db_path)) as db, db:
        row = db.execute("SELECT failure_count FROM galactapedia_feed_state WHERE id=1").fetchone()
        failures = (row[0] if row else 0) + 1
        delay = min(MAX_FAILURE_BACKOFF, base_backoff * (2 ** min(failures - 1, 12)))
        retry_at = getattr(error, "retry_at", None)
        if retry_at is not None:
            delay = max(delay, retry_at - now)
        next_attempt = now + max(base_backoff, delay)
        status = getattr(error, "http_status", None)
        db.execute("""INSERT INTO galactapedia_feed_state(id,last_attempt,next_attempt,
            failure_count,last_error,last_http_status) VALUES(1,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET last_attempt=excluded.last_attempt,
              next_attempt=excluded.next_attempt,failure_count=excluded.failure_count,
              last_error=excluded.last_error,last_http_status=excluded.last_http_status""",
            (now, next_attempt, failures, type(error).__name__, status))
    return {"status": "failed", "error_type": type(error).__name__,
            "http_status": status, "failure_count": failures, "next_attempt": next_attempt}


def _conditional_requests(db_path: Path, stop_event, *, opener, clock, article_limit, byte_limit,
                          robots: str, pacer: _RequestPacer
                          ) -> tuple[dict[int, dict[str, Any]], int, int, int]:
    with closing(_connect(db_path)) as db:
        cached = _cached_pages(db)
    if not cached:
        first_raw, etag, modified, changed, fetched, wire_bytes = _request_page(
            1, None, opener=opener, stop_event=stop_event, clock=clock,
            robots=robots, pacer=pacer)
        pages: dict[int, dict[str, Any]] = {1: {"raw": first_raw, "etag": etag,
            "last_modified": modified, "fetched_at": fetched, "item_count": 0, "changed": changed}}
        bodies = int(changed)
    else:
        first_raw, etag, modified, changed, fetched, wire_bytes = _request_page(
            1, cached.get(1), opener=opener, stop_event=stop_event, clock=clock,
            robots=robots, pacer=pacer)
        pages = {1: {"raw": first_raw, "etag": etag, "last_modified": modified,
                     "fetched_at": fetched, "item_count": 0, "changed": changed}}
        bodies = int(changed)
    _, meta = _payload(pages[1]["raw"], 1)
    total, last_page = meta.get("total"), meta.get("last_page")
    if (type(total) is not int or total < 1 or type(last_page) is not int
            or last_page < 1 or last_page != math.ceil(total / PAGE_SIZE)):
        raise GalactapediaFeedError("API page 1 declares an invalid total")
    if article_limit is not None and total > article_limit:
        raise GalactapediaFeedError("Galactapedia exceeds WIKILLM_SNAPSHOT_MAX_PAGES")
    response_bytes = wire_bytes
    total_bytes = len(first_raw)
    if byte_limit is not None and total_bytes > byte_limit:
        raise GalactapediaFeedError("Galactapedia exceeds WIKILLM_SNAPSHOT_MAX_EXPANDED_BYTES")
    for page_number in range(2, last_page + 1):
        _cancelled(stop_event)
        previous = cached.get(page_number)
        raw, etag, modified, changed, fetched, wire_bytes = _request_page(
            page_number, previous, opener=opener, stop_event=stop_event, clock=clock,
            robots=robots, pacer=pacer)
        response_bytes += wire_bytes
        total_bytes += len(raw)
        if byte_limit is not None and total_bytes > byte_limit:
            raise GalactapediaFeedError("Galactapedia exceeds WIKILLM_SNAPSHOT_MAX_EXPANDED_BYTES")
        rows, _ = _payload(raw, page_number)
        pages[page_number] = {"raw": raw, "etag": etag, "last_modified": modified,
                              "fetched_at": fetched, "item_count": len(rows), "changed": changed}
        bodies += int(changed)
    if 1 in pages:
        rows, _ = _payload(pages[1]["raw"], 1)
        pages[1]["item_count"] = len(rows)
    return pages, bodies, total_bytes, response_bytes


def refresh_galactapedia(db_path: Path | str, *, stop_event=None, clock=time.time,
                         interval: float = DEFAULT_INTERVAL,
                         failure_backoff: float = DEFAULT_FAILURE_BACKOFF,
                         opener=None) -> dict[str, Any]:
    """Refresh the full paginated feed, reusing cached pages on HTTP 304.

    The title inventory and body snapshot are published only after all declared
    pages, counts, original links and translations have passed validation.
    Missing/placeholder translations are explicit gaps; a prior full body for
    the same still-present URL is retained and counted separately.
    """
    if type(interval) not in (int, float) or not 60 <= interval <= 31_536_000:
        raise ValueError("interval must be 60 seconds..1 year")
    if type(failure_backoff) not in (int, float) or not 60 <= failure_backoff <= 86_400:
        raise ValueError("failure_backoff must be 60 seconds..1 day")
    if stop_event is not None and stop_event.is_set():
        return {"status": "cancelled", "pages_checked": 0, "bodies_downloaded": 0}
    path = Path(db_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    now = clock()
    if type(now) not in (int, float) or not math.isfinite(now):
        raise ValueError("clock must return a finite Unix timestamp")
    opener = opener or urllib.request.build_opener(_SameOriginRedirect())
    try:
        with closing(_connect(path)) as db:
            state = db.execute("SELECT last_success,next_attempt FROM galactapedia_feed_state WHERE id=1").fetchone()
        if state:
            success, retry_at = state
            if retry_at and retry_at > now:
                return {"status": "backoff", "next_attempt": retry_at,
                        "pages_checked": 0, "bodies_downloaded": 0}
            if success is not None and now < success + interval:
                return {"status": "not_due", "next_attempt": success + interval,
                        "pages_checked": 0, "bodies_downloaded": 0}
        with closing(_connect(path)) as db, db:
            db.execute("""INSERT INTO galactapedia_feed_state(id,last_attempt,next_attempt)
                VALUES(1,?,0) ON CONFLICT(id) DO UPDATE SET last_attempt=excluded.last_attempt""", (now,))
        pacer = _RequestPacer(MIN_REQUEST_INTERVAL, stop_event)
        robots, crawl_delay, robots_bytes = _get_robots(
            path, opener=opener, stop_event=stop_event, clock=clock, pacer=pacer)
        pacer.interval = max(MIN_REQUEST_INTERVAL, crawl_delay)
        from .refresh import snapshot_limits
        limits = snapshot_limits()
        pages, downloaded, _, page_response_bytes = _conditional_requests(
            path, stop_event, opener=opener, clock=clock, robots=robots, pacer=pacer,
            article_limit=limits.get("max_pages"), byte_limit=limits.get("max_expanded_bytes", MAX_TOTAL_RESPONSE_BYTES))
        articles, records, gaps, page_count, article_bodies = _validate_pages(pages)
        _cancelled(stop_event)
        source_id = _source_id(path.parent / IMPORT_FILENAME)
        retained = _prior_bodies(path, source_id, gaps)
        import_rows = articles + retained
        retained_count = len(retained)
        observed_at = _now_iso(clock)
        digest = hashlib.sha256(b"\n".join(pages[n]["raw"] for n in range(1, page_count + 1))).hexdigest()
        if downloaded == 0 and _corpus_verified(path, source_id, article_bodies + retained_count):
            _finish_unchanged(path, now=now, page_count=page_count, discovery_time=observed_at)
            import_result = {"status": "unchanged", "source_id": source_id,
                             "page_count": article_bodies + retained_count}
        elif import_rows:
            staged_path = _write_import_file(path, import_rows)

            def publish(db, published_source_id):
                _publish_pass(db, published_source_id, pages, records, gaps,
                              now=now, observed_at=observed_at,
                              article_bodies=article_bodies, retained_count=retained_count,
                              digest=digest)

            try:
                import_result = import_snapshot(path, staged_path, on_publish=publish, **limits)
            finally:
                try:
                    staged_path.unlink(missing_ok=True)
                except OSError:
                    pass
        else:
            from . import snapshots
            with closing(_connect(path)) as db, db:
                snapshots._ensure_schema(db)
                _clear_snapshot_source(db, source_id, observed_at)
                _publish_pass(db, source_id, pages, records, gaps,
                              now=now, observed_at=observed_at,
                              article_bodies=article_bodies, retained_count=retained_count,
                              digest=digest)
            import_result = {"status": "empty", "source_id": source_id, "page_count": 0}
        return {"status": "success", "pages_checked": page_count,
                "bodies_downloaded": downloaded, "response_bytes": page_response_bytes + robots_bytes,
                "titles": len(records), "article_bodies": article_bodies,
                "retained_prior_bodies": retained_count,
                "placeholders": sum(g["reason"] == "placeholder" for g in gaps),
                "missing": sum(g["reason"] == "missing" for g in gaps),
                "discovery_time": observed_at, "source_id": source_id,
                "snapshot": import_result}
    except corpus.RefreshCancelled:
        return {"status": "cancelled", "pages_checked": 0, "bodies_downloaded": 0}
    except Exception as error:
        failure = _record_failure(path, now=clock(), error=error, base_backoff=failure_backoff)
        return {**failure, "pages_checked": 0, "bodies_downloaded": 0}
