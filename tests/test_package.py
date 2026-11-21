import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import build
from wiki_llm.dependencies.wingman_wiki.library import lookup, record_note


class DistributionPrivacyTests(unittest.TestCase):
    def test_release_excludes_operator_notes_even_inside_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "wiki_llm"
            shutil.copytree(build.PACKAGE, package)
            for name in ("README.md", "LICENSE"):
                shutil.copyfile(build.ROOT / name, root / name)
            (root / "docs").mkdir()
            for name in build.BINARY_FILES:
                shutil.copyfile(build.ROOT / name, root / name)
            marker = "PRIVATE-OBSERVATION-573A"
            (root / "docs" / "private.webp").write_bytes(marker.encode())
            for relative in ("guides/personal-draft.md", "notes/local.md", "wiki.sqlite3", "docs/private.webp"):
                file = package / relative
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text(marker, encoding="utf-8")
            db = root / "personal" / "wiki.sqlite3"
            record_note("local", "Local", marker, status="reviewed", db_path=db)
            with patch.object(build, "ROOT", root), patch.object(build, "PACKAGE", package), patch.dict(
                    os.environ, {"SC_WIKI_DB": str(db)}):
                archive = build.build(root / "dist")
            with zipfile.ZipFile(archive) as bundle:
                self.assertEqual(set(bundle.namelist()), {
                    "wiki_llm/" + name.replace("dependencies/wingman_wiki/", "dependencies/wingman_wiki_v0_1_2/")
                    for name in (*build.FILES, *build.BINARY_FILES, "LICENSE", "README.md")})
                self.assertFalse(any(marker.encode() in bundle.read(k) for k in bundle.namelist()))

    def test_binary_banner_is_exact_and_readme_relative(self):
        members = build.package_members("0.1.2")
        self.assertIn(b"](docs/field-guide.webp)", members["README.md"])
        self.assertEqual(members["docs/field-guide.webp"],
                         (build.ROOT / "docs/field-guide.webp").read_bytes())
        self.assertEqual(members["docs/field-guide.webp"][:4], b"RIFF")
        self.assertEqual(members["docs/field-guide.webp"][8:12], b"WEBP")

    def test_archives_are_deterministic_and_portable(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = build.build(Path(tmp) / "first")
            second = build.build(Path(tmp) / "second")
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with zipfile.ZipFile(first) as bundle:
                for entry in bundle.infolist():
                    self.assertNotIn("\\", entry.filename)
                    self.assertEqual(entry.date_time, (1980, 1, 1, 0, 0, 0))
                    self.assertEqual(entry.create_system, 3)
                    self.assertEqual(entry.external_attr >> 16, 0o100644)

    def test_two_users_share_public_references_but_never_each_others_notes(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, other = (Path(tmp) / user / "wiki.sqlite3" for user in ("one", "two"))
            record_note("quasar", "Quasar", "My observation", status="reviewed", db_path=first)
            guides = build.PACKAGE / "guides"
            self.assertTrue(lookup(guides, "quasar", db_path=first)["references"])
            self.assertFalse(lookup(guides, "quasar", db_path=other)["references"])
            self.assertEqual(lookup(guides, "Aberdeen", db_path=first)["references"],
                             lookup(guides, "Aberdeen", db_path=other)["references"])
