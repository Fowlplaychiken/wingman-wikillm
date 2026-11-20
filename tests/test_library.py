import socket
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wiki_llm.dependencies.wingman_wiki.library import lookup, note_history, record_note

GUIDES = Path(__file__).resolve().parents[1] / "wiki_llm" / "guides"


class OfflineLibraryTests(unittest.TestCase):
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
