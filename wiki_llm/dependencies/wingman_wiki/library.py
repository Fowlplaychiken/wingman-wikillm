"""Offline search and revision history for a player's own notes."""
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urldefrag, urlsplit

from . import corpus
from .coverage import SQLITE_WHITESPACE, full_pages, renamed_articles, verify_inventory
from .snapshots import read_snapshot_page, search_snapshots
from .web_sources import in_scope, queue_inventory_urls, read_web_page, search_web_sources, web_coverage
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
            slug, revision, datetime.now(timezone.utc).isoformat(timespec="microseconds"),
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
    by_url = {}
    for ref in references:
        urls = {urldefrag(url)[0] for url in ref.get("source_links", [])}
        if ref.get("citation", "") and ref["citation"].startswith("https://"):
            urls.add(urldefrag(ref["citation"])[0])
        stamp = ref.get("reviewed") or ref.get("recorded_at") or ""
        if not urls or not stamp:
            continue
        for url in urls:
            by_url.setdefault(url, []).append((ref, stamp))
    if not by_url:
        return
    urls = tuple(by_url)
    slots = ",".join("?" for _ in urls)
    rows = _read(db_path, f"SELECT url,lastmod,status FROM wiki_urls WHERE url IN ({slots})", urls, "wiki_urls")
    rows += _read(db_path, f"SELECT url,observed_at,reason FROM source_changes WHERE url IN ({slots})",
                  urls, "source_changes")
    for row in rows:
        for ref, stamp in by_url[row["url"]]:
            if (row.get("status") == "removed" or (row.get("lastmod") or "")[:10] > stamp[:10]
                    or row.get("observed_at", "") > stamp):
                ref["needs_review"] = True
                ref.setdefault("changed_sources", []).append(row)


def review_queue(db_path=None, *, after=None, limit=10):
    """List the latest drafts and reviewed notes affected by source changes."""
    if type(limit) is not int or not 1 <= limit <= 50 or (after is not None and not isinstance(after, str)):
        raise ValueError("Use a topic ID as the cursor and request 1 to 50 notes.")
    cursor, notes = after or "", []
    while len(notes) <= limit:
        batch = _read(db_path, """SELECT n.slug,n.title,n.revision,n.status,n.source AS citation,n.recorded_at
            FROM note_revisions n WHERE n.slug>? AND n.status!='withdrawn'
            AND n.revision=(SELECT max(r.revision) FROM note_revisions r WHERE r.slug=n.slug)
            ORDER BY n.slug LIMIT 100""", (cursor,), "note_revisions")
        if not batch:
            break
        _source_changes(batch, db_path)
        for row in batch:
            if row["status"] == "draft" or row.get("needs_review"):
                row["reason"] = "draft" if row["status"] == "draft" else "source_changed"
                notes.append(row)
        cursor = batch[-1]["slug"]
        if len(batch) < 100:
            break
    return {"notes": notes[:limit], "next_after": notes[limit-1]["slug"] if len(notes) > limit else None}


def _selected(url, include_star_citizen, roots):
    if not isinstance(url, str) or not url.startswith("https://"):
        return False
    return (include_star_citizen and corpus.indexable(url)) or any(in_scope(url, root) for root in roots)


def read_page(db_path, url, *, offset=0, include_star_citizen=True, roots=()):
    """Read an article from the currently enabled local sources."""
    if type(offset) is not int or offset < 0:
        raise ValueError("Use an offset returned by the previous read.")
    if not isinstance(url, str):
        return {"status": "unavailable"}
    permanent = re.fullmatch(r"https://starcitizen\.tools/index\.php\?oldid=([1-9][0-9]{0,17})", url or "")
    selected = _selected(url, include_star_citizen, roots)
    if not selected and not (include_star_citizen and permanent):
        return {"status": "unavailable"}
    if selected:
        # Galactapedia keeps its article ID when the publisher changes a title
        # slug. A player's earlier citation can still open the current edition.
        if include_star_citizen and url.startswith(corpus.GALACTAPEDIA + "/article/"):
            path = community_db_path(db_path)
            canonical = renamed_articles(path, url).get(url)
            if canonical:
                current = read_snapshot_page(path, canonical, offset)
                if current["status"] != "not_found":
                    return {**current, "requested_url": url, "renamed": True}
        saved = read_snapshot_page(community_db_path(db_path), url, offset)
        if saved["status"] != "not_found":
            return saved
    if include_star_citizen and (permanent or corpus.indexable(url)):
        # A community citation may refer to an exact revision with a bounded cached excerpt.
        cached = _read(db_path, """SELECT p.title,p.extract,p.revision,s.url FROM pages p
            JOIN page_sources s ON p.pageid=s.pageid JOIN wiki_urls u ON u.url=s.url AND u.status='indexed'
            WHERE s.publisher='Star Citizen Wiki' AND p.extract!='' AND (s.url=? OR p.revision=?) LIMIT 1""",
            (url, int(permanent[1]) if permanent else None), "page_sources")
        if cached:
            item = cached[0]
            return {"status": "reference", "citation": url, "title": item["title"],
                    "text": item["extract"][offset:], "storage_mode": "short_excerpt",
                    "revision": item["revision"], "next_offset": None, "live_state": False}
    return read_web_page(community_db_path(db_path), url, roots, offset=offset)


def check_inventory(db_path, manifest, *, include_star_citizen=True, roots=(), queue_missing=False):
    path = community_db_path(db_path)
    return verify_inventory(path, manifest, lambda url: _selected(url, include_star_citizen, roots),
                            queue_undiscovered=(lambda urls: queue_inventory_urls(path, roots, urls)) if queue_missing else None)


def library_status(db_path=None, *, include_star_citizen=True, roots=()):
    path = community_db_path(db_path)
    allowed = lambda url: _selected(url, include_star_citizen, roots)
    full = full_pages(path)
    imports = []
    imported_total = 0
    historical_articles = 0
    if path.is_file():
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='snapshot_pages'").fetchone():
                db.create_function("enabled", 1, allowed)
                imports = [{"origin": host, "saved_articles": count, "full_text_articles": count}
                           for host, count in db.execute(
                    "SELECT source_origin,count(distinct url) FROM snapshot_pages "
                    "WHERE enabled(url) AND trim(body, ?) != '' GROUP BY source_origin",
                    (SQLITE_WHITESPACE,))]
                imported_total = db.execute(
                    "SELECT count(distinct url) FROM snapshot_pages "
                    "WHERE enabled(url) AND trim(body, ?) != ''",
                    (SQLITE_WHITESPACE,)).fetchone()[0]
                if include_star_citizen:
                    historical_articles = db.execute("""
                        SELECT count(DISTINCT p.url) FROM snapshot_pages p
                        JOIN snapshot_sources s USING(source_id)
                        WHERE s.file_format='starcitizen-archive-20230930'
                    """).fetchone()[0]
    status = {"star_citizen_enabled": include_star_citizen, "custom_wikis": web_coverage(path, roots),
              "imported_articles": imports}
    for root, info in status["custom_wikis"].items():
        discovered = _read(path, "SELECT url,status FROM web_pages WHERE root=? AND status NOT IN ('removed','redirected')",
                           (root,), "web_pages")
        known = len(discovered)
        saved = sum(row["url"] in full and row["status"] != "blocked" for row in discovered)
        info.update(known_article_urls=known, full_text_for_known_urls=saved, missing_full_text=known-saved,
                    inventory_basis="discovered URLs only")
    if include_star_citizen:
        counts = {}
        acquisitions = {
            "community": "automatic_paced_excerpts",
            "support": "local_import_only",
            "official": "local_import_only",
            "galactapedia": "community_api_or_local_import",
        }
        catalog_names = ("community", "support", "official", "galactapedia")
        last_updates = {row["name"]: row for row in _read(
            path, "SELECT name,last_success,last_error FROM refresh_catalogs", (), "refresh_catalogs")}
        for row in _read(path, """SELECT u.url, coalesce(p.extract != '', 0) AS has_excerpt
                FROM wiki_urls u LEFT JOIN pages p ON p.pageid=u.pageid
                WHERE u.status!='removed'""", (), "wiki_urls"):
            group = counts.setdefault(corpus.catalog_owner(row["url"]), {"titles": 0, "excerpts": 0, "full_text_for_known_urls": 0, "missing_full_text": 0})
            group["titles"] += 1
            group["excerpts"] += row["has_excerpt"]
            group["full_text_for_known_urls"] += row["url"] in full
            group["missing_full_text"] += row["url"] not in full
        for name in catalog_names:
            group = counts.setdefault(name, {"titles": 0, "excerpts": 0,
                "full_text_for_known_urls": 0, "missing_full_text": 0})
            group.update(indexed_titles=group["titles"], saved_excerpts=group["excerpts"],
                         full_text_articles=group["full_text_for_known_urls"],
                         automatic_acquisition=acquisitions[name],
                         coverage_basis="cached title URLs; not a complete publisher inventory")
            source_state = last_updates.get(name, {})
            group.update(last_success=source_state.get("last_success"),
                         last_error=source_state.get("last_error"))
        from .galactapedia_feed import feed_status
        feed = status["galactapedia_feed"] = feed_status(path)
        if feed["last_success"] is not None:
            counts["galactapedia"]["coverage_basis"] = "validated Star Citizen Wiki API Galactapedia inventory"
            last_updates["galactapedia"] = {"last_success": feed["last_success"], "last_error": feed["last_error"]}
            counts["galactapedia"].update(last_updates["galactapedia"])
        status.update(star_citizen_library=counts, last_updates=[
            {"name": name, **last_updates.get(name, {"last_success": None, "last_error": None}),
             "automatic_acquisition": acquisitions[name]}
            for name in catalog_names])
        # wiki_urls has one row per URL, and catalog_owner assigns each row to
        # exactly one bucket, so these sums describe unique known URLs.
        totals = {
            "enabled": True,
            "unique_known_title_urls": sum(counts[name]["titles"] for name in catalog_names),
            "saved_community_excerpt_urls": counts["community"]["excerpts"],
            "full_text_for_known_urls": sum(
                counts[name]["full_text_for_known_urls"] for name in catalog_names),
            "missing_full_text_for_known_urls": sum(
                counts[name]["missing_full_text"] for name in catalog_names),
        }
        status["coverage_totals"] = {
            "star_citizen_preset": totals,
            "all_enabled_local_imports": {"unique_article_urls": imported_total},
            "private_notes": {"included_in_coverage_totals": False},
        }
        sources = "; ".join(
            f"{name} {counts[name]['titles']}" for name in catalog_names)
        status["coverage_summary"] = (
            f"Star Citizen built-in preset: {totals['unique_known_title_urls']:,} unique known title URLs "
            f"({sources}); {totals['saved_community_excerpt_urls']:,} saved community excerpt URLs; "
            f"full text for {totals['full_text_for_known_urls']:,} of "
            f"{totals['unique_known_title_urls']:,} known URLs, with "
            f"{totals['missing_full_text_for_known_urls']:,} missing. Separately, "
            f"{imported_total:,} distinct local imported article URLs are enabled across all selected sources "
            "(see imported_articles by origin); imports can overlap known-URL coverage and are not added "
            "to the preset total. Private notes are excluded from coverage totals."
        )
        if historical_articles:
            status["historical_community_edition"] = {
                "date": "2023-09-30", "articles": historical_articles,
                "source": "https://archive.org/details/wiki-starcitizen.tools-20230930",
                "current_revisions_verified": False, "verification_basis": "dated archive",
            }
            status["coverage_summary"] += (
                f" The September 2023 community archive contains {historical_articles:,} saved articles. "
                "Full-text counts include historical revisions and do not establish current patch coverage."
            )
        if feed["last_success"] is not None:
            status["coverage_summary"] += (
                f" The Galactapedia feed lists {feed['titles']:,} entries. It supplied "
                f"{feed['article_bodies']:,} English articles, with {feed['placeholders']:,} placeholders "
                f"and {feed['missing']:,} missing translations. Older text is retained for "
                f"{feed['retained_prior_bodies']:,} of those gaps."
            )
    else:
        status["coverage_totals"] = {
            "star_citizen_preset": {"enabled": False},
            "all_enabled_local_imports": {"unique_article_urls": imported_total},
            "private_notes": {"included_in_coverage_totals": False},
        }
        status["coverage_summary"] = (
            "Star Citizen built-in preset is disabled; no preset coverage totals are reported. "
            f"Total enabled local imported article URLs (all imports): {imported_total:,} distinct "
            "across all selected sources (see imported_articles by origin). Private notes are excluded "
            "from coverage totals."
        )
    return status


def lookup(guides: Path, query: str, game_build=None, db_path=None, *, include_star_citizen=True, roots=()):
    db_path = community_db_path(db_path)
    curated = WikiReference.search_curated(guides, query, game_build=game_build) if include_star_citizen else {}
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
    cached = WikiReference.search_community(query, db_path) if include_star_citizen and game_build is None else []
    articles = search_snapshots(query, db_path, allow_url=lambda url: _selected(url, include_star_citizen, roots),
                                game_build=game_build)
    web = search_web_sources(query, db_path, roots) if roots and game_build is None else []
    references = curated.get("references", []) + notes + manual + articles + web + cached
    _source_changes(references, db_path)
    return {"status": "reference" if references else "no_matching_evidence",
            "references": references, "live_state": False,
            "discovery_links": [] if references or game_build is not None or not include_star_citizen else
            WikiReference.search_catalog(query, db_path)}
