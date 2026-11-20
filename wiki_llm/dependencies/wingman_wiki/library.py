"""Offline search and revision history for a player's own notes."""
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urldefrag, urlsplit

from .reference import WikiReference, _build_key, _query_terms, community_db_path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS note_revisions (
    slug TEXT NOT NULL, revision INTEGER NOT NULL, recorded_at TEXT NOT NULL,
    title TEXT NOT NULL, finding TEXT NOT NULL, source TEXT, game_build TEXT,
    status TEXT NOT NULL, PRIMARY KEY(slug, revision)
);
CREATE VIRTUAL TABLE IF NOT EXISTS current_notes USING fts5(
    slug UNINDEXED, title, finding, tokenize='porter unicode61'
);
"""


def record_note(slug, title, finding, *, status, source=None, game_build=None,
                previous_revision=0, db_path=None):
    """Append one revision. Parallel changes cannot overwrite one another."""
    if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug) or len(slug) > 100:
        raise ValueError("Use a short topic ID such as aberdeen-visibility.")
    if status not in {"draft", "reviewed", "withdrawn"}:
        raise ValueError("Select draft, reviewed or withdrawn.")
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 180:
        raise ValueError("Give the note a short title.")
    if not isinstance(finding, str) or not 1 <= len(finding.strip()) <= 4000:
        raise ValueError("Describe one finding or the reason for a correction.")
    if game_build and (not isinstance(game_build, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", game_build)):
        raise ValueError("Specify the observed build or omit it.")
    if source:
        parts = urlsplit(source)
        if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
                or len(source) > 2000 or any(c.isspace() for c in source)):
            raise ValueError("Supply the original public HTTPS link or omit it for a local observation.")
    if type(previous_revision) is not int or previous_revision < 0:
        raise ValueError("Supply the last revision for this topic.")
    db_path = community_db_path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as db, db:
        db.executescript(_SCHEMA)
        db.execute("BEGIN IMMEDIATE")
        current = db.execute("SELECT coalesce(max(revision),0) FROM note_revisions WHERE slug=?", (slug,)).fetchone()[0]
        if current != previous_revision:
            raise ValueError(f"This topic is at revision {current}. Read its history before editing.")
        revision = current + 1
        db.execute("INSERT INTO note_revisions VALUES(?,?,?,?,?,?,?,?)", (
            slug, revision, datetime.now(timezone.utc).isoformat(timespec="seconds"),
            title.strip(), finding.strip(), source or None, game_build or None, status))
        if status != "draft":
            db.execute("DELETE FROM current_notes WHERE slug=?", (slug,))
            if status == "reviewed":
                db.execute("INSERT INTO current_notes VALUES(?,?,?)", (slug, title, finding))
    return {"topic": slug, "revision": revision, "status": status}


def _read(db_path, sql, args, table):
    path = community_db_path(db_path)
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
            return []
        return [dict(row) for row in db.execute(sql, args)]


def note_history(slug, db_path=None, before=None):
    return _read(db_path, """SELECT * FROM note_revisions WHERE slug=?
                 AND (? IS NULL OR revision < ?) ORDER BY revision DESC LIMIT 10""",
                 (slug, before, before), "note_revisions")


def _source_changes(references, db_path):
    """Relate local reviews to publisher changes already observed by the updater."""
    for ref in references:
        urls = {urldefrag(url)[0] for url in ref.get("source_links", [])}
        if ref.get("citation", "") and ref["citation"].startswith("https://"):
            urls.add(urldefrag(ref["citation"])[0])
        stamp = (ref.get("reviewed") or ref.get("recorded_at") or "")[:10]
        if not urls or not stamp:
            continue
        slots = ",".join("?" for _ in urls)
        evidence = _read(db_path, f"SELECT url,lastmod,status FROM wiki_urls WHERE url IN ({slots})",
                         tuple(urls), "wiki_urls")
        changes = [row for row in evidence if row["status"] == "removed"
                   or (row["lastmod"] or "")[:10] > stamp]
        if changes:
            ref["needs_review"] = True
            ref["changed_sources"] = changes


def lookup(guides: Path, query: str, game_build=None, db_path=None):
    db_path = community_db_path(db_path)
    curated = WikiReference.search_curated(guides, query, game_build=game_build)
    terms = _query_terms(query)
    if not terms or (game_build is not None and _build_key(game_build) is None):
        return {"status": "invalid_query", "references": [], "discovery_links": []}
    match = " OR ".join('"' + term + '"' for term in terms)
    notes = _read(db_path, """
        SELECT n.slug, n.title, n.finding AS excerpt, n.source AS citation, n.game_build,
               n.revision, n.recorded_at FROM current_notes JOIN note_revisions n
          ON n.slug=current_notes.slug AND n.revision=(
              SELECT max(r.revision) FROM note_revisions r WHERE r.slug=n.slug AND r.status!='draft')
        WHERE current_notes MATCH ? AND (? IS NULL OR lower(n.game_build)=?)
        ORDER BY current_notes.rank, n.slug LIMIT 3""",
        (match, _build_key(game_build), _build_key(game_build)), "current_notes")
    for row in notes:
        row["source_kind"] = "reviewed_local_note" if row["citation"] else "local_observation"
        row["live_state"] = False
    manual = WikiReference.search_notes(db_path.parent / "notes", query, game_build=game_build)
    cached = WikiReference.search_community(query, db_path) if game_build is None else []
    references = curated.get("references", []) + notes + manual + cached
    _source_changes(references, db_path)
    return {"status": "reference" if references else "no_matching_evidence",
            "references": references, "live_state": False,
            "discovery_links": [] if references or game_build is not None else
            WikiReference.search_catalog(query, db_path)}
