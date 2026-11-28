"""Bounded local retrieval for curated and locally synchronized wiki references."""

from __future__ import annotations

import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_INDEX_ROW = re.compile(
    r"^\|\s*\[([^\]]{1,120})\]\(([A-Za-z0-9][A-Za-z0-9_-]{0,79}\.md)\)"
    r"\s*\|\s*(.{1,500}?)\s*\|\s*$"
)
_INDEX_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\.md\Z")
_REVIEW = re.compile(r"^Reviewed ([0-9]{4}-[0-9]{2}-[0-9]{2})\.$")
_BUILD = re.compile(r"^Game build:\s*([A-Za-z0-9][A-Za-z0-9._-]{0,63})\s*$")
_QUERY_WORD = re.compile(r"(?:[0-9]+(?:\.[0-9]+){1,3}|[a-zA-Z][a-zA-Z0-9-]{1,})")
_LINK = re.compile(r"\[[^\]]{1,200}\]\(([^)\s]{1,500})\)")
_STOP_WORDS = frozenset({
    "about", "after", "also", "and", "are", "does", "for", "from", "how", "into",
    "its", "mean", "more", "that", "the", "their", "them", "then", "there", "these",
    "this", "those", "what", "when", "where", "which", "with", "would",
})
_MAX_QUERY_BYTES = 512
_MAX_RESULTS = 3
_MAX_INDEXED_PAGES = 128
_MAX_EXCERPT_CHARS = 1600
_MAX_INDEX_BYTES = 64 * 1024
_MAX_PAGE_BYTES = 512 * 1024
_COMMUNITY_SOURCE = "https://starcitizen.tools/index.php?oldid={}"


def _eligible_public_citation(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    if parsed.netloc not in {
        "starcitizen.tools", "robertsspaceindustries.com", "support.robertsspaceindustries.com",
    }:
        return False
    return parsed.scheme == "https" and not parsed.query and not parsed.fragment


def _query_terms(query: object) -> tuple[str, ...]:
    if type(query) is not str or not query.strip():
        return ()
    if len(query.encode("utf-8")) > _MAX_QUERY_BYTES:
        return ()
    terms = []
    for word in _QUERY_WORD.findall(query.lower()):
        if word not in _STOP_WORDS and word not in terms:
            terms.append(word)
    return tuple(terms[:24])


def _decode(path: Path, limit: int) -> str:
    raw = path.read_bytes()
    if not 0 < len(raw) <= limit:
        raise ValueError("wiki file size outside bound")
    return raw.decode("utf-8", errors="strict")


def _indexed_pages(wiki_root: Path) -> tuple[tuple[str, str, str], ...]:
    index = _decode(wiki_root / "README.md", _MAX_INDEX_BYTES)
    result: list[tuple[str, str, str]] = []
    for line in index.splitlines():
        if not line.lstrip().startswith("|") or "[" not in line:
            continue
        match = _INDEX_ROW.fullmatch(line)
        if match is None:
            if re.match(r"^\|\s*\[", line):
                raise ValueError("wiki index row does not use the canonical link/summary shape")
            continue
        title, slug, summary = match.groups()
        if _INDEX_SLUG.fullmatch(slug) is None or not summary.strip():
            raise ValueError("unsupported wiki index row")
        if slug == "README.md" or any(existing_slug == slug for existing_slug, _, _ in result):
            raise ValueError("duplicate or recursive wiki index path")
        result.append((slug, title.strip(), summary.strip()))
        if len(result) > _MAX_INDEXED_PAGES:
            raise ValueError("wiki index exceeds the fixed page limit")
    if not result:
        raise ValueError("wiki index has no supported topic rows")
    return tuple(result)


def community_db_path(db_path: Path | str | None = None) -> Path:
    """Return the caller-selected or standard local wiki cache path."""
    if db_path is not None:
        return Path(db_path).expanduser()
    override = os.environ.get("SC_WIKI_DB")
    return (Path(override).expanduser() if override else
            Path.home() / ".starcitizen-wiki" / "wiki.sqlite3")


def _parse_game_page(
    slug: str, indexed_title: str, text: str, *, citation: str | None = None,
) -> tuple[dict[str, Any], ...]:
    lines = text.splitlines()
    if len(lines) < 3 or not lines[0].startswith("# ") or lines[1] != "":
        return ()
    title = lines[0][2:].strip()
    if not title or title != indexed_title or _REVIEW.fullmatch(lines[2]) is None:
        return ()
    reviewed = _REVIEW.fullmatch(lines[2]).group(1)
    body = "\n".join(lines[3:]).strip()
    if body.startswith("Game build:"):
        first, separator, remaining = body.partition("\n\n")
        match = _BUILD.fullmatch(first)
        if match is not None:
            page_build = match.group(1)
            body = remaining if separator else ""
        else:
            page_build = None
    else:
        page_build = None

    sections: list[tuple[str, list[str]]] = []
    current_title: str | None = None
    current_lines: list[str] = []
    for line in body.splitlines():
        if line.startswith("## "):
            if current_title is not None:
                sections.append((current_title, current_lines))
            current_title = line[3:].strip()
            if current_title.casefold() == "revision history":
                break
            current_lines = []
        elif current_title is not None:
            current_lines.append(line)
    else:
        if current_title is not None:
            sections.append((current_title, current_lines))

    parsed: list[dict[str, Any]] = []
    for heading, section_lines in sections:
        content = "\n".join(section_lines).strip()
        if not content:
            continue
        section_build = page_build
        first_paragraph = content.split("\n\n", 1)[0].strip()
        build_match = _BUILD.fullmatch(first_paragraph)
        if build_match is not None:
            section_build = build_match.group(1)
            content = content[len(first_paragraph):].lstrip()
        if not content:
            continue
        # A source-only paragraph is the footnote to the preceding paragraph.
        content = re.sub(r"\n\n(?=\[Source\]\(https://)", "\n", content)
        for paragraph in content.split("\n\n"):
            # Keep the citation next to the claim it actually supports.
            links = tuple(dict.fromkeys(
                link for link in _LINK.findall(paragraph)
                if link.startswith("https://") and urlsplit(link).hostname
            ))
            if not links and not citation:
                continue
            parsed.append({
                "title": title, "heading": heading, "reviewed": reviewed,
                "game_build": section_build, "text": paragraph,
                "page_url": links[0] if links else citation, "links": links,
            })
    return tuple(parsed)


def _build_key(value: object) -> str | None:
    if type(value) is not str or not value.strip() or len(value) > 64:
        return None
    return value.strip().lower().removeprefix("alpha ")


def _score(
    terms: tuple[str, ...], *, title: str, heading: str, text: str,
    summary: str = "",
) -> tuple[int, int]:
    title_terms = set(_QUERY_WORD.findall(title.lower()))
    heading_terms = set(_QUERY_WORD.findall(heading.lower()))
    summary_terms = set(_QUERY_WORD.findall(summary.lower()))
    text_terms = set(_QUERY_WORD.findall(text.lower()))
    title_hits = len(set(terms) & title_terms)
    heading_hits = len(set(terms) & heading_terms)
    text_hits = len(set(terms) & text_terms)
    summary_hits = len(set(terms) & summary_terms)
    return 4 * title_hits + 3 * heading_hits + 2 * summary_hits + text_hits, len(
        set(terms) & (title_terms | heading_terms | summary_terms | text_terms)
    )


def _excerpt(value: str) -> str:
    value = value.strip()
    if len(value) <= _MAX_EXCERPT_CHARS:
        return value
    return value[:_MAX_EXCERPT_CHARS].rsplit(" ", 1)[0].rstrip() + "…"


class WikiReference:
    """Search only the canonical local index and a separately labeled local cache."""

    @staticmethod
    def search_curated(
        wiki_root: Path, query: object, *, game_build: object = None,
        limit: int = _MAX_RESULTS,
    ) -> dict[str, Any]:
        terms = _query_terms(query)
        if not terms or type(limit) is not int or not 1 <= limit <= _MAX_RESULTS:
            return {"status": "unknown", "reason": "invalid_or_empty_query", "references": []}
        requested_build = _build_key(game_build) if game_build is not None else None
        if game_build is not None and requested_build is None:
            return {"status": "unknown", "reason": "invalid_game_build", "references": []}
        try:
            topics = _indexed_pages(Path(wiki_root))
            candidates = []
            for slug, title, summary in topics:
                try:
                    text = _decode(Path(wiki_root) / slug, _MAX_PAGE_BYTES)
                except (OSError, UnicodeError, ValueError):
                    continue
                for section in _parse_game_page(slug, title, text):
                    score, matched = _score(
                        terms, title=section["title"], heading=section["heading"],
                        text=section["text"], summary=summary,
                    )
                    if not matched or matched < min(2, len(terms)):
                        continue
                    scoped_build = section["game_build"]
                    if requested_build is not None and _build_key(scoped_build) != requested_build:
                        continue
                    result = {
                        "title": section["title"],
                        "section": section["heading"],
                        "reviewed": section["reviewed"],
                        "game_build": scoped_build,
                        "game_build_state": "matched" if scoped_build else "unknown",
                        "excerpt": _excerpt(section["text"]),
                        "citation": section["page_url"],
                        "source_links": list(section["links"]),
                        "source_kind": "curated_project_reference",
                        "live_state": False,
                    }
                    exact = matched == len(terms)
                    candidates.append((score, exact, result))
            candidates.sort(key=lambda item: (-item[0], item[2]["title"], item[2]["section"]))
            selected = [item[2] for item in candidates[:limit]]
            if not selected:
                return {"status": "unknown", "reason": "no_reviewed_match", "references": []}
            return {
                "status": "reference", "references": selected,
                "exact_curated_match": any(item[1] for item in candidates[:limit]),
            }
        except (OSError, UnicodeError, ValueError):
            return {
                "status": "unavailable",
                "reason": "reviewed_index_unavailable",
                "references": [],
            }

    @staticmethod
    def search_notes(
        notes_root: Path, query: object, *, game_build: object = None,
        limit: int = _MAX_RESULTS,
    ) -> list[dict[str, Any]]:
        """Search only published, reviewed Markdown notes in the selected local folder."""
        terms = _query_terms(query)
        if not terms or type(limit) is not int or not 1 <= limit <= _MAX_RESULTS:
            return []
        requested_build = _build_key(game_build) if game_build is not None else None
        if game_build is not None and requested_build is None:
            return []
        root = Path(notes_root)
        try:
            if not root.is_dir():
                return []
            candidates = []
            for path in sorted(root.glob("*.md")):
                if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\.md", path.name) is None:
                    continue
                if path.stem.casefold().startswith("draft-"):
                    continue
                try:
                    text = _decode(path, _MAX_PAGE_BYTES)
                except (OSError, UnicodeError, ValueError):
                    continue
                title_line = text.splitlines()[0] if text.splitlines() else ""
                if not title_line.startswith("# "):
                    continue
                title = title_line[2:].strip()
                citation = "local-wiki:" + path.stem
                for section in _parse_game_page(path.name, title, text, citation=citation):
                    score, matched = _score(
                        terms, title=section["title"], heading=section["heading"],
                        text=section["text"],
                    )
                    if not matched or matched < min(2, len(terms)):
                        continue
                    scoped_build = section["game_build"]
                    if requested_build is not None and _build_key(scoped_build) != requested_build:
                        continue
                    candidates.append((score, {
                        "title": section["title"], "section": section["heading"],
                        "reviewed": section["reviewed"], "game_build": scoped_build,
                        "game_build_state": "matched" if scoped_build else "unknown",
                        "excerpt": _excerpt(section["text"]), "citation": section["page_url"],
                        "source_links": list(section["links"]),
                        "source_kind": "deployment_curated_note", "live_state": False,
                    }))
            candidates.sort(key=lambda item: (-item[0], item[1]["title"], item[1]["section"]))
            return [item[1] for item in candidates[:limit]]
        except OSError:
            return []

    @staticmethod
    def search_community(query: object, db_path: Path | str | None = None,
                         *, limit: int = _MAX_RESULTS) -> list[dict[str, Any]]:
        """Read bounded, cited excerpts from the local community wiki FTS cache."""
        terms = _query_terms(query)
        if (not terms or type(limit) is not int or not 1 <= limit <= _MAX_RESULTS):
            return []
        path = community_db_path(db_path)
        if not path.is_file():
            return []
        match = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
        try:
            uri = path.resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=1.0)) as connection:
                connection.execute("PRAGMA query_only=ON")
                rows = connection.execute(
                    "SELECT pages.pageid, pages.title, pages.revision, pages.timestamp, "
                    "pages.extract, page_sources.url, page_sources.publisher, "
                    "page_sources.retrieved_at FROM page_search "
                    "JOIN pages ON pages.pageid = page_search.rowid "
                    "LEFT JOIN page_sources ON page_sources.pageid = pages.pageid "
                    "JOIN wiki_urls ON wiki_urls.url = page_sources.url "
                    "WHERE page_search MATCH ? AND pages.extract != '' "
                    "AND wiki_urls.status = 'indexed' "
                    "AND page_sources.publisher = 'Star Citizen Wiki' "
                    "ORDER BY bm25(page_search), pages.title LIMIT ?",
                    (match, limit),
                ).fetchall()
        except (OSError, sqlite3.Error, ValueError):
            return []
        results = []
        for (
            pageid, title, revision, timestamp, extract, source_url, publisher, retrieved_at
        ) in rows:
            if (type(pageid) is not int or pageid <= 0
                    or (revision is not None and (type(revision) is not int or revision <= 0))
                    or type(title) is not str or not title.strip()
                    or (timestamp is not None
                        and (type(timestamp) is not str or len(timestamp) > 40))
                    or type(extract) is not str or not extract.strip()
                    or type(source_url) is not str or not source_url.startswith("https://")
                    or type(publisher) is not str or not publisher.strip()
                    or type(retrieved_at) is not str or len(retrieved_at) > 40):
                continue
            if not _eligible_public_citation(source_url):
                continue
            is_community = publisher == "Star Citizen Wiki"
            citation = (
                _COMMUNITY_SOURCE.format(revision)
                if is_community and type(revision) is int else source_url
            )
            results.append({
                "title": title[:256],
                "excerpt": _excerpt(extract),
                "revision": revision,
                "timestamp": timestamp,
                "retrieved_at": retrieved_at,
                "publisher": publisher,
                "citation": citation,
                "source_kind": ("community_wiki_unverified_reference" if is_community
                                else "locally_cached_publisher_reference"),
                "attribution": ("Star Citizen Wiki, CC BY-SA 4.0" if is_community
                                else publisher),
                "game_build": None,
                "game_build_state": "unknown",
                "live_state": False,
            })
        return results

    @staticmethod
    def search_catalog(query: object, db_path: Path | str | None = None,
                       *, limit: int = _MAX_RESULTS) -> list[dict[str, Any]]:
        """Return cited title/link candidates; body vocabulary is discovery, not evidence."""
        terms = _query_terms(query)
        if not terms or type(limit) is not int or not 1 <= limit <= _MAX_RESULTS:
            return []
        path = community_db_path(db_path)
        if not path.is_file():
            return []
        match = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
        try:
            uri = path.resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=1.0)) as connection:
                connection.execute("PRAGMA query_only=ON")
                title_rows = connection.execute(
                    "SELECT catalog_search.url, catalog_search.title, catalog_search.publisher "
                    "FROM catalog_search JOIN wiki_urls "
                    "ON wiki_urls.url = catalog_search.url "
                    "WHERE catalog_search MATCH ? AND wiki_urls.status != 'removed' "
                    "ORDER BY bm25(catalog_search), title LIMIT ?",
                    (match, limit),
                ).fetchall()
                has_vocabulary = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='page_vocabulary'"
                ).fetchone()
                if has_vocabulary:
                    body_rows = connection.execute(
                        "SELECT catalog_search.url, catalog_search.title, catalog_search.publisher "
                        "FROM page_vocabulary "
                        "JOIN page_sources ON page_sources.pageid = page_vocabulary.rowid "
                        "JOIN wiki_urls ON wiki_urls.url = page_sources.url "
                        "JOIN catalog_search ON catalog_search.url = page_sources.url "
                        "WHERE page_vocabulary MATCH ? AND wiki_urls.status != 'removed' "
                        "AND page_sources.publisher = 'Star Citizen Wiki' "
                        "ORDER BY bm25(page_vocabulary), catalog_search.title LIMIT ?",
                        (match, limit),
                    ).fetchall()
                else:
                    # Existing databases acquire this index on their next paced refresh.
                    body_rows = []
        except (OSError, sqlite3.Error, ValueError):
            return []
        results = []
        seen = set()
        for rows, matched_on in ((body_rows, "body_vocabulary"), (title_rows, "title")):
            for url, title, publisher in rows:
                if (type(url) is not str or type(title) is not str or not title.strip()
                    or type(publisher) is not str or not publisher.strip()):
                    continue
                if url in seen or not _eligible_public_citation(url):
                    continue
                seen.add(url)
                results.append({
                    "title": title[:256], "citation": url,
                    "publisher": publisher[:128],
                    "source_kind": "cached_catalog_candidate",
                    "matched_on": matched_on,
                    "insufficient_evidence": True,
                    "game_build": None, "game_build_state": "unknown", "live_state": False,
                })
        return results[:limit]
