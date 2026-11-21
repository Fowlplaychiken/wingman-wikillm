"""Incremental public-source refresh, independent of offline lookup.

Official sources supply catalog titles/links; only community pages supply short
attributed excerpts. No model, API key, Wingman Core or private project required.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from . import corpus

SCHEMA = """
CREATE TABLE IF NOT EXISTS refresh_catalogs(
 name TEXT PRIMARY KEY, last_attempt REAL, last_success REAL, last_error TEXT);
CREATE TABLE IF NOT EXISTS refresh_pages(
 url TEXT PRIMARY KEY, last_attempt REAL, last_success REAL,
 next_attempt REAL NOT NULL DEFAULT 0, last_error TEXT);
CREATE TABLE IF NOT EXISTS refresh_hosts(host TEXT PRIMARY KEY, last_request REAL NOT NULL,
 blocked_until REAL NOT NULL DEFAULT 0,last_phase TEXT,last_error TEXT,last_http_status INTEGER);
CREATE TABLE IF NOT EXISTS refresh_queue(id INTEGER PRIMARY KEY CHECK(id=1),prefer_existing INTEGER NOT NULL);
"""
CATALOGS = ("community", "support", "official", "galactapedia")
AUTOMATIC_CATALOGS = ("community",)


def _enabled_catalogs(values):
    if values is None:
        # RSI Terms section IV does not permit automated collection. Keep the
        # paced community-wiki route, and leave existing RSI title caches intact;
        # an authorized caller may still opt into a separate supplied feed.
        return AUTOMATIC_CATALOGS
    if isinstance(values, (str, bytes)):
        raise ValueError("enabled_catalogs must be an iterable of catalog names")
    try:
        selected = tuple(values)
    except TypeError as exc:
        raise ValueError("enabled_catalogs must be an iterable of catalog names") from exc
    if any(type(name) is not str or name not in CATALOGS for name in selected):
        raise ValueError("unknown enabled catalog")
    return tuple(name for name in CATALOGS if name in selected)


@dataclass
class RefreshReport:
    catalogs: dict[str, dict] = field(default_factory=dict)
    indexed: int = 0
    failed: int = 0
    cancelled: bool = False
    leased_elsewhere: bool = False
    coverage: dict[str, dict] = field(default_factory=dict)
    deferred: int = 0
    sources: dict = field(default_factory=dict)

    def building_library(self):
        community = self.coverage.get("community", {})
        if self.indexed and community.get("catalog_urls", 0) > community.get("indexed_pages", 0):
            return True
        wikis = self.sources.get("wikis", {})
        return bool(wikis.get("indexed") and any(
            source.get("pages", {}).get("pending") for source in wikis.get("coverage", {}).values()))


def _owner(url):
    return corpus.catalog_owner(url)


def _cancelled(stop_event):
    if stop_event is not None and stop_event.is_set():
        raise corpus.RefreshCancelled()


def _catalog(publisher, name, stop_event):
    records = {}
    items = (publisher.galactapedia() if name == "galactapedia" else
             corpus.sitemap_entries(publisher, corpus.SITEMAPS[CATALOGS.index(name)], set()))
    for url, value in items:
        _cancelled(stop_event)
        if not corpus.indexable(url) or _owner(url) != name:
            continue
        records[url] = value
        if len(records) > 100_000:
            raise ValueError("catalog exceeds bounded capacity")
    if not records:
        raise ValueError("empty catalog cannot replace last good catalog")
    return records


def _apply_catalog(db, name, records, observed_at):
    # Publish one complete source atomically. Other sources and last-good pages
    # survive partial discovery, robots failures, and malformed feeds.
    corpus.apply_catalog(db, name, records, observed_at)


def _refresh_once(db_path: str | Path, *, batch_size: int = 5,
                 catalog_interval: float = 86_400, page_interval: float = 604_800,
                 publisher=None, stop_event: threading.Event | None = None,
                 clock=time.time, enabled_catalogs=None) -> RefreshReport:
    """Refresh due catalogs independently, then a bounded community-page batch.

    Failure backoff is at least five minutes; successful verification is paced
    by page_interval. A cancelled pass keeps every previously committed source.
    """
    if type(batch_size) is not int or not 1 <= batch_size <= 100:
        raise ValueError("batch_size must be 1..100")
    if not all(type(x) in (int, float) and 60 <= x <= 31_536_000
               for x in (catalog_interval, page_interval)):
        raise ValueError("refresh intervals must be 60 seconds..1 year")
    enabled = _enabled_catalogs(enabled_catalogs)
    path = Path(db_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    source = publisher if publisher is not None else corpus.Publisher(stop_event)
    report = RefreshReport()
    with closing(sqlite3.connect(path, timeout=2)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(corpus.SCHEMA + SCHEMA)
        columns = {x[1] for x in db.execute("PRAGMA table_info(refresh_hosts)")}
        for name, definition in (("blocked_until", "REAL NOT NULL DEFAULT 0"), ("last_phase", "TEXT"),
                                 ("last_error", "TEXT"), ("last_http_status", "INTEGER")):
            if name not in columns:
                db.execute(f"ALTER TABLE refresh_hosts ADD COLUMN {name} {definition}")
        db.commit()
        if isinstance(source, corpus.Publisher):
            source.clock = clock
            for host, stamp, until in db.execute("SELECT host,last_request,blocked_until FROM refresh_hosts"):
                source.last[host] = time.monotonic() - max(0, clock() - stamp)
                source.blocked_until[host] = until
            def request_started(host, phase="unknown"):
                with db:
                    db.execute("""INSERT INTO refresh_hosts(host,last_request,last_phase) VALUES(?,?,?)
                        ON CONFLICT(host) DO UPDATE SET last_request=excluded.last_request,
                        last_phase=excluded.last_phase,last_error=NULL,last_http_status=NULL""", (host, clock(), phase))
            source.request_started = request_started
            def request_failed(host, phase, error_type, status, until):
                with db:
                    db.execute("""UPDATE refresh_hosts SET last_phase=?,last_error=?,last_http_status=?,
                        blocked_until=max(blocked_until,?) WHERE host=?""", (phase, error_type, status, until, host))
            source.request_failed = request_failed
        try:
            for name in CATALOGS:
                _cancelled(stop_event)
                if name not in enabled:
                    report.catalogs[name] = {"state": "disabled"}
                    continue
                now = clock()
                state = db.execute("SELECT last_attempt,last_success,last_error FROM refresh_catalogs WHERE name=?", (name,)).fetchone()
                delay = min(catalog_interval, 900) if state and state[2] else catalog_interval
                if state and now - state[0] < delay:
                    report.catalogs[name] = {"state": "not_due", "stale": bool(state[2])}
                    continue
                try:
                    records = _catalog(source, name, stop_event)
                    _cancelled(stop_event)
                    with db:
                        _apply_catalog(db, name, records, datetime.fromtimestamp(now, UTC).isoformat(timespec="microseconds"))
                        db.execute("""INSERT INTO refresh_catalogs VALUES(?,?,?,NULL)
                            ON CONFLICT(name) DO UPDATE SET last_attempt=excluded.last_attempt,
                            last_success=excluded.last_success,last_error=NULL""", (name, now, now))
                    report.catalogs[name] = {"state": "refreshed", "discovered": len(records), "stale": False}
                except corpus.RefreshCancelled:
                    raise
                except Exception as exc:
                    with db:
                        db.execute("""INSERT INTO refresh_catalogs(name,last_attempt,last_error) VALUES(?,?,?)
                            ON CONFLICT(name) DO UPDATE SET last_attempt=excluded.last_attempt,last_error=excluded.last_error""",
                            (name, now, type(exc).__name__))
                    report.catalogs[name] = {"state": "failed", "error_type": type(exc).__name__, "stale": True}
            now = clock()
            # Alternate revalidation/retries and discovery, including across
            # one-item batches/processes. A large never-read catalog cannot starve
            # due content; conversely overdue content cannot consume every slot.
            queues = {0: [], 1: []}
            for existing in ((0, 1) if "community" in enabled else ()):
                queues[existing] = list(db.execute("""SELECT u.url FROM wiki_urls u
                    LEFT JOIN refresh_pages r ON r.url=u.url
                    WHERE u.status!='removed' AND u.url LIKE 'https://starcitizen.tools/%'
                    AND (u.pageid IS NOT NULL OR r.last_attempt IS NOT NULL)=?
                    AND coalesce(r.next_attempt,0)<=?
                    ORDER BY coalesce(r.next_attempt,0),coalesce(r.last_attempt,0),u.url LIMIT ?""", (existing, now, batch_size)))
            state = db.execute("SELECT prefer_existing FROM refresh_queue WHERE id=1").fetchone()
            prefer = state[0] if state else 1
            targets = []
            for _ in range(batch_size):
                group = prefer if queues[prefer] else 1-prefer
                if not queues[group]:
                    break
                targets.append(queues[group].pop(0))
                prefer = 1-group
            if targets:
                with db:
                    db.execute("INSERT OR REPLACE INTO refresh_queue VALUES(1,?)", (prefer,))
            blocked = dict(getattr(source, "blocked_until", {}))
            for (url,) in targets:
                _cancelled(stop_event)
                now = clock()
                host = corpus.urlsplit(url).netloc
                if blocked.get(host, 0) > now:
                    report.deferred += 1
                    continue
                try:
                    count = corpus.crawl(db, source, 1, [url],
                        observed_at=datetime.fromtimestamp(now, UTC).isoformat(timespec="microseconds"))
                    with db:
                        db.execute("""INSERT INTO refresh_pages VALUES(?,?,?,?,NULL)
                            ON CONFLICT(url) DO UPDATE SET last_attempt=excluded.last_attempt,
                            last_success=excluded.last_success,next_attempt=excluded.next_attempt,last_error=NULL""",
                            (url, now, now, now + page_interval))
                    report.indexed += count
                except corpus.RefreshCancelled:
                    raise
                except corpus.PublisherRateLimited as exc:
                    blocked[exc.host] = exc.retry_until
                    with db:
                        db.execute("""INSERT INTO refresh_pages(url,last_attempt,next_attempt,last_error) VALUES(?,?,?,?)
                            ON CONFLICT(url) DO UPDATE SET last_attempt=excluded.last_attempt,
                            next_attempt=excluded.next_attempt,last_error=excluded.last_error""",
                            (url, now, exc.retry_until, "PublisherRateLimited"))
                    report.failed += 1
                except Exception as exc:
                    with db:
                        db.execute("""INSERT INTO refresh_pages(url,last_attempt,next_attempt,last_error) VALUES(?,?,?,?)
                            ON CONFLICT(url) DO UPDATE SET last_attempt=excluded.last_attempt,
                            next_attempt=excluded.next_attempt,last_error=excluded.last_error""",
                            (url, now, now + min(page_interval, 900), type(exc).__name__))
                    report.failed += 1
        except corpus.RefreshCancelled:
            report.cancelled = True
        for name in CATALOGS:
            rows = [(url, status, pageid) for url, status, pageid in db.execute(
                "SELECT url,status,pageid FROM wiki_urls WHERE status!='removed'") if _owner(url) == name]
            state = db.execute("SELECT last_success,last_error FROM refresh_catalogs WHERE name=?", (name,)).fetchone()
            report.coverage[name] = {"catalog_urls": len(rows), "indexed_pages": sum(x[2] is not None for x in rows),
                "last_catalog_success": state[0] if state else None, "last_catalog_error": state[1] if state else None}
    return report


class _Cancellation:
    def __init__(self, external, lost):
        self.external, self.lost = external, lost

    def is_set(self):
        return self.lost.is_set() or (self.external is not None and self.external.is_set())

    def wait(self, timeout):
        end = time.monotonic() + timeout
        while not self.is_set():
            remaining = end - time.monotonic()
            if remaining <= 0:
                return False
            self.lost.wait(min(.1, remaining))
        return True


def snapshot_limits():
    """Optional per-install limits for a trusted large export, in bytes or pages."""
    limits = {}
    for option in ("max_compressed_bytes", "max_expanded_bytes", "max_pages"):
        value = os.environ.get("WIKILLM_SNAPSHOT_" + option.upper())
        if value is None:
            continue
        if not value.isdecimal():
            raise ValueError(f"Use a nonnegative integer for snapshot {option}.")
        limits[option] = int(value) or None
    return limits


def refresh_once(db_path: str | Path, *, batch_size: int = 5,
                 catalog_interval: float = 86_400, page_interval: float = 604_800,
                 publisher=None, stop_event: threading.Event | None = None,
                 clock=time.time, enabled_catalogs=None, roots=(), import_snapshots=False) -> RefreshReport:
    """One leased refresh for a per-user DB; stale lease recovers after 120 s.

    A small heartbeat renews during bounded network reads. Losing the lease
    cancels further work. Profiles sharing the DB also share persisted pacing.
    """
    if stop_event is not None and stop_event.is_set():
        return RefreshReport(cancelled=True)
    enabled = _enabled_catalogs(enabled_catalogs)
    path = Path(db_path).expanduser()
    if not enabled and not roots and import_snapshots and not (path.parent / "snapshots").exists():
        return RefreshReport()
    path.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    with closing(sqlite3.connect(path, timeout=2)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE IF NOT EXISTS refresh_lease(id INTEGER PRIMARY KEY CHECK(id=1),token TEXT,expires REAL)")
        db.execute("BEGIN IMMEDIATE")
        current = db.execute("SELECT token,expires FROM refresh_lease WHERE id=1").fetchone()
        if current and current[1] > clock():
            db.rollback()
            return RefreshReport(leased_elsewhere=True)
        db.execute("INSERT OR REPLACE INTO refresh_lease VALUES(1,?,?)", (token, clock()+120))
        db.commit()
    finished, lost = threading.Event(), threading.Event()

    def heartbeat():
        while not finished.wait(20):
            try:
                with closing(sqlite3.connect(path, timeout=2)) as db, db:
                    if db.execute("UPDATE refresh_lease SET expires=? WHERE id=1 AND token=?",
                                  (clock()+120, token)).rowcount != 1:
                        lost.set()
                        return
            except sqlite3.Error:
                lost.set()
                return
    pulse = threading.Thread(target=heartbeat, name="wikillm-refresh-lease", daemon=True)
    pulse.start()
    cancellation = _Cancellation(stop_event, lost)
    try:
        if isinstance(publisher, corpus.Publisher):
            publisher.stop_event = cancellation
        report = RefreshReport()
        if enabled:
            report = _refresh_once(path, batch_size=batch_size, catalog_interval=catalog_interval,
                page_interval=page_interval, publisher=publisher, stop_event=cancellation, clock=clock,
                enabled_catalogs=enabled)
        if roots and not cancellation.is_set():
            from .web_sources import refresh_web_sources
            try:
                report.sources["wikis"] = refresh_web_sources(path, roots, batch_size=batch_size, stop_event=cancellation)
            except Exception as exc:
                report.sources["wikis"] = {"status": "failed", "error_type": type(exc).__name__}
                report.failed += 1
        if import_snapshots and not cancellation.is_set():
            from .snapshots import sync_snapshots
            try:
                report.sources["imports"] = sync_snapshots(path, **snapshot_limits())
                report.failed += sum(row.get("status") == "error" for row in report.sources["imports"]["sources"])
            except (OSError, ValueError, sqlite3.Error) as exc:
                report.sources["imports"] = {"status": "failed", "error_type": type(exc).__name__}
                report.failed += 1
        report.cancelled = report.cancelled or cancellation.is_set()
        return report
    finally:
        finished.set()
        pulse.join(3)
        with closing(sqlite3.connect(path, timeout=2)) as db, db:
            db.execute("DELETE FROM refresh_lease WHERE id=1 AND token=?", (token,))


class RefreshScheduler:
    """One optional daemon worker; offline queries never await it.

    stop() interrupts pacing immediately. An active HTTP request may take its
    existing 15 second timeout; False means the worker has not stopped yet.
    """
    def __init__(self, db_path, *, interval=3600, catch_up=False, **refresh_options):
        if type(interval) not in (int, float) or not 60 <= interval <= 31_536_000:
            raise ValueError("scheduler interval must be 60 seconds..1 year")
        if "stop_event" in refresh_options:
            raise ValueError("scheduler owns its cancellation event")
        self.db_path, self.interval, self.options = db_path, interval, refresh_options
        self.catch_up = catch_up
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self.last_report = None
        self.last_error = None

    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self
            self._stop.clear()
            self._thread = threading.Thread(target=self._work, name="wikillm-refresh", daemon=True)
            self._thread.start()
        return self

    def _work(self):
        next_import = 0.0
        while not self._stop.is_set():
            delay = self.interval
            try:
                options = self.options.copy()
                if options.get("import_snapshots"):
                    options["import_snapshots"] = time.monotonic() >= next_import
                self.last_report = refresh_once(self.db_path, stop_event=self._stop, **options)
                if "imports" in self.last_report.sources:
                    next_import = time.monotonic() + self.interval
                if self.catch_up and self.last_report.building_library():
                    delay = min(delay, 120)
                self.last_error = None
            except Exception as exc:
                self.last_error = type(exc).__name__
            if self._stop.wait(delay):
                break

    def stop(self, timeout=0):
        with self._lock:
            self._stop.set()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        return thread is None or not thread.is_alive()


def main(argv=None, *, publisher=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=corpus.corpus_path())
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--catalog-interval", type=float, default=86400)
    parser.add_argument("--page-interval", type=float, default=604800)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=float, default=3600)
    args = parser.parse_args(argv)
    options = dict(batch_size=args.batch_size, catalog_interval=args.catalog_interval,
                   page_interval=args.page_interval, publisher=publisher)
    if not args.watch:
        report = refresh_once(args.db, **options)
        print(json.dumps(asdict(report)))
        return int(report.failed > 0 or any(x["state"] == "failed" for x in report.catalogs.values()))
    worker = RefreshScheduler(args.db, interval=args.interval, **options).start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        return 0 if worker.stop(timeout=16) else 1


if __name__ == "__main__":
    raise SystemExit(main())
