"""Offline coverage of configured sources and a publisher-supplied article inventory."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path

SQLITE_WHITESPACE = "\t\n\v\f\r \x1c\x1d\x1e\x1f\x85\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"


def full_pages(db_path):
    """Map original URLs to the revisions whose article text is saved locally."""
    path = Path(db_path)
    pages = {}
    if not path.is_file():
        return pages
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "snapshot_pages" in tables:
            # Keep the large article bodies in SQLite. This also excludes
            # whitespace-only rows left by older importer versions.
            for url, revision in db.execute(
                    "SELECT url,revision FROM snapshot_pages WHERE trim(body, ?) != ''",
                    (SQLITE_WHITESPACE,)):
                pages.setdefault(url, set()).add(revision)
        if "web_pages" in tables:
            for url, in db.execute("SELECT DISTINCT url FROM web_pages WHERE status='indexed' AND storage_mode='full_text'"):
                pages.setdefault(url, set()).add(None)
    return pages


def verify_inventory(db_path, manifest, allowed):
    """Check every expected URL and optional revision without contacting a wiki."""
    saved = full_pages(db_path)
    seen = {}
    counts = {"present": 0, "missing_text": 0, "revision_unverified": 0, "disabled": 0}
    examples = []
    with Path(manifest).open(encoding="utf-8-sig") as source:
        for number, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                url = row["url"]
                if not isinstance(url, str) or not url.startswith("https://"):
                    raise ValueError
                revision = row.get("revision")
                if revision is not None:
                    if type(revision) not in (int, str) or str(revision) == "":
                        raise ValueError
                    revision = str(revision)
            except (TypeError, ValueError, KeyError) as exc:
                raise ValueError(f"Invalid URL or revision on inventory line {number}.") from exc
            if url in seen:
                if seen[url] != revision:
                    raise ValueError(f"Conflicting revisions on inventory line {number}.")
                continue
            seen[url] = revision
            reason = "present"
            if not allowed(url):
                reason = "disabled"
            elif url not in saved:
                reason = "missing_text"
            elif revision is not None and revision not in saved[url]:
                reason = "revision_unverified"
            counts[reason] += 1
            if reason != "present" and len(examples) < 10:
                examples.append({"url": url, "reason": reason})
    if not seen:
        raise ValueError("The article inventory is empty.")
    return {"status": "matches_inventory" if counts["present"] == len(seen) else "incomplete",
            "expected_articles": len(seen), **counts, "first_gaps": examples}
