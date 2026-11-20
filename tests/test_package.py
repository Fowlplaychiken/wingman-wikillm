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
            marker = "PRIVATE-OBSERVATION-573A"
            for relative in ("guides/personal-draft.md", "notes/local.md", "wiki.sqlite3"):
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
                    "wiki_llm/" + name.replace("dependencies/wingman_wiki/", "dependencies/wingman_wiki_v0_1_1/")
                    for name in (*build.FILES, "LICENSE", "README.md")})
                self.assertFalse(any(marker.encode() in bundle.read(k) for k in bundle.namelist()))

    def test_two_users_share_public_references_but_never_each_others_notes(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, other = (Path(tmp) / user / "wiki.sqlite3" for user in ("one", "two"))
            record_note("quasar", "Quasar", "My observation", status="reviewed", db_path=first)
            guides = build.PACKAGE / "guides"
            self.assertTrue(lookup(guides, "quasar", db_path=first)["references"])
            self.assertFalse(lookup(guides, "quasar", db_path=other)["references"])
            self.assertEqual(lookup(guides, "Aberdeen", db_path=first)["references"],
                             lookup(guides, "Aberdeen", db_path=other)["references"])
