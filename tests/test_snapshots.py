import bz2
import gzip
import json
import sqlite3
from pathlib import Path

import tempfile
import unittest
from contextlib import closing

from wiki_llm.dependencies.wingman_wiki.snapshots import (
    SnapshotError,
    import_snapshot,
    read_snapshot_page,
    search_snapshots,
    sync_snapshots,
)


def _xml(*pages, base="https://game-wiki.example.org/wiki/Main_Page"):
    body = f'''<?xml version="1.0" encoding="UTF-8"?>
<mediawiki xmlns="http://www.mediawiki.org/xml/export-0.11/">
  <siteinfo><sitename>Game Wiki</sitename><base>{base}</base><namespaces>
    <namespace key="0"></namespace><namespace key="6">File</namespace>
    <namespace key="14">Category</namespace>
  </namespaces></siteinfo>
  {''.join(pages)}
</mediawiki>'''
    return body


def _page(title, page_id, revisions, ns=0, redirect=None):
    redir = f'<redirect title="{redirect}" />' if redirect else ""
    return (f"<page><title>{title}</title><ns>{ns}</ns><id>{page_id}</id>{redir}"
            f"{revisions}</page>")


def _revision(revision, timestamp, text):
    return (f"<revision><id>{revision}</id><timestamp>{timestamp}</timestamp>"
            f"<text>{text}</text></revision>")



class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._temporary.name)

    def tearDown(self):
        self._temporary.cleanup()

    def test_mediawiki_import_indexes_latest_revision_namespace_redirect_and_links(self):
        tmp_path = self.tmp_path
        source = tmp_path / "game.xml"
        source.write_text(_xml(
            _page("Engine", 10,
                  _revision(4, "2026-01-01T00:00:00Z", "obsolete turbine" ) +
                  _revision(9, "2025-12-01T00:00:00Z", "Latest turbine [[Power Grid|power link]]")),
            _page("Category:Power", 12,
                  _revision(3, "2025-05-01T00:00:00Z", "Power category"), ns=14),
            _page("Old Engine", 13, _revision(2, "2025-03-01T00:00:00Z", "redirect"),
                  redirect="Engine"),
        ), encoding="utf-8")
        db = tmp_path / "user" / "wiki.sqlite3"

        result = import_snapshot(db, source)

        assert result["status"] == "imported"
        assert result["page_count"] == 3
        matches = search_snapshots("Latest turbine", db)
        assert len(matches) == 1
        assert matches[0]["url"] == "https://game-wiki.example.org/wiki/Engine"
        assert matches[0]["source_origin"] == "https://game-wiki.example.org"
        assert matches[0]["revision"] == "9"
        assert matches[0]["revision_timestamp"] == "2025-12-01T00:00:00Z"
        page = read_snapshot_page(db, matches[0]["url"])
        assert page["text"] == "Latest turbine [[Power Grid|power link]]"
        assert page["links"] == ["https://game-wiki.example.org/wiki/Power_Grid"]
        namespaced = read_snapshot_page(db, "https://game-wiki.example.org/wiki/Category:Power")
        assert namespaced["namespace"] == 14
        assert namespaced["namespace_name"] == "Category"
        redirect = read_snapshot_page(db, "https://game-wiki.example.org/wiki/Old_Engine")
        assert redirect["redirect_target"] == "Engine"


    def test_mediawiki_latest_timestamp_is_used_when_revision_id_missing(self):
        tmp_path = self.tmp_path
        source = tmp_path / "timestamps.xml"
        source.write_text(_xml(_page("Clock", 1,
            "<revision><timestamp>2025-01-01T00:00:00Z</timestamp><text>Earlier</text></revision>"
            "<revision><timestamp>2026-01-01T00:00:00Z</timestamp><text>Latest timestamp</text></revision>")),
            encoding="utf-8")
        db = tmp_path / "db.sqlite3"

        imported = import_snapshot(db, source)

        assert imported["page_count"] == 1
        found = search_snapshots("Latest timestamp", db)
        assert found[0]["revision"] is None
        assert found[0]["revision_timestamp"] == "2026-01-01T00:00:00Z"


    def test_jsonl_search_filters_before_limit_and_reads_bounded_chunks(self):
        tmp_path = self.tmp_path
        source = tmp_path / "articles.jsonl"
        body = ("Nebula survey bright nebula. " * 400) + "Last passage."
        articles = [
            {"url": "https://lore.example.org/game/nebula", "title": "Nebula Lore", "text": body,
             "revision": 77, "game_build": "2.0"},
            {"url": "https://other.example.org/game/nebula", "title": "Nebula Other", "text": body,
             "revision": "r5", "game_build": "1.0"},
        ]
        source.write_text("\n".join(json.dumps(a) for a in articles) + "\n", encoding="utf-8")
        db = tmp_path / "user" / "wiki.sqlite3"
        import_snapshot(db, source)

        assert len(search_snapshots("nebula", db, limit=1, allow_source=lambda origin: origin == "https://other.example.org")) == 1
        assert search_snapshots("nebula", db, game_build="2.0")[0]["url"] == articles[0]["url"]
        assert search_snapshots("nebula", db, game_build="2.00") == []
        assert search_snapshots("nebula", db, allow_url=lambda url: url.startswith("https://other.example.org"))[0]["url"] == articles[1]["url"]
        assert search_snapshots("nebula", db, source_origin="https://lore.example.org")[0]["game_build_state"] == "provided"
        assert not search_snapshots("nebula", db, allow_source=lambda origin: False)

        first = read_snapshot_page(db, articles[0]["url"])
        assert first["status"] == "snapshot"
        assert len(first["text"]) == 6000
        assert first["next_offset"] == 6000
        second = read_snapshot_page(db, articles[0]["url"], first["next_offset"])
        assert second["offset"] == 6000
        assert second["text"].startswith(body[6000:])
        assert read_snapshot_page(db, articles[0]["url"], len(body))["next_offset"] is None


    def test_import_rejects_nonpublic_or_nonhttps_json_urls(self):
        tmp_path = self.tmp_path
        db = tmp_path / "db.sqlite3"
        for url in ("http://lore.example.org/article", "https://localhost/article",
                    "https://127.0.0.1/article", "https://user:secret@lore.example.org/article",
                    "file:///tmp/article"):
            source = tmp_path / (str(abs(hash(url))) + ".jsonl")
            source.write_text(json.dumps({"url": url, "title": "Article", "text": "words"}) + "\n",
                              encoding="utf-8")
            with self.assertRaises(SnapshotError):
                import_snapshot(db, source)
        assert search_snapshots("words", db) == []


    def test_failed_or_truncated_reimport_preserves_last_good_and_change_log(self):
        tmp_path = self.tmp_path
        source = tmp_path / "wiki.xml"
        db = tmp_path / "db.sqlite3"
        original = _xml(_page("Stable", 1, _revision(1, "2025-01-01T00:00:00Z", "stable evidence")))
        source.write_text(original, encoding="utf-8")
        import_snapshot(db, source)
        source.write_text(original.replace("</mediawiki>", ""), encoding="utf-8")

        with self.assertRaises(SnapshotError):
            import_snapshot(db, source)

        assert search_snapshots("stable evidence", db)[0]["revision"] == "1"
        with closing(sqlite3.connect(db)) as connection:
            assert connection.execute("SELECT count(*) FROM source_changes").fetchone()[0] == 0


    def test_successful_reimport_removes_pages_and_records_precise_changes(self):
        tmp_path = self.tmp_path
        source = tmp_path / "wiki.xml"
        db = tmp_path / "db.sqlite3"
        source.write_text(_xml(
            _page("Change", 1, _revision(1, "2025-01-01T00:00:00Z", "obsoleteold")),
            _page("Remove", 2, _revision(2, "2025-01-02T00:00:00Z", "withdrawnonly"))), encoding="utf-8")
        import_snapshot(db, source)
        source.write_text(_xml(
            _page("Change", 1, _revision(3, "2026-01-01T00:00:00Z", "currentnew")),
            _page("Added", 3, _revision(4, "2026-01-02T00:00:00Z", "addedonly"))), encoding="utf-8")

        import_snapshot(db, source)

        assert search_snapshots("obsoleteold", db) == []
        assert search_snapshots("withdrawnonly", db) == []
        assert search_snapshots("currentnew", db)[0]["revision"] == "3"
        with closing(sqlite3.connect(db)) as connection:
            changes = dict(connection.execute("SELECT url,reason FROM source_changes"))
            observed = dict(connection.execute("SELECT url,observed_at FROM source_changes"))
        assert changes == {
            "https://game-wiki.example.org/wiki/Change": "snapshot_revision_changed",
            "https://game-wiki.example.org/wiki/Remove": "snapshot_page_removed",
            "https://game-wiki.example.org/wiki/Added": "snapshot_page_added",
        }
        assert all(value.endswith("+00:00") for value in observed.values())


    def test_distinct_source_files_do_not_clobber_each_other_and_unchanged_is_fast(self):
        tmp_path = self.tmp_path
        db = tmp_path / "db.sqlite3"
        first = tmp_path / "one.jsonl"
        second = tmp_path / "two.jsonl"
        article = {"url": "https://shared.example.org/page", "title": "Shared", "text": "shared evidence"}
        first.write_text(json.dumps(article) + "\n", encoding="utf-8")
        second.write_text(json.dumps({**article, "text": "shared second evidence"}) + "\n", encoding="utf-8")
        one = import_snapshot(db, first)
        two = import_snapshot(db, second)
        assert one["source_id"] != two["source_id"]
        assert len(search_snapshots("shared", db, limit=2)) == 2
        assert import_snapshot(db, first)["status"] == "unchanged"

    def test_snapshot_tables_do_not_touch_existing_wiki_pages_or_user_notes(self):
        tmp_path = self.tmp_path
        db = tmp_path / "db.sqlite3"
        source = tmp_path / "pages.jsonl"
        with closing(sqlite3.connect(db)) as connection:
            connection.executescript("""
                CREATE TABLE pages(pageid INTEGER PRIMARY KEY,title TEXT,revision INTEGER);
                CREATE TABLE note_revisions(slug TEXT,revision INTEGER,title TEXT,PRIMARY KEY(slug,revision));
                INSERT INTO pages VALUES(7,'Agent page',11);
                INSERT INTO note_revisions VALUES('private-note',3,'Player note');
            """)
            connection.commit()
        source.write_text(json.dumps({
            "url": "https://lore.example.org/page", "title": "Lore", "text": "lore evidence"}) + "\n",
            encoding="utf-8")

        import_snapshot(db, source)

        with closing(sqlite3.connect(db)) as connection:
            self.assertEqual(connection.execute("SELECT * FROM pages").fetchall(), [(7, "Agent page", 11)])
            self.assertEqual(connection.execute("SELECT * FROM note_revisions").fetchall(),
                             [("private-note", 3, "Player note")])


    def test_sync_scans_only_supported_sibling_files(self):
        tmp_path = self.tmp_path
        db = tmp_path / "user" / "wiki.sqlite3"
        source_dir = db.parent / "snapshots"
        source_dir.mkdir(parents=True)
        (source_dir / "a.jsonl").write_text(json.dumps({
            "url": "https://data.example.org/article", "title": "Star", "text": "starlight"}) + "\n",
            encoding="utf-8")
        (source_dir / "ignore.txt").write_text("starlight", encoding="utf-8")

        result = sync_snapshots(db)

        assert result["status"] == "synced"
        assert [source["file"] for source in result["sources"]] == ["a.jsonl"]
        assert search_snapshots("starlight", db)[0]["url"] == "https://data.example.org/article"

    def test_gzip_and_bzip2_exports_stream_into_snapshots(self):
        tmp_path = self.tmp_path
        db = tmp_path / "db.sqlite3"
        xml_bytes = _xml(_page("Compressed", 1,
            _revision(5, "2026-02-01T00:00:00Z", "compressed turbine evidence"))).encode()
        xml_gzip = tmp_path / "wiki.xml.gz"
        xml_gzip.write_bytes(gzip.compress(xml_bytes))
        xml_bzip = tmp_path / "wiki.xml.bz2"
        xml_bzip.write_bytes(bz2.compress(xml_bytes))
        jsonl = tmp_path / "articles.jsonl.gz"
        jsonl.write_bytes(gzip.compress((json.dumps({
            "url": "https://lore.example.org/article", "title": "Gzip Lore",
            "text": "compressed nebula evidence"}) + "\n").encode()))

        results = [import_snapshot(db, item) for item in (xml_gzip, xml_bzip, jsonl)]

        assert [result["file_format"] for result in results] == [
            "mediawiki-xml-gzip", "mediawiki-xml-bzip2", "jsonl-gzip"]
        assert len(search_snapshots("compressed", db, limit=3)) == 3
        assert search_snapshots("nebula", db)[0]["title"] == "Gzip Lore"

    def test_aggregate_import_limits_are_overridable_and_failure_keeps_last_good(self):
        tmp_path = self.tmp_path
        source = tmp_path / "pages.jsonl.gz"
        db = tmp_path / "db.sqlite3"
        old_article = {"url": "https://lore.example.org/a", "title": "Old", "text": "oldtoken"}
        new_article = {"url": "https://lore.example.org/b", "title": "New", "text": "newtoken"}
        second_article = {"url": "https://lore.example.org/c", "title": "Newer", "text": "newertoken"}
        source.write_bytes(gzip.compress((json.dumps(old_article) + "\n").encode()))
        import_snapshot(db, source)
        source.write_bytes(gzip.compress((json.dumps(new_article) + "\n" +
                                          json.dumps(second_article) + "\n").encode()))

        with self.assertRaisesRegex(SnapshotError, "expanded-byte limit"):
            import_snapshot(db, source, max_expanded_bytes=8)
        with self.assertRaisesRegex(SnapshotError, "page-count limit"):
            import_snapshot(db, source, max_pages=1)
        # None explicitly disables aggregate input ceilings for large full wikis.
        result = import_snapshot(db, source, max_compressed_bytes=None,
                                 max_expanded_bytes=None, max_pages=None)

        assert result["status"] == "imported" and result["page_count"] == 2
        assert search_snapshots("oldtoken", db) == []
        assert search_snapshots("newtoken", db)

    def test_sync_reports_bad_export_and_continues_importing_later_files(self):
        tmp_path = self.tmp_path
        db = tmp_path / "user" / "wiki.sqlite3"
        source_dir = db.parent / "snapshots"
        source_dir.mkdir(parents=True)
        (source_dir / "a-broken.xml.gz").write_bytes(gzip.compress(b"<mediawiki>")[:-5])
        valid = {"url": "https://data.example.org/article", "title": "Good",
                 "text": "good snapshot evidence"}
        (source_dir / "b-good.jsonl.gz").write_bytes(
            gzip.compress((json.dumps(valid) + "\n").encode()))

        result = sync_snapshots(db)

        assert result["status"] == "synced"
        assert result["sources"][0]["status"] == "error"
        assert result["sources"][0]["error_type"] == "SnapshotError"
        assert result["sources"][0]["error"]
        assert result["sources"][1]["status"] == "imported"
        assert search_snapshots("good snapshot", db)[0]["title"] == "Good"

    def test_corrupt_compressed_reimport_keeps_last_good_snapshot(self):
        tmp_path = self.tmp_path
        source = tmp_path / "articles.jsonl.gz"
        db = tmp_path / "db.sqlite3"
        article = {"url": "https://lore.example.org/article", "title": "Stable",
                   "text": "lastgoodtoken"}
        valid_archive = gzip.compress((json.dumps(article) + "\n").encode())
        source.write_bytes(valid_archive)
        import_snapshot(db, source)
        source.write_bytes(valid_archive[:-5])

        with self.assertRaises(SnapshotError):
            import_snapshot(db, source)

        assert search_snapshots("lastgoodtoken", db)[0]["title"] == "Stable"

    def test_sync_accepts_user_page_limit_override(self):
        tmp_path = self.tmp_path
        db = tmp_path / "user" / "wiki.sqlite3"
        source_dir = db.parent / "snapshots"
        source_dir.mkdir(parents=True)
        articles = [
            {"url": "https://data.example.org/one", "title": "One", "text": "firsttoken"},
            {"url": "https://data.example.org/two", "title": "Two", "text": "secondtoken"},
        ]
        source = source_dir / "all.jsonl.gz"
        source.write_bytes(gzip.compress(("\n".join(json.dumps(item) for item in articles) + "\n").encode()))

        rejected = sync_snapshots(db, max_pages=1)
        accepted = sync_snapshots(db, max_pages=None)

        assert rejected["sources"][0]["status"] == "error"
        assert "page-count limit" in rejected["sources"][0]["error"]
        assert accepted["sources"][0]["status"] == "imported"
        assert accepted["sources"][0]["page_count"] == 2
