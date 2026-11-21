"""Bounded conversion of the licensed 2023 Star Citizen Wiki snapshot.

This module handles one explicitly identified local archive. It never contacts a
publisher, follows links, executes wiki markup, or accepts arbitrary wiki dumps.
The full revision stream is reduced in memory to the newest main-namespace
revision per page; only eligible articles with original wikitext are yielded.
"""

from __future__ import annotations

from collections import Counter
import importlib.util
import re
from typing import BinaryIO, Iterator, MutableMapping
from urllib.parse import quote, urlencode
from xml.etree import ElementTree as ET


SOURCE_NAME = "starcitizen.tools"
SOURCE_ORIGIN = "https://starcitizen.tools"
ARCHIVE_IDENTIFIER = "wiki-starcitizen.tools-20230930"
ARCHIVE_URI = f"https://archive.org/details/{ARCHIVE_IDENTIFIER}"
ARCHIVE_FILENAME = "starcitizen.tools-20230930-history.xml.zst"
ARCHIVE_FILE_URI = (
    "https://archive.org/download/" + ARCHIVE_IDENTIFIER + "/" + ARCHIVE_FILENAME
)
ARCHIVE_SIZE = 16_758_993
SHA256 = "34c609fdd321226da3b8c49be5f2edf26ef02b912deea480a37519c0e89ce06a"
SHA1 = "df8a1f3fef36509ee96e8a39a5466003b472cca2"
SNAPSHOT_DATE = "2023-09-30"
LICENSE_URL = "https://creativecommons.org/licenses/by-sa/4.0/"
ATTRIBUTION = "Star Citizen Wiki contributors"
HISTORY_NOTICE = "Latest page revision at the 2023-09-30 snapshot; prior revisions omitted."

# The source frame expands to 903,853,128 bytes. Keep headroom for the exact
# archive while making an unexpected or replaced source fail closed.
MAX_EXPANDED_BYTES = 1_073_741_824
MAX_ZSTD_WINDOW_LOG = 30
MAX_UNIQUE_PAGES = 25_000
MAX_BUFFERED_ARTICLE_CHARS = 128_000_000
MAX_PAGE_CHARS = 2_000_000

PROVENANCE = {
    "source_name": SOURCE_NAME,
    "source_origin": SOURCE_ORIGIN,
    "archive_identifier": ARCHIVE_IDENTIFIER,
    "archive_uri": ARCHIVE_URI,
    "archive_file_uri": ARCHIVE_FILE_URI,
    "archive_sha256": SHA256,
    "archive_sha1": SHA1,
    "snapshot_date": SNAPSHOT_DATE,
    "license_url": LICENSE_URL,
    "attribution": ATTRIBUTION,
    "history": HISTORY_NOTICE,
    "historical_snapshot": True,
}

_FORBIDDEN_XML = re.compile(br"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_REDIRECT = re.compile(r"^\s*#\s*redirect\b", re.IGNORECASE)
_TEMPLATE_NAME = re.compile(r"\{\{\s*([^|{}\n]{1,160})")
_CC_BY_SA = re.compile(r"\bcc\s*[- ]?by\s*[- ]?sa(?:\s*[- ]?4(?:\.0)?)?\b", re.IGNORECASE)
_OTHER_CC = re.compile(r"\bcc\s*[- ]?by\s*[- ]?(?:nc|nd|nc\s*[- ]?sa)\b", re.IGNORECASE)
_TEXT_LICENSE_EXCEPTION = re.compile(
    r"\b(?:all\s+rights\s+reserved|used\s+with\s+permission|non[- ]free|fair\s+use)\b",
    re.IGNORECASE,
)
_THIRD_PARTY_TITLE = re.compile(
    r"^(?:comm[- ]?link|galactapedia|spectrum|jump\s+point|patch\s+notes?|"
    r"updates?|roadmap|rsi\s+(?:support|news)|star\s+citizen\s+wiki)\s*[:/]|"
    r"\.(?:js|css|json|lua|php|svg|png|jpe?g|gif|webp|pdf|mp3|ogg|mp4)$",
    re.IGNORECASE,
)
_PUBLISHER_TEMPLATE = re.compile(
    r"(?:comm[- ]?link|galactapedia|spectrum\s+dispatch|official\s+press\s+release)",
    re.IGNORECASE,
)
_VERBATIM_TEMPLATE = re.compile(
    r"(?:quote|quotation|blockquote|cquote|excerpt|extract|snippet)",
    re.IGNORECASE,
)
_IN_GAME_TEXT_TEMPLATE = re.compile(
    r"(?:in[- ]game\s+description|game\s+text)",
    re.IGNORECASE,
)
_QUOTE_MARKUP = re.compile(
    r"(?:<blockquote\b|\|\s*(?:quote|quotation|quoted\s+text)\s*=)",
    re.IGNORECASE,
)
_SOURCE_FIELD = re.compile(
    r"\|\s*(?:source\s+text|description\s+source)\s*=",
    re.IGNORECASE,
)
_RIGHTS_TEMPLATE = re.compile(
    r"(?:licen[cs]e|copyright|rights\s+reserved|non[- ]?free|fair\s+use|"
    r"used\s+with\s+permission|third[- ]?party\s+rights)",
    re.IGNORECASE,
)


class ArchiveError(ValueError):
    """The supplied data is not the exact supported archive edition."""


class _BoundedXmlReader:
    """Count expanded bytes and reject DTD/entity declarations while parsing."""

    def __init__(self, stream: BinaryIO, maximum: int = MAX_EXPANDED_BYTES) -> None:
        self._stream = stream
        self._maximum = maximum
        self._read = 0
        self._tail = b""

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = min(64 * 1024, self._maximum - self._read + 1)
        data = self._stream.read(size)
        self._read += len(data)
        if self._read > self._maximum:
            raise ArchiveError("expanded archive exceeds the fixed byte limit")
        if data:
            if _FORBIDDEN_XML.search(self._tail + data):
                raise ArchiveError("XML DTD and entity declarations are not accepted")
            self._tail = (self._tail + data)[-24:]
        return data


def open_zstd(compressed_binary_stream: BinaryIO) -> BinaryIO:
    """Open the supported Zstandard frame without closing its input stream."""
    try:
        from compression import zstd
    except ImportError:
        if importlib.util.find_spec("zstandard") is not None:
            import zstandard

            decompressor = zstandard.ZstdDecompressor(
                max_window_size=MAX_EXPANDED_BYTES
            )
            return decompressor.stream_reader(
                compressed_binary_stream, read_size=128 * 1024, closefd=False
            )
        raise RuntimeError("Use Python 3.14 compression.zstd or install zstandard")

    return zstd.open(
        compressed_binary_stream,
        "rb",
        options={zstd.DecompressionParameter.window_log_max: MAX_ZSTD_WINDOW_LOG},
    )


def _local_name(tag: object) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _direct_text(element: ET.Element, name: str) -> str | None:
    child = next((child for child in element if _local_name(child.tag) == name), None)
    return None if child is None else child.text


def _revision_text(
    revision: ET.Element,
) -> tuple[str | None, str | None]:
    """Return main-slot wikitext and model without retaining contributor data."""
    direct_text = next(
        (child for child in revision if _local_name(child.tag) == "text"), None
    )
    if direct_text is not None:
        return direct_text.text or "", _direct_text(revision, "model") or "wikitext"

    slots = next((child for child in revision if _local_name(child.tag) == "slots"), None)
    if slots is None:
        return None, None
    main_slot = next(
        (slot for slot in slots if _local_name(slot.tag) == "slot"
         and slot.attrib.get("role", "main").casefold() == "main"),
        None,
    )
    if main_slot is None:
        return None, None
    text = next((node for node in main_slot.iter() if _local_name(node.tag) == "text"), None)
    model = next((node.text for node in main_slot.iter()
                  if _local_name(node.tag) == "model"), None)
    return None if text is None else (text.text or ""), model or "wikitext"


def _revision_rank(revision_id: str | None, timestamp: str | None) -> tuple[int, int, str]:
    if revision_id and revision_id.isdecimal():
        return (1, int(revision_id), timestamp or "")
    return (0, 0, timestamp or "")


def _third_party_title(title: str) -> bool:
    return bool(_THIRD_PARTY_TITLE.search(title.strip()))


def _rights_exception(wikitext: str) -> bool:
    """Reject explicit non-CC or unclear page-level rights declarations."""
    if _OTHER_CC.search(wikitext):
        return True
    for match in _TEMPLATE_NAME.finditer(wikitext):
        name = re.sub(r"[ _]+", " ", match.group(1)).strip()
        if _RIGHTS_TEMPLATE.search(name) and not _CC_BY_SA.search(name):
            return True
    if _TEXT_LICENSE_EXCEPTION.search(wikitext) and not _CC_BY_SA.search(wikitext):
        return True
    return False


def _ambiguous_third_party_reason(wikitext: str) -> str | None:
    """Return a reason when markup signals quoted or publisher-owned text."""
    if _QUOTE_MARKUP.search(wikitext):
        return "quoted_markup"
    if _SOURCE_FIELD.search(wikitext):
        return "source_field"
    for match in _TEMPLATE_NAME.finditer(wikitext):
        name = re.sub(r"[ _]+", " ", match.group(1)).strip()
        if _PUBLISHER_TEMPLATE.search(name):
            return "publisher_template"
        if _IN_GAME_TEXT_TEMPLATE.search(name):
            return "in_game_text_template"
        if _VERBATIM_TEMPLATE.search(name):
            return "quoted_template"
    return None


def _canonical_url(title: str) -> str:
    safe_title = quote(title.replace(" ", "_"), safe="/:@!$&'()*+,;=-._~")
    return f"{SOURCE_ORIGIN}/{safe_title}"


def _history_url(title: str) -> str:
    query = urlencode({"title": title.replace(" ", "_"), "action": "history"})
    return f"{SOURCE_ORIGIN}/index.php?{query}"


def _set_count(counts: MutableMapping[str, int], key: str, value: int = 1) -> None:
    counts[key] = counts.get(key, 0) + value


def iter_articles(
    stream: BinaryIO,
    counts: MutableMapping[str, int] | None = None,
) -> Iterator[dict[str, object]]:
    """Yield importer-compatible JSONL rows from the exact historical snapshot.

    The supplied stream must be the expanded XML returned by ``open_zstd``.
    Revisions are deduplicated by page ID, retaining only the highest revision
    ID and timestamp. Results are yielded only after site identity and the whole
    XML document have been validated.
    """
    stats: MutableMapping[str, int] = counts if counts is not None else Counter()
    stats.setdefault("xml_page_blocks", 0)
    stats.setdefault("main_namespace_page_blocks", 0)
    stats.setdefault("other_namespace_page_blocks", 0)
    stats.setdefault("revision_blocks", 0)
    stats.setdefault("unique_main_namespace_pages", 0)
    stats.setdefault("skipped_redirect", 0)
    stats.setdefault("skipped_third_party_verbatim", 0)
    stats.setdefault("skipped_ambiguous_third_party", 0)
    stats.setdefault("skipped_third_party_quoted_markup", 0)
    stats.setdefault("skipped_third_party_quoted_template", 0)
    stats.setdefault("skipped_third_party_source_field", 0)
    stats.setdefault("skipped_third_party_publisher_template", 0)
    stats.setdefault("skipped_third_party_in_game_text_template", 0)
    stats.setdefault("skipped_explicit_or_unknown_rights", 0)
    stats.setdefault("skipped_non_text", 0)
    stats.setdefault("skipped_no_text", 0)
    stats.setdefault("skipped_empty_text", 0)
    stats.setdefault("accepted_articles", 0)

    pages: dict[str, dict[str, object]] = {}
    buffered_chars = 0
    siteinfo_seen = False
    canonical_base = None
    sitename = None
    page: dict[str, object] | None = None
    page_element: ET.Element | None = None
    stack: list[ET.Element] = []

    try:
        guarded_stream = _BoundedXmlReader(stream)
        for event, element in ET.iterparse(guarded_stream, events=("start", "end")):
            tag = _local_name(element.tag)
            if event == "start":
                stack.append(element)
                if tag == "page":
                    page_element = element
                    page = {
                        "title": None,
                        "namespace": None,
                        "page_id": None,
                        "redirect": None,
                        "revision": None,
                    }
                continue

            parent = stack[-2] if len(stack) > 1 else None
            if tag == "siteinfo":
                if siteinfo_seen:
                    raise ArchiveError("archive contains duplicate siteinfo records")
                siteinfo_seen = True
                sitename = next((node.text for node in element.iter()
                                 if _local_name(node.tag) == "sitename"), None)
                canonical_base = next((node.text for node in element.iter()
                                       if _local_name(node.tag) == "base"), None)
                if sitename != "Star Citizen Wiki" or canonical_base != SOURCE_ORIGIN + "/":
                    raise ArchiveError("siteinfo does not identify the supported Star Citizen Wiki host")
                element.clear()
            elif page is not None and parent is page_element and tag in {"title", "ns", "id"}:
                if tag == "title":
                    page["title"] = element.text or ""
                elif tag == "ns":
                    page["namespace"] = element.text or ""
                else:
                    page["page_id"] = element.text or ""
                element.clear()
            elif page is not None and parent is page_element and tag == "redirect":
                page["redirect"] = element.attrib.get("title") or ""
                element.clear()
            elif page is not None and parent is page_element and tag == "revision":
                _set_count(stats, "revision_blocks")
                revision_id = _direct_text(element, "id")
                timestamp = _direct_text(element, "timestamp")
                body, model = _revision_text(element)
                current = page.get("revision")
                rank = _revision_rank(revision_id, timestamp)
                if current is None or rank > current["rank"]:  # type: ignore[index]
                    page["revision"] = {
                        "rank": rank,
                        "revision_id": revision_id,
                        "timestamp": timestamp,
                        "body": body,
                        "model": model,
                    }
                element.clear()
            elif tag == "page" and page is not None:
                _set_count(stats, "xml_page_blocks")
                title = page.get("title")
                namespace = page.get("namespace")
                page_id = page.get("page_id")
                if not isinstance(title, str) or not title.strip() or not page_id:
                    raise ArchiveError("XML page is missing its title or stable page ID")
                if not isinstance(namespace, str) or not namespace.isdecimal():
                    raise ArchiveError("XML page has an invalid namespace ID")
                if namespace != "0":
                    _set_count(stats, "other_namespace_page_blocks")
                else:
                    _set_count(stats, "main_namespace_page_blocks")
                    revision = page.get("revision")
                    if revision is None:
                        raise ArchiveError("main-namespace page has no revision record")
                    body = revision["body"]  # type: ignore[index]
                    if isinstance(body, str):
                        if len(body) > MAX_PAGE_CHARS:
                            body = None
                            revision["model"] = "oversized"
                        else:
                            buffered_chars += len(body)
                            if buffered_chars > MAX_BUFFERED_ARTICLE_CHARS:
                                raise ArchiveError("current-article memory bound exceeded")
                    revision_id = revision["revision_id"]  # type: ignore[index]
                    timestamp = revision["timestamp"]  # type: ignore[index]
                    rank = revision["rank"]  # type: ignore[index]
                    existing = pages.get(str(page_id))
                    if existing is None or rank > existing["rank"]:
                        if existing is not None:
                            buffered_chars -= len(existing.get("body") or "")
                        pages[str(page_id)] = {
                            "title": title.strip(),
                            "namespace": 0,
                            "page_id": str(page_id),
                            "redirect": page.get("redirect"),
                            "revision_id": revision_id,
                            "timestamp": timestamp,
                            "rank": rank,
                            "body": body,
                            "model": revision["model"],  # type: ignore[index]
                        }
                    else:
                        buffered_chars -= len(body or "")
                    if len(pages) > MAX_UNIQUE_PAGES:
                        raise ArchiveError("unique-page count exceeds the fixed source bound")
                page = None
                page_element = None
                element.clear()
            if stack:
                stack.pop()
        if not siteinfo_seen or not canonical_base:
            raise ArchiveError("MediaWiki XML has no verified siteinfo record")
    except ET.ParseError as error:
        raise ArchiveError(f"archive XML is malformed or truncated ({type(error).__name__})") from error

    stats["unique_main_namespace_pages"] = len(pages)
    for candidate in sorted(pages.values(), key=lambda item: str(item["title"]).casefold()):
        title = str(candidate["title"])
        body = candidate.get("body")
        if candidate.get("redirect") or (isinstance(body, str) and _REDIRECT.match(body)):
            _set_count(stats, "skipped_redirect")
            continue
        if _third_party_title(title):
            _set_count(stats, "skipped_third_party_verbatim")
            continue
        ambiguous_reason = (
            _ambiguous_third_party_reason(body) if isinstance(body, str) else None
        )
        if ambiguous_reason:
            _set_count(stats, "skipped_ambiguous_third_party")
            _set_count(stats, "skipped_third_party_" + ambiguous_reason)
            continue
        if candidate.get("model") == "oversized":
            _set_count(stats, "skipped_non_text")
            continue
        if candidate.get("model") not in {None, "wikitext", "text/x-wiki"}:
            _set_count(stats, "skipped_non_text")
            continue
        if body is None:
            _set_count(stats, "skipped_no_text")
            continue
        if _rights_exception(body):
            _set_count(stats, "skipped_explicit_or_unknown_rights")
            continue
        if re.search(r"<(?:script|style)\b", body, re.IGNORECASE):
            _set_count(stats, "skipped_non_text")
            continue
        if not body:
            _set_count(stats, "skipped_empty_text")
            continue
        revision_id = candidate.get("revision_id")
        revision: int | str | None = (
            int(revision_id) if isinstance(revision_id, str) and revision_id.isdecimal()
            else revision_id if isinstance(revision_id, str) else None
        )
        article: dict[str, object] = {
            "url": _canonical_url(title),
            "title": title,
            # Keep the original main-namespace wikitext exactly as exported.
            # It remains inert: this importer does not render or fetch markup.
            "text": body,
            "revision": revision,
            "revision_timestamp": candidate.get("timestamp"),
            "source_page_id": candidate.get("page_id"),
            "namespace": 0,
            "game_build": None,
            # These fields are retained by standalone JSONL consumers. The
            # repository importer may ignore them and records PROVENANCE once.
            "archive_identifier": ARCHIVE_IDENTIFIER,
            "snapshot_date": SNAPSHOT_DATE,
            "license": LICENSE_URL,
            "attribution": ATTRIBUTION,
            "history_notice": HISTORY_NOTICE,
            "history_url": _history_url(title),
            "historical_snapshot": True,
        }
        _set_count(stats, "accepted_articles")
        yield article


__all__ = [
    "ARCHIVE_FILE_URI",
    "ARCHIVE_FILENAME",
    "ARCHIVE_IDENTIFIER",
    "ARCHIVE_SIZE",
    "ARCHIVE_URI",
    "ATTRIBUTION",
    "HISTORY_NOTICE",
    "LICENSE_URL",
    "MAX_EXPANDED_BYTES",
    "PROVENANCE",
    "SHA1",
    "SHA256",
    "SNAPSHOT_DATE",
    "SOURCE_NAME",
    "SOURCE_ORIGIN",
    "ArchiveError",
    "iter_articles",
    "open_zstd",
]
