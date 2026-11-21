"""Local-only import and retrieval of publisher-authorized wiki snapshots.

Snapshots are files supplied by the user. This module never downloads a URL,
executes imported text, or writes to the field-note or refresh tables.
"""

from __future__ import annotations

import hashlib
import bz2
import gzip
from html.parser import HTMLParser
import ipaddress
from datetime import datetime, timezone
from collections import Counter
from contextlib import ExitStack, closing
import io
import json
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit
import xml.etree.ElementTree as ET
import zlib

from . import sc_archive

EDITION_FILENAME = "starcitizen.tools-20230930-articles.jsonl.gz"
EDITION_SHA256 = "ca221b01fbab2d8968b259525457006e560d1c6e06460e3d2998ee2fc2661ead"


def bundled_edition():
    path = Path(__file__).resolve().parents[2] / "editions" / EDITION_FILENAME
    return path if path.is_file() else None


# Defaults are sized for a normal personal wiki export. Callers importing a
# larger publisher-authorized dump can raise either ceiling or pass None to
# disable that aggregate ceiling. Per-record ceilings remain enforced.
DEFAULT_MAX_COMPRESSED_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_MAX_PAGES = 2_000_000
MAX_PAGE_CHARS = 2_000_000
MAX_JSONL_LINE_BYTES = 4 * 1024 * 1024
MAX_QUERY_BYTES = 512
MAX_QUERY_TERMS = 24
MAX_RESULTS = 50
PAGE_CHUNK_CHARS = 6_000
MAX_LINKS = 500

_STOP_WORDS = frozenset({
    "about", "after", "also", "and", "are", "does", "for", "from", "how",
    "into", "its", "mean", "more", "that", "the", "their", "them", "then",
    "there", "these", "this", "those", "what", "when", "where", "which", "with",
})
_WORD = re.compile(r"(?:[0-9]+(?:\.[0-9]+){1,3}|[a-zA-Z][a-zA-Z0-9-]{1,})")
_WIKILINK = re.compile(r"\[\[([^]|#]+)(?:#[^]|]*)?(?:\|[^]]*)?\]\]")
_MARKDOWN_LINK = re.compile(r"\[[^\]]{1,500}\]\((https?://[^\s)<>]{1,2048})\)")
_EXTERNAL_WIKI_LINK = re.compile(r"\[(https?://[^\s\]]{1,2048})(?:\s+[^\]]*)?\]")
_FORBIDDEN_XML = re.compile(br"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_RESERVED_HOST_SUFFIXES = (".example", ".invalid", ".test", ".localhost", ".local", ".internal", ".onion")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshot_sources (
    source_id TEXT PRIMARY KEY,
    file_name TEXT NOT NULL,
    file_format TEXT NOT NULL,
    source_origin TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    page_count INTEGER NOT NULL,
    imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_changes (
    url TEXT PRIMARY KEY, observed_at TEXT NOT NULL, reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshot_pages (
    source_id TEXT NOT NULL,
    page_key TEXT NOT NULL,
    url TEXT NOT NULL,
    source_origin TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    revision TEXT,
    revision_timestamp TEXT,
    namespace INTEGER,
    namespace_name TEXT,
    redirect_target TEXT,
    game_build TEXT,
    links_json TEXT NOT NULL,
    PRIMARY KEY (source_id, page_key)
);
CREATE INDEX IF NOT EXISTS snapshot_pages_url ON snapshot_pages(url);
CREATE VIRTUAL TABLE IF NOT EXISTS snapshot_search USING fts5(
    source_id UNINDEXED, page_key UNINDEXED, title, body,
    tokenize='porter unicode61'
);
"""


# Search and direct reads choose from all versions of the URL, including those
# whose text does not match the query. Numeric revision IDs are comparable when
# all copies have them. Otherwise prefer a dated copy, normalizing time zones.
_CURRENT_PAGE = """
    SELECT page_rowid FROM (
        SELECT c.rowid AS page_rowid, c.page_key, c.source_id, s.imported_at,
               snapshot_timestamp(c.revision_timestamp) AS stamp,
               CASE WHEN c.revision <> '' AND c.revision NOT GLOB '*[^0-9]*'
                    THEN ltrim(c.revision, '0') END AS digits
        FROM snapshot_pages c JOIN snapshot_sources s ON s.source_id=c.source_id
        WHERE c.url={url}
    )
    ORDER BY CASE WHEN count(digits) OVER () < count(*) OVER () THEN stamp END DESC,
             length(digits) DESC, digits DESC, stamp DESC, imported_at DESC, source_id, page_key
    LIMIT 1
"""


def _snapshot_timestamp(value: str | None) -> str | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except (ValueError, OverflowError):
        return None


class SnapshotError(ValueError):
    """Invalid or unstable local snapshot; the previous import remains intact."""


def _public_url(value: object, *, https_only: bool = False) -> tuple[str, str]:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise SnapshotError("source URL is missing or too long")
    if any(ord(ch) < 0x21 or ord(ch) == 0x7F for ch in value) or "\\" in value:
        raise SnapshotError("source URL contains whitespace or an unsafe character")
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").rstrip(".").lower()
        port = parts.port
    except ValueError as error:
        raise SnapshotError("source URL is malformed") from error
    schemes = {"https"} if https_only else {"https", "http"}
    if parts.scheme.lower() not in schemes or not host or parts.username or parts.password:
        raise SnapshotError("source URL must be a public HTTP(S) article link")
    if port is not None and port not in ({443} if parts.scheme.lower() == "https" else {80}):
        raise SnapshotError("source URL must use the standard HTTP(S) port")
    if port == (443 if parts.scheme.lower() == "https" else 80):
        port = None
    if not parts.path or parts.path == "/":
        raise SnapshotError("source URL must identify an article")
    if host == "localhost" or host.endswith(_RESERVED_HOST_SUFFIXES):
        raise SnapshotError("source URL must identify a public host")
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        if "." not in host or host.startswith(".") or host.endswith("."):
            raise SnapshotError("source URL must identify a public host")
    else:
        if not address.is_global:
            raise SnapshotError("source URL must identify a public host")
    display_host = f"[{host}]" if ":" in host else host
    netloc = display_host if port is None else f"{display_host}:{port}"
    normalized = urlunsplit((parts.scheme.lower(), netloc, parts.path, parts.query, parts.fragment))
    origin = urlunsplit((parts.scheme.lower(), netloc, "", "", ""))
    return normalized, origin


def _source_id(path: Path) -> str:
    identity = os.path.normcase(str(path.resolve())).casefold().encode("utf-8")
    return hashlib.sha256(identity).hexdigest()


def _validate_limit(value: int | None, name: str) -> int | None:
    if value is not None and (type(value) is not int or value <= 0):
        raise ValueError(f"{name} must be a positive integer or None")
    return value


def _stable_hash(path: Path, max_compressed_bytes: int | None) -> tuple[str, os.stat_result]:
    try:
        before = path.stat()
        if not path.is_file() or before.st_size <= 0:
            raise SnapshotError("snapshot file is empty or is not a regular file")
        if max_compressed_bytes is not None and before.st_size > max_compressed_bytes:
            raise SnapshotError("snapshot exceeds the compressed-byte limit")
        digest = hashlib.sha256()
        scanned = 0
        tail = b""
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if _stat_key(before) != _stat_key(opened):
                raise SnapshotError("snapshot changed before it could be read")
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                scanned += len(chunk)
                if max_compressed_bytes is not None and scanned > max_compressed_bytes:
                    raise SnapshotError("snapshot exceeds the compressed-byte limit")
                if path.name.lower().endswith(".xml") and _FORBIDDEN_XML.search(tail + chunk):
                    raise SnapshotError("XML DTD and entity declarations are not accepted")
                tail = (tail + chunk)[-24:]
                digest.update(chunk)
            after = os.fstat(stream.fileno())
        path_after = path.stat()
    except OSError as error:
        raise SnapshotError("snapshot file could not be read") from error
    if _stat_key(before) != _stat_key(after) or _stat_key(after) != _stat_key(path_after):
        raise SnapshotError("snapshot changed while it was being read")
    if scanned != before.st_size:
        raise SnapshotError("snapshot was truncated while it was being read")
    return digest.hexdigest(), after


def _stat_key(value: os.stat_result) -> tuple[int, int, int, int, int]:
    # Windows path and handle stats can differ on ctime. Birth time is consistent
    # across both, and both content passes still compare SHA-256.
    stamp = getattr(value, "st_birthtime_ns", value.st_ctime_ns) if os.name == "nt" else value.st_ctime_ns
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, stamp


def _query(query: object) -> tuple[str, ...]:
    if not isinstance(query, str) or not query.strip() or len(query.encode("utf-8")) > MAX_QUERY_BYTES:
        return ()
    terms: list[str] = []
    for term in _WORD.findall(query.lower()):
        if term not in _STOP_WORDS and term not in terms:
            terms.append(term)
    return tuple(terms[:MAX_QUERY_TERMS])


def _fts_query(query: object) -> str:
    terms = _query(query)
    return " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)


def _element_text(parent: ET.Element, wanted: str) -> str | None:
    for child in parent:
        if _local(child.tag) == wanted:
            return child.text or ""
    return None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _canonical_page_url(base: str, title: str) -> tuple[str, str]:
    base_url, origin = _public_url(base)
    parts = urlsplit(base_url)
    title_value = title.replace(" ", "_")
    encoded_title = quote(title_value, safe="/:@!$&'()*+,;=-._~")
    query = parse_qsl(parts.query, keep_blank_values=True)
    if any(key.casefold() == "title" for key, _ in query):
        replaced = [(key, title_value if key.casefold() == "title" else val) for key, val in query]
        page_url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(replaced), ""))
    else:
        path = parts.path
        if path.endswith("$1"):
            path = path[:-2]
        elif not path.endswith("/"):
            path = path.rsplit("/", 1)[0] + "/"
        page_url = urlunsplit((parts.scheme, parts.netloc, path + encoded_title, "", ""))
    return page_url, origin


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.values: list[str] = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name.lower() == "href" and value:
                self.values.append(value)


def _extract_links(body: str, base: str | None) -> list[str]:
    found: list[str] = []
    if base:
        for match in _WIKILINK.finditer(body):
            title = match.group(1).strip()
            if title and not title.startswith(("#", ":")):
                try:
                    found.append(_canonical_page_url(base, title)[0])
                except SnapshotError:
                    pass
    found.extend(_MARKDOWN_LINK.findall(body))
    found.extend(_EXTERNAL_WIKI_LINK.findall(body))
    parser = _Links()
    try:
        parser.feed(body)
    except Exception:
        pass
    found.extend(parser.values)
    result: list[str] = []
    for link in found:
        try:
            safe, _ = _public_url(urljoin(base, link) if base else link)
        except SnapshotError:
            continue
        if safe not in result:
            result.append(safe)
            if len(result) >= MAX_LINKS:
                break
    return result


def _iter_xml_pages(stream, insert_page, max_pages: int | None):
    """Streaming XML variant that collects direct page fields without retaining history."""
    base: str | None = None
    namespaces: dict[int, str] = {}
    root = None
    page: dict | None = None
    stack: list[ET.Element] = []
    page_count = 0
    try:
        for event, element in ET.iterparse(stream, events=("start", "end")):
            tag = _local(element.tag)
            if event == "start":
                if root is None:
                    root = element
                stack.append(element)
                if tag == "page":
                    page = {"revision_key": None, "revision": None, "revision_timestamp": None,
                            "body": None, "namespace": None, "namespace_name": None,
                            "redirect_target": None, "title": None, "ns": None, "id": None}
                continue
            if tag == "siteinfo":
                base_element = next((node for node in element.iter() if _local(node.tag) == "base"), None)
                if base_element is None or not base_element.text:
                    raise SnapshotError("MediaWiki export has no siteinfo base URL")
                base, _ = _public_url(base_element.text.strip())
                for node in element.iter():
                    if _local(node.tag) == "namespace":
                        key = node.attrib.get("key")
                        if key is not None:
                            try:
                                namespaces[int(key)] = node.text or ""
                            except ValueError as error:
                                raise SnapshotError("MediaWiki export has an invalid namespace id") from error
                element.clear()
                if root is not None:
                    root.clear()
            elif page is not None and tag == "revision":
                revision_id = _element_text(element, "id")
                timestamp = _element_text(element, "timestamp")
                text_element = next((node for node in element if _local(node.tag) == "text"), None)
                if text_element is None and not any(k in element.attrib for k in ("deleted", "suppressed")):
                    raise SnapshotError("MediaWiki revision has no text element")
                body = "" if text_element is None else (text_element.text or "")
                if len(body) > MAX_PAGE_CHARS:
                    raise SnapshotError("MediaWiki page exceeds the per-page size limit")
                revision_num = int(revision_id) if revision_id and revision_id.isdecimal() else None
                stamp = _snapshot_timestamp(timestamp) or ""
                rank = (1, revision_num, stamp) if revision_num is not None else (0, stamp)
                if page["revision_key"] is None or rank > page["revision_key"]:
                    page["revision_key"] = rank
                    page["revision"] = revision_id
                    page["revision_timestamp"] = timestamp
                    page["body"] = body
                element.clear()
            elif page is not None and len(stack) >= 2 and stack[-2] is not None and _local(stack[-2].tag) == "page":
                if tag in {"title", "ns", "id"}:
                    page[tag] = element.text or ""
                elif tag == "redirect":
                    page["redirect_target"] = element.attrib.get("title")
                element.clear()
            if tag == "page" and page is not None:
                title = page["title"]
                if not title or (page["revision"] is None and page["revision_timestamp"] is None):
                    raise SnapshotError("MediaWiki page is missing its title or revision")
                try:
                    namespace = int(page["ns"] or 0)
                except ValueError as error:
                    raise SnapshotError("MediaWiki page has an invalid namespace") from error
                page_id = page["id"] or f"{namespace}:{title}"
                if not page_id:
                    raise SnapshotError("MediaWiki page has no stable id")
                if base is None:
                    raise SnapshotError("MediaWiki export must supply siteinfo before pages")
                url, origin = _canonical_page_url(base, title)
                body = page["body"] or ""
                page_count += 1
                if max_pages is not None and page_count > max_pages:
                    raise SnapshotError("MediaWiki export exceeds the configured page-count limit")
                insert_page({
                    "page_key": page_id, "url": url, "source_origin": origin,
                    "title": title, "body": body, "revision": page["revision"],
                    "revision_timestamp": page["revision_timestamp"], "namespace": namespace,
                    "namespace_name": namespaces.get(namespace, ""),
                    "redirect_target": page["redirect_target"], "game_build": None,
                    "links_json": json.dumps(_extract_links(body, base), ensure_ascii=False),
                })
                page = None
                element.clear()
                if root is not None:
                    root.clear()
            if stack:
                stack.pop()
    except ET.ParseError as error:
        raise SnapshotError(f"MediaWiki export is malformed or truncated ({error})") from error
    if base is None or page_count == 0:
        raise SnapshotError("MediaWiki export has no complete pages")
    return base, page_count


def _jsonl_records(stream):
    line_number = 0
    while True:
        raw_line = stream.readline(MAX_JSONL_LINE_BYTES + 1)
        if not raw_line:
            break
        line_number += 1
        if len(raw_line) > MAX_JSONL_LINE_BYTES:
            raise SnapshotError("JSONL article exceeds the per-line size limit")
        try:
            line = raw_line.decode("utf-8")
        except UnicodeDecodeError as error:
            raise SnapshotError("JSONL export must be UTF-8") from error
        if not line.strip():
            continue
        try:
            article = json.loads(line)
        except json.JSONDecodeError as error:
            raise SnapshotError(f"JSONL line {line_number} is invalid") from error
        yield line_number, article


def _stage_articles(records, insert_page, max_pages: int | None):
    page_count = 0
    source_origin: str | None = None
    for line_number, article in records:
        if not isinstance(article, dict):
            raise SnapshotError(f"JSONL line {line_number} must be an object")
        url, origin = _public_url(article.get("url"), https_only=True)
        title, body = article.get("title"), article.get("text")
        if not isinstance(title, str) or not title.strip() or len(title) > 500:
            raise SnapshotError(f"JSONL line {line_number} has an invalid title")
        if not isinstance(body, str) or not body.strip() or len(body) > MAX_PAGE_CHARS:
            raise SnapshotError(f"JSONL line {line_number} has invalid or oversized article text")
        revision = article.get("revision")
        if revision is not None and type(revision) not in (str, int):
            raise SnapshotError(f"JSONL line {line_number} has an invalid revision")
        timestamp = article.get("revision_timestamp")
        if timestamp is not None and (not isinstance(timestamp, str) or len(timestamp) > 100):
            raise SnapshotError(f"JSONL line {line_number} has an invalid revision timestamp")
        namespace = article.get("namespace")
        if namespace is not None and type(namespace) is not int:
            raise SnapshotError(f"JSONL line {line_number} has an invalid namespace")
        build = article.get("game_build")
        if build is not None and (not isinstance(build, str) or len(build) > 100):
            raise SnapshotError(f"JSONL line {line_number} has an invalid game build")
        redirect = article.get("redirect")
        if redirect is not None and (not isinstance(redirect, str) or len(redirect) > 500):
            raise SnapshotError(f"JSONL line {line_number} has an invalid redirect")
        page_count += 1
        if max_pages is not None and page_count > max_pages:
            raise SnapshotError("JSONL export exceeds the configured page-count limit")
        page_key = url
        insert_page({
            "page_key": page_key, "url": url, "source_origin": origin,
            "title": title.strip(), "body": body, "revision": None if revision is None else str(revision),
            "revision_timestamp": timestamp, "namespace": namespace, "namespace_name": None,
            "redirect_target": redirect, "game_build": build,
            "links_json": json.dumps(_extract_links(body, url), ensure_ascii=False),
        })
        if source_origin is None:
            source_origin = origin
        elif origin != source_origin:
            source_origin = "mixed"
    if page_count == 0:
        raise SnapshotError("JSONL export has no complete articles")
    return source_origin or "mixed", page_count


def _ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(_SCHEMA)


class _DigestReader(io.RawIOBase):
    """Hash and bound compressed source bytes while the parser streams."""

    def __init__(self, stream, *, max_bytes: int | None):
        self.stream = stream
        self.digest = hashlib.sha256()
        self.max_bytes = max_bytes
        self.bytes_read = 0

    def readable(self):
        return True

    def readinto(self, buffer):
        count = self.stream.readinto(buffer)
        if count:
            data = memoryview(buffer)[:count]
            self.bytes_read += count
            if self.max_bytes is not None and self.bytes_read > self.max_bytes:
                raise SnapshotError("snapshot exceeds the compressed-byte limit")
            self.digest.update(data)
        return count


class _ExpandedReader(io.RawIOBase):
    """Bound decompressed bytes and reject XML declarations before parsing."""

    def __init__(self, stream, *, max_bytes: int | None, xml_mode: bool):
        self.stream = stream
        self.max_bytes = max_bytes
        self.xml_mode = xml_mode
        self.bytes_read = 0
        self.tail = b""

    def readable(self):
        return True

    def readinto(self, buffer):
        count = self.stream.readinto(buffer)
        if count:
            self.bytes_read += count
            if self.max_bytes is not None and self.bytes_read > self.max_bytes:
                raise SnapshotError("snapshot exceeds the configured expanded-byte limit")
            if self.xml_mode:
                chunk = memoryview(buffer)[:count].tobytes()
                if _FORBIDDEN_XML.search(self.tail + chunk):
                    raise SnapshotError("XML DTD and entity declarations are not accepted")
                self.tail = (self.tail + chunk)[-24:]
        return count


def _file_format(path: Path) -> tuple[str, str | None]:
    name = path.name.casefold()
    if name == sc_archive.ARCHIVE_FILENAME:
        return "starcitizen-archive-20230930", "zstd"
    if name == EDITION_FILENAME:
        return "starcitizen-archive-20230930", "gzip"
    if name.endswith(".xml.gz"):
        return "mediawiki-xml-gzip", "gzip"
    if name.endswith(".xml.bz2"):
        return "mediawiki-xml-bzip2", "bzip2"
    if name.endswith(".xml"):
        return "mediawiki-xml", None
    if name.endswith(".jsonl.gz") or name.endswith(".ndjson.gz"):
        return "jsonl-gzip", "gzip"
    if name.endswith(".jsonl") or name.endswith(".ndjson"):
        return "jsonl", None
    raise SnapshotError("snapshot must be .xml, .xml.gz, .xml.bz2, .jsonl, .jsonl.gz, .ndjson, or .ndjson.gz")


def import_snapshot(db_path: Path | str, path: Path | str, *,
                    max_compressed_bytes: int | None = DEFAULT_MAX_COMPRESSED_BYTES,
                    max_expanded_bytes: int | None = DEFAULT_MAX_EXPANDED_BYTES,
                    max_pages: int | None = DEFAULT_MAX_PAGES) -> dict:
    """Import one local XML/JSONL snapshot transactionally.

    The source identity is tied to its resolved file path. Reimporting a changed
    file replaces only rows from that source; other snapshots and user notes stay.
    Gzip XML/JSONL and bzip2 XML are streamed. Aggregate compressed-byte,
    expanded-byte, and page-count ceilings can be raised for large authorized
    exports or explicitly disabled with ``None``. Individual article and
    JSONL-record limits are always enforced.
    """
    db_path, source_path = Path(db_path).expanduser(), Path(path).expanduser()
    if not source_path.is_file():
        raise SnapshotError("snapshot path must name a regular local file")
    max_compressed_bytes = _validate_limit(max_compressed_bytes, "max_compressed_bytes")
    max_expanded_bytes = _validate_limit(max_expanded_bytes, "max_expanded_bytes")
    max_pages = _validate_limit(max_pages, "max_pages")
    file_format, compression = _file_format(source_path)
    digest, original_stat = _stable_hash(source_path, max_compressed_bytes)
    archive_counts = Counter()
    if file_format == "starcitizen-archive-20230930":
        expected_digest = sc_archive.SHA256 if compression == "zstd" else EDITION_SHA256
        if digest != expected_digest:
            raise SnapshotError("The Star Citizen archive does not match the published edition.")
    source_id = _source_id(source_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path, timeout=10)) as db:
        _ensure_schema(db)
        existing = db.execute("SELECT content_sha256,page_count FROM snapshot_sources WHERE source_id=?",
                              (source_id,)).fetchone()
        if existing and existing[0] == digest:
            return {"status": "unchanged", "source_id": source_id,
                    "file_format": file_format, "page_count": existing[1]}
        db.execute("""CREATE TEMP TABLE snapshot_stage (
            page_key TEXT PRIMARY KEY,url TEXT NOT NULL,source_origin TEXT NOT NULL,
            title TEXT NOT NULL,body TEXT NOT NULL,revision TEXT,revision_timestamp TEXT,
            namespace INTEGER,namespace_name TEXT,redirect_target TEXT,game_build TEXT,
            links_json TEXT NOT NULL)""")

        def stage(page: dict) -> None:
            try:
                db.execute("INSERT INTO snapshot_stage VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                    page["page_key"], page["url"], page["source_origin"], page["title"],
                    page["body"], page["revision"], page["revision_timestamp"], page["namespace"],
                    page["namespace_name"], page["redirect_target"], page["game_build"], page["links_json"]))
            except sqlite3.IntegrityError as error:
                raise SnapshotError("snapshot contains a duplicate page identity") from error

        try:
            with source_path.open("rb") as stream, ExitStack() as filters:
                opened = os.fstat(stream.fileno())
                if _stat_key(original_stat) != _stat_key(opened):
                    raise SnapshotError("snapshot changed before parsing")
                digest_stream = _DigestReader(stream, max_bytes=max_compressed_bytes)
                compressed_stream = io.BufferedReader(digest_stream)
                if compression == "gzip":
                    decoded_stream = gzip.GzipFile(fileobj=compressed_stream, mode="rb")
                elif compression == "bzip2":
                    decoded_stream = bz2.BZ2File(compressed_stream, mode="rb")
                elif compression == "zstd":
                    decoded_stream = sc_archive.open_zstd(compressed_stream)
                else:
                    decoded_stream = compressed_stream
                filters.enter_context(decoded_stream)
                bounded_stream = io.BufferedReader(_ExpandedReader(
                    decoded_stream, max_bytes=max_expanded_bytes,
                    xml_mode=file_format.startswith("mediawiki-xml") or compression == "zstd"))
                with bounded_stream:
                    if file_format.startswith("mediawiki-xml"):
                        base, page_count = _iter_xml_pages(bounded_stream, stage, max_pages)
                        source_origin = _public_url(base)[1]
                    elif compression == "zstd":
                        records = enumerate(sc_archive.iter_articles(bounded_stream, archive_counts), 1)
                        source_origin, page_count = _stage_articles(records, stage, max_pages)
                    else:
                        source_origin, page_count = _stage_articles(_jsonl_records(bounded_stream), stage, max_pages)
                after = os.fstat(stream.fileno())
            path_after = source_path.stat()
            if _stat_key(original_stat) != _stat_key(after) or _stat_key(after) != _stat_key(path_after):
                raise SnapshotError("snapshot changed while it was being parsed")
            if digest_stream.digest.hexdigest() != digest:
                raise SnapshotError("snapshot changed between hash and parse")
            if page_count != db.execute("SELECT count(*) FROM snapshot_stage").fetchone()[0]:
                raise SnapshotError("snapshot did not stage a complete page set")
            if not source_origin:
                raise SnapshotError("snapshot has no valid publisher origin")
            # Stage rows live in SQLite's temporary database; commit them before
            # starting the atomic replacement transaction in the main database.
            db.commit()
            db.execute("BEGIN IMMEDIATE")
            previous_source = db.execute("SELECT 1 FROM snapshot_sources WHERE source_id=?",
                                         (source_id,)).fetchone() is not None
            if previous_source:
                old_pages = {row[0]: row[1:] for row in db.execute(
                    "SELECT url,title,body,revision,revision_timestamp FROM snapshot_pages WHERE source_id=?",
                    (source_id,))}
                new_pages = {row[0]: row[1:] for row in db.execute(
                    "SELECT url,title,body,revision,revision_timestamp FROM snapshot_stage")}
                observed_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
                changes = []
                changes.extend((url, "snapshot_page_removed") for url in old_pages.keys() - new_pages.keys())
                changes.extend((url, "snapshot_page_added") for url in new_pages.keys() - old_pages.keys())
                for url in old_pages.keys() & new_pages.keys():
                    old, new = old_pages[url], new_pages[url]
                    if old != new:
                        reason = "snapshot_revision_changed" if old[2:] != new[2:] else "snapshot_content_changed"
                        changes.append((url, reason))
                db.executemany("""INSERT INTO source_changes VALUES(?,?,?)
                    ON CONFLICT(url) DO UPDATE SET observed_at=excluded.observed_at,
                    reason=excluded.reason""",
                    ((url, observed_at, reason) for url, reason in changes))
            db.execute("DELETE FROM snapshot_search WHERE source_id=?", (source_id,))
            db.execute("DELETE FROM snapshot_pages WHERE source_id=?", (source_id,))
            db.execute("""INSERT INTO snapshot_pages(source_id,page_key,url,source_origin,title,body,
                revision,revision_timestamp,namespace,namespace_name,redirect_target,game_build,links_json)
                SELECT ?,page_key,url,source_origin,title,body,revision,revision_timestamp,namespace,
                namespace_name,redirect_target,game_build,links_json FROM snapshot_stage""", (source_id,))
            db.execute("""INSERT INTO snapshot_search(source_id,page_key,title,body)
                SELECT ?,page_key,title,body FROM snapshot_stage""", (source_id,))
            imported_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
            db.execute("""INSERT INTO snapshot_sources(source_id,file_name,file_format,source_origin,
                content_sha256,file_size,page_count,imported_at) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(source_id) DO UPDATE SET file_name=excluded.file_name,
                file_format=excluded.file_format,source_origin=excluded.source_origin,
                content_sha256=excluded.content_sha256,file_size=excluded.file_size,
                page_count=excluded.page_count,imported_at=excluded.imported_at""",
                       (source_id, source_path.name, file_format, source_origin, digest,
                        original_stat.st_size, page_count, imported_at))
            db.commit()
        except (OSError, EOFError, zlib.error) as error:
            if db.in_transaction:
                db.rollback()
            raise SnapshotError(
                f"snapshot compression is corrupt or truncated ({type(error).__name__}: {error})"
            ) from error
        except Exception:
            if db.in_transaction:
                db.rollback()
            raise
    report = {"status": "imported", "source_id": source_id,
              "file_format": file_format, "page_count": page_count}
    if compression == "zstd":
        report["archive_counts"] = dict(archive_counts)
    return report


def sync_snapshots(db_path: Path | str, *, packaged_edition: Path | None = None,
                   max_compressed_bytes: int | None = DEFAULT_MAX_COMPRESSED_BYTES,
                   max_expanded_bytes: int | None = DEFAULT_MAX_EXPANDED_BYTES,
                   max_pages: int | None = DEFAULT_MAX_PAGES) -> dict:
    """Import sibling snapshots, allowing aggregate ceilings to be overridden.

    Pass a larger integer for a full wiki, or ``None`` to disable any aggregate
    ceiling. Individual article and JSONL-record limits remain active.
    """
    max_compressed_bytes = _validate_limit(max_compressed_bytes, "max_compressed_bytes")
    max_expanded_bytes = _validate_limit(max_expanded_bytes, "max_expanded_bytes")
    max_pages = _validate_limit(max_pages, "max_pages")
    database = Path(db_path).expanduser()
    directory = database.parent / "snapshots"
    if not directory.exists() and packaged_edition is None:
        return {"status": "empty", "directory": str(directory), "sources": []}
    if directory.exists() and not directory.is_dir():
        raise SnapshotError("snapshot source path must be a directory")
    results = []
    paths = sorted(directory.iterdir(), key=lambda item: item.name.casefold()) if directory.is_dir() else []
    if packaged_edition is not None:
        paths.insert(0, packaged_edition)
    for path in paths:
        if not path.is_file():
            continue
        try:
            _file_format(path)
        except SnapshotError:
            continue
        try:
            results.append({"file": path.name, **import_snapshot(
                database, path, max_compressed_bytes=max_compressed_bytes,
                max_expanded_bytes=max_expanded_bytes, max_pages=max_pages)})
        except Exception as error:
            results.append({"file": path.name, "status": "error",
                            "error_type": type(error).__name__,
                            "error": str(error) or repr(error)})
    return {"status": "synced", "directory": str(directory), "sources": results}


def _snapshot_provenance(row: sqlite3.Row) -> dict:
    source = {"source_kind": "user_supplied_snapshot", "citation": row["url"], "live_state": False}
    if row["file_format"] == "starcitizen-archive-20230930":
        history = urlencode({"title": row["title"].replace(" ", "_"), "action": "history"})
        source.update({
            "source_kind": "historical_community_archive",
            "publisher": sc_archive.ATTRIBUTION, "edition_date": sc_archive.SNAPSHOT_DATE,
            "archive_url": sc_archive.ARCHIVE_URI,
            "license": "CC BY-SA 4.0", "license_url": sc_archive.LICENSE_URL,
            "attribution": "Star Citizen Wiki contributors, CC BY-SA 4.0",
            "attribution_url": "https://starcitizen.tools/index.php?" + history,
        })
    return source


def search_snapshots(query: object, db_path: Path | str, limit: int = 3, *,
                     source_origin: str | None = None, game_build: str | None = None,
                     allow_source=None, allow_url=None) -> list[dict]:
    """Return ranked matched passages from local snapshots; no publisher requests occur."""
    if type(limit) is not int or not 1 <= limit <= MAX_RESULTS:
        raise ValueError(f"limit must be between 1 and {MAX_RESULTS}")
    match = _fts_query(query)
    if not match:
        return []
    requested_origin = None
    if source_origin is not None:
        requested_origin = _public_url(source_origin + "/article")[1]
    path = Path(db_path).expanduser()
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='snapshot_search'").fetchone():
            return []
        db.create_function("snapshot_source_allowed", 1,
                           lambda origin: int(allow_source is None or bool(allow_source(origin))))
        db.create_function("snapshot_url_allowed", 1,
                           lambda url: int(allow_url is None or bool(allow_url(url))))
        db.create_function("snapshot_timestamp", 1, _snapshot_timestamp, deterministic=True)
        rows = db.execute(f"""SELECT p.source_id,p.page_key,p.url,p.source_origin,p.title,p.revision,s.file_format,
            p.revision_timestamp,p.namespace,p.namespace_name,p.redirect_target,p.game_build,
            snippet(snapshot_search,3,'[',']',' … ',36) AS excerpt,
            bm25(snapshot_search) AS rank
            FROM snapshot_search JOIN snapshot_pages p
            ON p.source_id=snapshot_search.source_id AND p.page_key=snapshot_search.page_key
            JOIN snapshot_sources s ON s.source_id=p.source_id
            WHERE snapshot_search MATCH ?
            AND (? IS NULL OR p.source_origin=?)
            AND (? IS NULL OR p.game_build=?)
            AND snapshot_source_allowed(p.source_origin)=1
            AND snapshot_url_allowed(p.url)=1
            AND p.rowid=({_CURRENT_PAGE.format(url="p.url")})
            ORDER BY rank,p.source_origin,p.title,p.url LIMIT ?""",
                         (match, requested_origin, requested_origin, game_build, game_build, limit)).fetchall()
    results = []
    for row in rows:
        item = dict(row)
        item.pop("rank", None)
        item.update({"status": "snapshot", **_snapshot_provenance(row),
                     "source_links": [item["url"]], "game_build_state":
                         "provided" if item["game_build"] else "unspecified"})
        results.append(item)
        if len(results) >= limit:
            break
    return results


def read_snapshot_page(db_path: Path | str, url: str, offset: int = 0) -> dict:
    """Read a bounded text chunk from the newest local copy of an article URL."""
    if type(offset) is not int or offset < 0:
        raise ValueError("offset must be a nonnegative character offset")
    normalized, _ = _public_url(url)
    path = Path(db_path).expanduser()
    if not path.is_file():
        return {"status": "not_found", "url": normalized}
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='snapshot_pages'").fetchone():
            return {"status": "not_found", "url": normalized}
        db.create_function("snapshot_timestamp", 1, _snapshot_timestamp, deterministic=True)
        row = db.execute(f"""SELECT p.*,s.file_format FROM snapshot_pages p
            JOIN snapshot_sources s ON s.source_id=p.source_id WHERE p.rowid=({_CURRENT_PAGE.format(url='?')})""",
                         (normalized,)).fetchone()
    if row is None:
        return {"status": "not_found", "url": normalized}
    text = row["body"]
    if offset > len(text):
        raise ValueError("offset is beyond the end of this article")
    chunk = text[offset:offset + PAGE_CHUNK_CHARS]
    next_offset = offset + len(chunk) if offset + len(chunk) < len(text) else None
    return {
        "status": "snapshot", "url": row["url"], "source_origin": row["source_origin"],
        "title": row["title"], "text": chunk, "offset": offset, "next_offset": next_offset,
        "revision": row["revision"], "revision_timestamp": row["revision_timestamp"],
        "namespace": row["namespace"], "namespace_name": row["namespace_name"],
        "redirect_target": row["redirect_target"], "game_build": row["game_build"],
        "game_build_state": "provided" if row["game_build"] else "unspecified",
        "file_format": row["file_format"], **_snapshot_provenance(row),
        "links": json.loads(row["links_json"]), "source_id": row["source_id"],
    }
