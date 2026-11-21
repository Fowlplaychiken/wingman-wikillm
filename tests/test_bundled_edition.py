import gzip
import hashlib
import json
from unittest.mock import patch

import pytest

import build
from wiki_llm.dependencies.wingman_wiki import snapshots
from wiki_llm.dependencies.wingman_wiki.library import read_page
from wiki_llm.dependencies.wingman_wiki.refresh import refresh_once


def test_release_edition_is_offline_and_scoped_to_star_citizen(tmp_path):
    url = "https://starcitizen.tools/Aberdeen"
    body = "{{Infobox|planet=Hurston}}\nA moon of Hurston."
    row = {"url": url, "title": "Aberdeen", "revision": 95, "text": body}
    edition = tmp_path / snapshots.EDITION_FILENAME
    edition.write_bytes(gzip.compress((json.dumps(row) + "\n").encode(), mtime=0))
    sha = hashlib.sha256(edition.read_bytes()).hexdigest()
    db = tmp_path / "library" / "wiki.sqlite3"
    with patch.object(snapshots, "EDITION_SHA256", sha), patch.object(snapshots, "bundled_edition", return_value=edition):
        refresh_once(db, enabled_catalogs=(), import_snapshots=True, include_sc_edition=False)
        assert not db.exists()
        report = refresh_once(db, enabled_catalogs=(), import_snapshots=True, include_sc_edition=True)
        assert report.sources["imports"]["sources"][0]["page_count"] == 1
        assert read_page(db, url)["text"] == body
        assert read_page(db, url)["edition_date"] == "2023-09-30"
        assert read_page(db, url, include_star_citizen=False)["status"] == "unavailable"
        second = refresh_once(db, enabled_catalogs=(), import_snapshots=True, include_sc_edition=True)
        assert second.sources["imports"]["sources"][0]["status"] == "unchanged"
        edition.write_bytes(gzip.compress(b"unverified edition", mtime=0))
        with pytest.raises(ValueError, match="release checksum"):
            build.build(tmp_path / "release", edition=edition)
        with pytest.raises(snapshots.SnapshotError, match="published edition"):
            snapshots.import_snapshot(db, edition)
        assert read_page(db, url)["text"] == body
