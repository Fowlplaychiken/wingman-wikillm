"""Per-library choices for extra wiki and public JSON sources."""
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass

from .json_feeds import JsonFeed, parse_feeds
from .reference import community_db_path
from .web_sources import parse_roots


@dataclass(frozen=True)
class _Source:
    value: str | JsonFeed
    enabled: bool
    origin: str

    @property
    def url(self):
        return self.value.url if isinstance(self.value, JsonFeed) else self.value


_SCHEMA = """CREATE TABLE IF NOT EXISTS source_choices(
    kind TEXT NOT NULL, url TEXT NOT NULL, added_definition TEXT,
    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
    setting_at_change INTEGER CHECK(setting_at_change IN (0,1)),
    PRIMARY KEY(kind,url))"""


def _one(kind, declaration):
    if not isinstance(declaration, str) or len(declaration.strip().splitlines()) != 1:
        raise ValueError("Supply one public wiki or API address at a time.")
    parser = parse_roots if kind == "wiki" else parse_feeds
    values = parser(declaration.strip())
    if len(values) != 1:
        raise ValueError("Supply a public address or a JSON API field mapping.")
    return values[0]


def _configured(roots, feeds):
    result = {}
    for kind, declarations in (("wiki", roots), ("api", feeds)):
        if isinstance(declarations, str):
            declarations = declarations.splitlines()
        for line in declarations:
            if isinstance(line, JsonFeed) and kind == "api":
                line = json.dumps(asdict(line))
            if not isinstance(line, str):
                raise ValueError("Source settings must be URL or mapping lines.")
            line = line.strip()
            if not line:
                continue
            enabled = not line.startswith("#")
            try:
                value = _one(kind, line if enabled else line[1:].strip())
            except ValueError:
                if enabled:
                    raise
                continue  # Ordinary comments need not be source declarations.
            source = _Source(value, enabled, "settings")
            key = (kind, source.url)
            prior = result.get(key)
            if prior and prior.enabled:
                if enabled and prior.value != value:
                    raise ValueError("Conflicting field mappings for one collection.")
                continue
            result[key] = source
    return result


def _choices(connection, configured):
    result = configured.copy()
    if connection is None or not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_choices'"
    ).fetchone():
        return result
    for kind, url, definition, enabled, baseline in connection.execute(
        "SELECT kind,url,added_definition,enabled,setting_at_change FROM source_choices ORDER BY rowid"
    ):
        key = (kind, url)
        entry = configured.get(key)
        if entry is not None:
            # A new setting, or changing its # prefix after a voice command,
            # takes precedence over the earlier choice.
            if baseline == entry.enabled:
                result[key] = _Source(entry.value, bool(enabled), "settings")
        elif definition is not None:
            result[key] = _Source(_one(kind, definition), bool(enabled), "voice")
    return result


def _read_choices(db, configured):
    path = community_db_path(db)
    if not path.is_file():
        return configured
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)) as conn:
        return _choices(conn, configured)


def effective_sources(db, roots=(), feeds=()):
    """Return enabled wiki roots and JsonFeed mappings without creating a database."""
    choices = _read_choices(db, _configured(roots, feeds))
    return tuple(s.value for (k, _), s in choices.items() if k == "wiki" and s.enabled), tuple(
        s.value for (k, _), s in choices.items() if k == "api" and s.enabled
    )


def _result(choices, status, message):
    sources = [{"kind": kind, "url": item.url, "enabled": item.enabled, "origin": item.origin}
               for (kind, _), item in choices.items()]
    return {"status": status, "message": message, "sources": sources,
            "wiki_count": sum(row["enabled"] for row in sources if row["kind"] == "wiki"),
            "api_count": sum(row["enabled"] for row in sources if row["kind"] == "api")}


def manage_source(db, action, source=None, kind="wiki", roots=(), feeds=()):
    """List, add, pause, or resume one source. All operations are local."""
    if kind not in ("wiki", "api") or action not in ("list", "add", "pause", "resume"):
        raise ValueError("Choose list, add, pause, or resume and a source kind of wiki or api.")
    configured = _configured(roots, feeds)
    if action == "list":
        choices = _read_choices(db, configured)
        return _result(choices, "listed", f"{len(choices)} extra sources in this library.")
    line = source.strip() if isinstance(source, str) else ""
    commented = line.startswith("#")
    value = _one(kind, line[1:].strip() if commented else line)
    url = value.url if isinstance(value, JsonFeed) else value
    path = community_db_path(db)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=3)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(_SCHEMA)
        choices = _choices(conn, configured)
        current = choices.get((kind, url))
        if action == "add" and current:
            state = "active" if current.enabled else "paused"
            return _result(choices, state, f"{url} is already {state}.")
        if action != "add" and current is None:
            return _result(choices, "not_found", f"{url} is not in this library's extra sources.")
        added = None
        if action == "add":
            added = json.dumps(asdict(value)) if isinstance(value, JsonFeed) else value
        enabled = (not commented) if action == "add" else action == "resume"
        setting = configured.get((kind, url))
        conn.execute("""
            INSERT INTO source_choices(kind,url,added_definition,enabled,setting_at_change)
            VALUES(?,?,?,?,?) ON CONFLICT(kind,url) DO UPDATE SET
              added_definition=COALESCE(excluded.added_definition,source_choices.added_definition),
              enabled=excluded.enabled,setting_at_change=excluded.setting_at_change
            """, (kind, url, added, enabled, setting.enabled if setting else None))
        state = "active" if enabled else "paused"
        return _result(_choices(conn, configured), state, f"{url} is now {state}.")
