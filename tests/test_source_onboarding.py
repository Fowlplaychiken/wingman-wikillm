import json
import sys
from urllib.parse import urlencode

import pytest

from wiki_llm import __main__ as cli
from wiki_llm.dependencies.wingman_wiki import json_feeds, web_sources
from wiki_llm.dependencies.wingman_wiki.snapshots import read_snapshot_page
from wiki_llm.dependencies.wingman_wiki.source_choices import effective_sources, manage_source
from wiki_llm.dependencies.wingman_wiki.source_onboarding import setup_source


HOST = "https://wiki.example.org"
ROOT = HOST + "/wiki/"
API = HOST + "/api/lore"
ARTICLE = ROOT + "Glacier"
HTML = (b'<!doctype html><html><head><title>Glacial Realms Wiki</title></head><body>'
        b'<main><p>Glacial Realms is home to rare elemental creatures and glacial ruins.</p>'
        b'<a href="Glacier">Glacier</a><a href="https://outside.example.org/Index">Other source</a>'
        b'</main></body></html>')


def _html(body=HTML, **headers):
    return web_sources.Response(200, {"Content-Type": "text/html", **headers}, body)


def _json(payload):
    return web_sources.Response(200, {"Content-Type": "application/vnd.api+json"}, json.dumps(payload).encode())


class _Publisher:
    def __init__(self, monkeypatch):
        self.calls = []
        self.pages = {HOST + "/robots.txt": web_sources.Response(404, {}, b""), ROOT: _html()}
        monkeypatch.setattr(web_sources, "_transport", self.request)
        monkeypatch.setattr(web_sources.corpus, "robots_delay", lambda *_: 0)

    def request(self, url, headers, *, validate_url=None):
        if validate_url:
            validate_url(url)
        self.calls.append(url)
        return self.pages[url]


@pytest.fixture
def publisher(monkeypatch):
    return _Publisher(monkeypatch)


def test_wiki_preview_then_add_saves_only_the_verified_root(tmp_path, publisher):
    db = tmp_path / "wiki.db"
    publisher.pages[HOST + "/wiki"] = web_sources.Response(301, {"Location": "/wiki/"}, b"")
    preview = setup_source(db, "preview", "wiki.example.org/wiki#Categories")
    assert preview["status"] == "ready"
    assert preview["source"] == {"kind": "wiki", "url": ROOT, "name": "Glacial Realms Wiki"}
    assert preview["preview"]["article_text_found"]
    assert preview["preview"]["in_scope_links_on_page"] == 1
    assert effective_sources(db) == ((), ())
    assert publisher.calls == [HOST + "/robots.txt", HOST + "/wiki", ROOT]
    assert setup_source(db, "add", ROOT)["status"] == "active"
    assert effective_sources(db) == ((ROOT,), ())
    fetches = publisher.calls[:]
    assert setup_source(db, "pause", ROOT)["status"] == "paused"
    assert setup_source(db, "add", ROOT)["status"] == "paused"
    assert setup_source(db, "resume", ROOT)["status"] == "active"
    assert setup_source(db, "remove", ROOT)["status"] == "removed"
    assert publisher.calls == fetches


def test_json_alias_detection_persists_mapping_and_background_refresh_reads_it(tmp_path, publisher):
    db = tmp_path / "wiki.db"
    publisher.pages[API] = _json({"payload": {"lore_entries": [
        {"headline": "Glacier", "markdown": "An ice creature with armored claws.", "permalink": ARTICLE},
    ]}, "next": "?page=2", "count": 2})
    ridge = ROOT + "Ice_Ridge"
    publisher.pages[API + "?page=2"] = _json({"payload": {"lore_entries": [
        {"headline": "Ice Ridge", "markdown": "A ridge retreat for ice creatures.", "permalink": ridge},
    ]}, "count": 2})
    added = setup_source(db, "add", API)
    assert added["status"] == "active"
    assert added["source"] == {"kind": "api", "url": API, "name": "Lore"}
    assert added["preview"] == {"scope": "first_page", "valid_entries_on_first_page": 1,
                                "publisher_reported_total": 2, "more_pages": True}
    assert publisher.calls == [HOST + "/robots.txt", API]
    roots, feeds = effective_sources(db)
    assert not roots and feeds[0].items == "payload.lore_entries"
    assert json_feeds.refresh_feeds(db, feeds)[API]["status"] == "ready"
    saved = json_feeds.feed_db_path(db, feeds[0])
    result = read_snapshot_page(saved, ARTICLE)
    assert result["title"] == "Glacier"
    assert result["url"] == ARTICLE


def test_conflicting_collections_require_one_choice_and_advanced_mapping_still_works(tmp_path, publisher):
    db = tmp_path / "wiki.db"
    catalog = [{"codename": "Glacier", "details": {"history": "Glaciers predate the realms."}, "id": "Glacier"}]
    publisher.pages[API] = _json({"results": catalog, "entries": catalog})
    unclear = setup_source(db, "add", API)
    assert unclear["status"] == "needs_input" and unclear["message"].count("?") == 1
    assert "results" in unclear["message"] and "entries" in unclear["message"]
    assert effective_sources(db) == ((), ())
    mapping = json.dumps({"url": API, "items": "entries", "title": "codename", "text": "details.history",
                          "url_template": ROOT + "{id}", "id_field": "id"})
    assert setup_source(db, "add", mapping)["status"] == "active"
    assert effective_sources(db)[1] == json_feeds.parse_feeds(mapping)


def test_existing_default_mapping_survives_remove_and_guided_readd(tmp_path, publisher):
    db = tmp_path / "wiki.db"
    publisher.pages[API] = _json([{"name": "Glacier", "description": "An ice creature.", "url": ARTICLE}])
    manage_source(db, "add", API, kind="api")
    prior_feed = effective_sources(db)[1][0]
    assert setup_source(db, "remove", API)["status"] == "removed"
    assert setup_source(db, "add", API)["status"] == "active"
    assert effective_sources(db)[1] == (prior_feed,)


def test_missing_article_link_asks_about_link_without_sharing_response_text(tmp_path, publisher):
    db = tmp_path / "wiki.db"
    publisher.pages[API] = _json({"results": [
        {"name": "Glacier", "descriptions": [{"desc": "A body that stays out of the tool result."}], "key": "glacier"},
    ]})
    result = setup_source(db, "add", API)
    assert result["status"] == "needs_input"
    assert "link" in result["message"] and result["message"].count("?") == 1
    assert "A body" not in str(result)
    assert effective_sources(db) == ((), ())


def test_mediawiki_api_identifies_and_checks_declared_article_page(tmp_path, publisher):
    db = tmp_path / "wiki.db"
    endpoint = HOST + "/engine/api.php"
    publisher.pages[endpoint + "?" + urlencode(web_sources._SITE_ROUTES)] = _json({"query": {"general": {
        "base": ROOT, "server": HOST, "scriptpath": "/engine", "articlepath": "/wiki/$1",
    }}})
    assert setup_source(db, "add", endpoint + "?action=query&list=allpages")["source"]["url"] == ROOT
    assert effective_sources(db) == ((ROOT,), ())
    assert len(publisher.calls) == 3 and publisher.calls[-1] == ROOT


@pytest.mark.parametrize(("source", "status"), [
    (ROOT, "blocked"), (API, "blocked"),
    (HOST + "/to-other-publisher", "needs_input"),
    (HOST + "/missing", "unavailable"),
])
def test_failed_previews_do_not_enable_sources(tmp_path, publisher, source, status):
    db = tmp_path / "wiki.db"
    publisher.pages[ROOT] = _html(**{"X-Robots-Tag": "noindex"})
    publisher.pages[HOST + "/robots.txt"] = web_sources.Response(200, {}, b"User-agent: *\nDisallow: /api/")
    publisher.pages[HOST + "/to-other-publisher"] = web_sources.Response(
        302, {"Location": "https://outside.example.org/wiki/"}, b"")
    publisher.pages[HOST + "/missing"] = web_sources.Response(404, {}, b"")
    assert setup_source(db, "add", source)["status"] == status
    assert effective_sources(db) == ((), ())
    assert API not in publisher.calls
    assert not any("outside.example.org" in call for call in publisher.calls)


@pytest.mark.parametrize("source", [
    "https://localhost/wiki", "https://127.0.0.1/wiki",
    API + "?%61ccess_token=dummy-probe-secret",
    API + "?X-Amz-Signature=dummy-probe-secret",
    "https://person:dummy-probe-secret@wiki.example.org/",
])
def test_no_network_or_files_for_invalid_or_credentialed_addresses(tmp_path, publisher, source):
    db = tmp_path / "wiki.db"
    with pytest.raises(ValueError) as error:
        setup_source(db, "add", source)
    assert "dummy-probe-secret" not in str(error.value)
    assert not publisher.calls and not db.exists()


def test_cli_uses_one_database_across_source_actions(tmp_path, publisher, monkeypatch, capsys):
    db = tmp_path / "wiki.db"
    def call(*args):
        monkeypatch.setattr(sys, "argv", ["wiki_llm", "--db", str(db), "sources", *args])
        cli.main()
        return json.loads(capsys.readouterr().out)
    assert call("preview", ROOT)["status"] == "ready"
    assert call("list")["sources"] == []
    assert call("add", ROOT)["status"] == "active"
    assert call("list")["sources"][0]["url"] == ROOT
    assert call("pause", ROOT)["status"] == "paused"
    assert call("remove", ROOT)["status"] == "removed"


def test_same_wiki_and_feed_address_requires_disambiguation(tmp_path, publisher):
    db = tmp_path / "wiki.db"
    manage_source(db, "add", ROOT, kind="wiki")
    manage_source(db, "add", ROOT, kind="api")
    assert setup_source(db, "pause", ROOT)["status"] == "needs_input"
    assert setup_source(db, "pause", ROOT, kind="api")["status"] == "paused"
    assert effective_sources(db) == ((ROOT,), ())
    assert not publisher.calls
