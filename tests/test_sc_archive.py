from collections import Counter
from html import escape
import gzip
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import prepare_sc_edition
from wiki_llm.dependencies.wingman_wiki import sc_archive


def _revision(revision_id, timestamp, text, model="wikitext", author="ArchiveEditor"):
    contributor = f"<contributor><username>{author}</username></contributor>" if author else ""
    return f"""
    <revision>
      <id>{revision_id}</id>
      <timestamp>{timestamp}</timestamp>
      {contributor}
      <model>{model}</model>
      <text xml:space="preserve">{escape(text, quote=False)}</text>
    </revision>
    """


def _page(page_id, title, text, *, namespace=0, redirect=None, revisions=None):
    redirect_xml = f'<redirect title="{redirect}" />' if redirect is not None else ""
    revs = revisions if revisions is not None else _revision(
        "10", "2023-09-01T00:00:00Z", text
    )
    return f"""
    <page>
      <title>{title}</title>
      <ns>{namespace}</ns>
      <id>{page_id}</id>
      {redirect_xml}
      {revs}
    </page>
    """


def _xml(pages, *, base="https://starcitizen.tools/"):
    return (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<mediawiki xmlns='http://www.mediawiki.org/xml/export-0.10/'>"
        f"<siteinfo><sitename>Star Citizen Wiki</sitename><base>{base}</base></siteinfo>"
        + "".join(pages)
        + "</mediawiki>"
    ).encode()


class StarCitizenArchiveTests(unittest.TestCase):
    def test_latest_main_namespace_article_preserves_full_wikitext_with_provenance(self):
        old = _page(
            "7", "A Test Article", "old text that must not survive the later revision",
            revisions=_revision("10", "2023-08-01T00:00:00Z", "old text"),
        )
        current = _page(
            "7", "A Test Article", "",
            revisions=_revision(
                "24", "2023-09-21T12:30:00Z",
                "{{Infobox spacecraft\n| name = Test Ship\n| role = fighter\n}}\n"
                "A [[Target page|clear view]] above [[File:Test.png|thumb]].\n"
                "'''Stable text.''' == History ==\n"
                "{| class=\"wikitable\"\n|+ Table caption\n| Item || Detail\n|}\n"
                "<ref name=\"source\">Citation text</ref> & literal",
                author="KnownWikiEditor",
            ),
        )
        stats = Counter()
        rows = list(sc_archive.iter_articles(io.BytesIO(_xml([old, current])), stats))

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["url"], "https://starcitizen.tools/A_Test_Article")
        self.assertEqual(row["revision"], 24)
        self.assertEqual(row["revision_timestamp"], "2023-09-21T12:30:00Z")
        self.assertEqual(row["source_page_id"], "7")
        self.assertNotIn("revision_contributor", row)
        self.assertEqual(row["namespace"], 0)
        self.assertEqual(
            row["text"],
            "{{Infobox spacecraft\n| name = Test Ship\n| role = fighter\n}}\n"
            "A [[Target page|clear view]] above [[File:Test.png|thumb]].\n"
            "'''Stable text.''' == History ==\n"
            "{| class=\"wikitable\"\n|+ Table caption\n| Item || Detail\n|}\n"
            "<ref name=\"source\">Citation text</ref> & literal",
        )
        self.assertIn("[[Target page|clear view]]", row["text"])
        self.assertNotIn("old text", row["text"])
        self.assertIn("[[File:Test.png|thumb]]", row["text"])
        self.assertIn("{{Infobox spacecraft", row["text"])
        self.assertIn("<ref name=\"source\">Citation text</ref>", row["text"])
        self.assertEqual(row["archive_identifier"], sc_archive.ARCHIVE_IDENTIFIER)
        self.assertEqual(row["license"], sc_archive.LICENSE_URL)
        self.assertEqual(
            row["history_url"],
            "https://starcitizen.tools/index.php?title=A_Test_Article&action=history",
        )
        self.assertTrue(row["historical_snapshot"])
        self.assertEqual(stats["xml_page_blocks"], 2)
        self.assertEqual(stats["accepted_articles"], 1)

    def test_script_pages_are_skipped_without_executing_markup(self):
        page = _page("9", "Script article", "Intro <script>alert(1)</script> outro")
        stats = Counter()
        rows = list(sc_archive.iter_articles(io.BytesIO(_xml([page])), stats))
        self.assertEqual(rows, [])
        self.assertEqual(stats["skipped_non_text"], 1)

    def test_filters_non_main_redirect_reprints_and_unclear_rights(self):
        pages = [
            _page("1", "Main namespace", "This is useful plain article text."),
            _page("2", "Template:Do not import", "Template text.", namespace=10),
            _page("3", "Redirect article", "#REDIRECT [[Main namespace]]"),
            _page("4", "Comm-Link:Release", "Copied release text."),
            _page("5", "License exception", "{{Copyright notice}} Borrowed text."),
            _page("6", "Non-wikitext", "not article prose", revisions=_revision(
                "11", "2023-09-05T00:00:00Z", "not article prose", model="javascript"
            )),
        ]
        stats = Counter()
        rows = list(sc_archive.iter_articles(io.BytesIO(_xml(pages)), stats))

        self.assertEqual([row["title"] for row in rows], ["Main namespace"])
        self.assertEqual(stats["other_namespace_page_blocks"], 1)
        self.assertEqual(stats["skipped_redirect"], 1)
        self.assertEqual(stats["skipped_third_party_verbatim"], 1)
        self.assertEqual(stats["skipped_explicit_or_unknown_rights"], 1)
        self.assertEqual(stats["skipped_non_text"], 1)

    def test_ambiguous_quote_and_source_markup_is_excluded(self):
        pages = [
            _page("1", "Quoted material", "{{Quote|text=Publisher supplied text.}}"),
            _page("2", "Description source", "{{Ship|description source=Press release}}"),
            _page("3", "Quoted block", "<blockquote>unlicensed quote</blockquote>"),
            _page("4", "Quoted reference", "{{Cite web|quote=Publisher words}}"),
            _page("5", "Eligible", "Original wiki prose."),
            _page("6", "Cited", "{{Cite web|url=https://example.test}} Wiki prose."),
            _page("7", "Reference", "Lore<ref>{{Cite RSI|url=https://example.test}}</ref> text."),
            _page("8", "Source template", "{{Source|website=Galactapedia}} Wiki prose."),
        ]
        stats = Counter()
        rows = list(sc_archive.iter_articles(io.BytesIO(_xml(pages)), stats))
        self.assertEqual(
            [row["title"] for row in rows],
            ["Cited", "Eligible", "Reference", "Source template"],
        )
        self.assertEqual(stats["skipped_ambiguous_third_party"], 4)
        self.assertEqual(stats["skipped_third_party_quoted_template"], 1)
        self.assertEqual(stats["skipped_third_party_quoted_markup"], 2)
        self.assertEqual(stats["skipped_third_party_source_field"], 1)

    def test_explicit_cc_by_sa_template_is_not_treated_as_exception(self):
        page = _page(
            "9", "Licensed article",
            "{{CC BY-SA 4.0}} The article text remains available for private search.",
        )
        stats = Counter()
        rows = list(sc_archive.iter_articles(io.BytesIO(_xml([page])), stats))
        self.assertEqual(len(rows), 1)
        self.assertEqual(stats["skipped_explicit_or_unknown_rights"], 0)

    def test_foreign_siteinfo_fails_closed(self):
        page = _page("1", "Foreign", "Text from a different site.")
        with self.assertRaises(sc_archive.ArchiveError):
            list(sc_archive.iter_articles(io.BytesIO(
                _xml([page], base="https://example.org/")
            )))

    def test_doctype_and_entity_declarations_are_rejected(self):
        unsafe = (
            b'<!DOCTYPE mediawiki [<!ENTITY x "content">]>'
            b'<mediawiki><siteinfo><sitename>Star Citizen Wiki</sitename>'
            b'<base>https://starcitizen.tools/</base></siteinfo></mediawiki>'
        )
        with self.assertRaises(sc_archive.ArchiveError):
            list(sc_archive.iter_articles(io.BytesIO(unsafe)))

    def test_zstd_reader_leaves_the_compressed_input_open(self):
        try:
            from compression import zstd
        except ImportError:
            self.skipTest("stdlib zstd unavailable in this Python")
        source = io.BytesIO(zstd.compress(b"zstandard round-trip"))
        with sc_archive.open_zstd(source) as reader:
            self.assertEqual(reader.read(), b"zstandard round-trip")
        self.assertFalse(source.closed)

    def test_python_zstandard_backend_uses_byte_window_limit(self):
        class FakeDecompressor:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def stream_reader(self, source, **kwargs):
                self.source = source
                self.stream_kwargs = kwargs
                return self

        source = io.BytesIO(b"frame")
        fake_module = Mock(ZstdDecompressor=FakeDecompressor)
        with patch.dict("sys.modules", {"compression": None, "zstandard": fake_module}), patch.object(
            sc_archive.importlib.util, "find_spec", return_value=object()
        ):
            reader = sc_archive.open_zstd(source)
        self.assertEqual(reader.kwargs["max_window_size"], 2**30)
        self.assertEqual(reader.stream_kwargs, {"read_size": 128 * 1024, "closefd": False})

    def test_export_is_deterministic_and_keeps_wikitext_provenance(self):
        article = {
            "url": "https://starcitizen.tools/Test_Page",
            "title": "Test Page",
            "text": "{{Infobox}}\n[[Target|link]]",
            "revision": 24,
            "revision_timestamp": "2023-09-21T12:30:00Z",
            "source_page_id": "7",
            "history_url": "https://starcitizen.tools/index.php?title=Test_Page&action=history",
        }

        def one_article(_stream, counts):
            counts["accepted_articles"] = 1
            return iter([article])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.zst"
            source.write_bytes(b"verified fixture")
            outputs = [root / "first.jsonl.gz", root / "second.jsonl.gz"]
            with patch.object(prepare_sc_edition, "_verify_source", return_value={}), \
                 patch.object(prepare_sc_edition.sc_archive, "open_zstd",
                              side_effect=lambda stream: stream), \
                 patch.object(prepare_sc_edition.sc_archive, "iter_articles",
                              side_effect=one_article):
                receipts = [prepare_sc_edition.prepare(source, output) for output in outputs]
            first, second = (output.read_bytes() for output in outputs)
            self.assertEqual(first, second)
            self.assertEqual(receipts[0]["output_sha256"], receipts[1]["output_sha256"])
            self.assertEqual(int.from_bytes(first[4:8], "little"), 0)
            self.assertEqual(first[3] & 0x08, 0)  # No embedded output filename.
            row = json.loads(gzip.decompress(first).decode("utf-8"))
            self.assertEqual(row["text"], article["text"])
            self.assertEqual(row["attribution_url"], article["history_url"])
            self.assertEqual(row["revision_timestamp"], article["revision_timestamp"])
            self.assertNotIn("revision_contributor", row)


if __name__ == "__main__":
    unittest.main()
