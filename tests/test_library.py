from contextlib import closing
from datetime import datetime, timezone
import json
import socket
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wiki_llm.dependencies.wingman_wiki.library import check_inventory, library_status, lookup, note_history, read_page, record_note

GUIDES = Path(__file__).resolve().parents[1] / "wiki_llm" / "guides"


class OfflineLibraryTests(unittest.TestCase):
    def test_review_queue_finds_drafts_and_stale_citations_and_advances_after_review(self):
        from wiki_llm.dependencies.wingman_wiki.library import review_queue
        with patch.object(socket, "socket", side_effect=AssertionError("network access")):
            self.assertEqual(review_queue(self.db)["notes"], [])
            url = "https://starcitizen.tools/Aberdeen"
            for slug, state in (("a", "draft"), ("b", "reviewed"), ("c", "withdrawn"), ("d", "reviewed")):
                record_note(slug, "Visibility", "Observed haze", status=state, db_path=self.db,
                            source=url + "#Atmosphere" if slug == "b" else None)
            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute("CREATE TABLE source_changes(url TEXT, observed_at TEXT, reason TEXT)")
                db.execute("INSERT INTO source_changes VALUES(?, ?, ?)",
                           (url, datetime.now(timezone.utc).isoformat(timespec="microseconds"), "changed"))
            first = review_queue(self.db, limit=1)
            self.assertEqual([(r["slug"], r["reason"]) for r in first["notes"]], [("a", "draft")])
            second = review_queue(self.db, after=first["next_after"], limit=1)
            self.assertEqual([(r["slug"], r["revision"], r["reason"]) for r in second["notes"]],
                             [("b", 1, "source_changed")])
            self.assertIsNone(second["next_after"])
            record_note("b", "Visibility", "Rechecked haze", status="reviewed", previous_revision=1,
                        source=url, db_path=self.db)
            self.assertEqual([r["slug"] for r in review_queue(self.db)["notes"]], ["a"])

    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.db = Path(root.name) / "wiki.sqlite3"

    def test_cold_install_returns_both_actual_publishers_with_network_disabled(self):
        with patch.object(socket, "socket", side_effect=AssertionError("network access")):
            found = lookup(GUIDES, "Aberdeen visibility", db_path=self.db)
            links = found["references"][0]["source_links"]
            self.assertIn("https://starcitizen.tools/Aberdeen", links)
            self.assertTrue(found["references"][0]["citation"].startswith(
                "https://robertsspaceindustries.com/galactapedia/article/"))
            self.assertFalse(self.db.exists())

    def test_build_filters_and_no_evidence(self):
        matched = lookup(GUIDES, "life support load", "4.10.3", self.db)["references"]
        self.assertTrue(matched)
        self.assertTrue(all(r["game_build"] == "4.10.3" for r in matched))
        self.assertTrue(all("https://github.com/" not in r["citation"] for r in matched))
        self.assertFalse(lookup(GUIDES, "life support load", "3.1.0", self.db)["references"])
        self.assertFalse(lookup(GUIDES, "nonexistentzz", db_path=self.db)["references"])

    def test_notes_survive_reopen_drafts_corrections_and_retraction(self):
        options = {"db_path": self.db, "game_build": "4.10.3"}
        def write(text, state, expected):
            return record_note("quasar", "Quasar", text, status=state,
                               previous_revision=expected, **options)
        write("Initial field observation", "draft", 0)
        self.assertFalse(lookup(GUIDES, "Quasar", db_path=self.db)["references"])
        write("Observed value 42", "reviewed", 1)
        write("Unverified value 99", "draft", 2)
        actual = lookup(GUIDES, "Quasar", "4.10.3", self.db)["references"][0]
        self.assertEqual((actual["excerpt"], actual["citation"]), ("Observed value 42", None))
        self.assertEqual(actual["source_kind"], "local_observation")
        with self.assertRaisesRegex(ValueError, "revision 3"):
            write("Stale edit", "reviewed", 2)
        write("Superseded after independent check", "withdrawn", 3)
        self.assertFalse(lookup(GUIDES, "Quasar", db_path=self.db)["references"])
        self.assertEqual(len(note_history("quasar", self.db)), 4)

    def test_existing_markdown_note_uses_its_public_source(self):
        notes = self.db.parent / "notes"
        notes.mkdir()
        (notes / "sample.md").write_text(
            "# Quasar\n\nReviewed 2026-11-20.\n\n## Finding\n\nQuasar visibility"
            "\n\n[Source](https://starcitizen.tools/Aberdeen)\n", encoding="utf-8")
        note = lookup(GUIDES, "Quasar", db_path=self.db)["references"][0]
        self.assertEqual(note["citation"], "https://starcitizen.tools/Aberdeen")

    def test_upstream_change_flags_local_claim_and_preserves_history(self):
        from contextlib import closing
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("CREATE TABLE wiki_urls(url TEXT,lastmod TEXT,status TEXT)")
            conn.execute("INSERT INTO wiki_urls VALUES(?,?,?)", (
                "https://starcitizen.tools/Aberdeen", "2026-11-21", "indexed"))
        found = lookup(GUIDES, "Aberdeen visibility", db_path=self.db)["references"][0]
        self.assertTrue(found["needs_review"])
        self.assertEqual(found["changed_sources"][0]["lastmod"], "2026-11-21")

    def test_other_game_search_read_and_status_offline_with_preset_disabled(self):
        from wiki_llm.dependencies.wingman_wiki.snapshots import import_snapshot
        base = "https://wiki.example.org/other/"
        url = base + "Engine"
        export = self.db.parent / "game.jsonl"
        pages = [{"url": url, "title": "Steam engines", "text": "Coal powers the locomotive.", "game_build": "1.0"},
                 {"url": "https://wiki.example.org/hidden/Engine", "title": "Locomotive", "text": "Hidden locomotive"}]
        export.write_text("\n".join(json.dumps(row) for row in pages), encoding="utf-8")
        import_snapshot(self.db, export)
        options = {"include_star_citizen": False, "roots": (base,)}
        with patch.object(socket, "socket", side_effect=AssertionError("network access")):
            self.assertFalse(lookup(GUIDES, "Aberdeen", db_path=self.db, **options)["references"])
            hit = lookup(GUIDES, "What powers a locomotive?", "1.0", self.db, **options)["references"]
            self.assertEqual([r["citation"] for r in hit], [url])
            self.assertIn("Coal", read_page(self.db, url, **options)["text"])
            self.assertEqual(read_page(self.db, pages[1]["url"], **options)["status"], "unavailable")
            self.assertEqual(library_status(self.db, **options)["imported_articles"][0]["saved_articles"], 1)
            self.assertFalse(lookup(GUIDES, "locomotive", db_path=self.db, roots=())["references"])
            self.assertTrue(lookup(GUIDES, "Aberdeen", db_path=self.db, roots=(base,))["references"])
            record_note("my-railway", "My railway", "I named my locomotive Grebe.", status="reviewed", db_path=self.db)
            self.assertTrue(lookup(GUIDES, "Grebe", db_path=self.db, include_star_citizen=False)["references"])

    def test_new_evidence_marks_note_until_reviewed_again_on_same_day(self):
        url = "https://wiki.example.org/rail/Engine"
        result = record_note("engine", "Steam engine", "Coal", status="reviewed", source=url, db_path=self.db)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("CREATE TABLE source_changes(url PRIMARY KEY,observed_at TEXT,reason TEXT)")
            stamp = note_history("engine", self.db)[0]["recorded_at"]
            from datetime import datetime, timedelta
            when = (datetime.fromisoformat(stamp) + timedelta(microseconds=1)).isoformat()
            conn.execute("INSERT INTO source_changes VALUES(?,?,?)", (url, when, "web_page_changed"))
        self.assertTrue(lookup(GUIDES, "Steam", db_path=self.db)["references"][0]["needs_review"])
        record_note("engine", "Steam engine", "Updated coal evidence", status="reviewed", source=url,
                    previous_revision=result["revision"], db_path=self.db)
        self.assertFalse(lookup(GUIDES, "Steam", db_path=self.db)["references"][0].get("needs_review", False))

    def test_prove_article_text_against_independent_inventory_and_revision(self):
        from wiki_llm.dependencies.wingman_wiki.snapshots import import_snapshot
        base = "https://atlas.example.org/game/"
        manifest, export = (self.db.parent / name for name in ("inventory.jsonl", "articles.jsonl"))
        def write(path, rows):
            path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
        expected = [{"url": base+"Orphan", "revision": 4}, {"url": base+"Main", "revision": "b"}]
        write(manifest, expected)
        articles = [dict(item, title="Map", text="Complete location record.") for item in expected]
        write(export, [dict(articles[0], revision=3)])
        import_snapshot(self.db, export)
        opts = {"include_star_citizen": False, "roots": (base,)}
        partial = check_inventory(self.db, manifest, **opts)
        self.assertEqual((partial["missing_text"], partial["revision_unverified"]), (1, 1))
        write(export, articles)
        import_snapshot(self.db, export)
        with patch.object(socket, "socket", side_effect=AssertionError("network access")):
            self.assertEqual(check_inventory(self.db, manifest, **opts)["status"], "matches_inventory")
            self.assertEqual(check_inventory(self.db, manifest, include_star_citizen=False)["disabled"], 2)
