"""One publisher's unlinked, namespaced game articles through the normal refresh path."""
import json
import sqlite3
from urllib.parse import quote

import pytest

from test_web_sources import MediaWikiAPI, html
from wiki_llm.dependencies.wingman_wiki import web_sources as web


ROOT = "https://wiki.example/wiki/Main_Page"
NAMESPACES = {
    "0": {"id": 0, "*": "", "content": ""},
    "100": {"id": 100, "*": "Item", "content": ""},
    "101": {"id": 101, "*": "Item talk"},
    "104": {"id": 104, "*": "Quest", "content": True},
}


def batch(namespace, title=None, *, following=None, namespaces=None):
    result = {"query": {"allpages": [] if not title else [
        {"ns": namespace, "title": title, "pageid": 123}]}}
    if namespaces is not None:
        result["query"]["namespaces"] = namespaces
    if following:
        result["continue"] = {"continue": "-||", "apcontinue": following}
    return result


@pytest.fixture(autouse=True)
def no_fixture_delay(monkeypatch):
    monkeypatch.setattr(web.corpus, "robots_delay", lambda *_: 0)


def test_orphan_game_articles_in_declared_namespaces_resume_per_namespace(tmp_path):
    db = tmp_path / "wiki.sqlite3"
    source = MediaWikiAPI(ROOT, [
        batch(0, following="Next", namespaces=NAMESPACES), batch(0),
        batch(100, "Item:Planetary Map", following="Next"),
        batch(100, "Item:Ion Core"), batch(104, "Quest:Deep Cavern"),
    ])
    names = ("Item:Planetary_Map", "Item:Ion_Core", "Quest:Deep_Cavern")
    for name in names:
        source.pages[ROOT.replace("Main_Page", name)] = html(name)
    source.run(db, (ROOT,))
    states = [source.run(db, (ROOT,))["roots"][ROOT]["api"] for _ in range(5)]
    assert [call["apnamespace"] for call in source.api_calls] == [["0"], ["0"], ["100"], ["100"], ["104"]]
    assert ["apcontinue" in call for call in source.api_calls] == [False, True, False, True, False]
    assert source.api_calls[0]["siprop"] == ["namespaces"]
    assert [state["complete"] for state in states] == [False, False, False, False, True]
    for name in names:
        assert web.read_web_page(db, ROOT.replace("Main_Page", name), (ROOT,))["status"] == "reference"


@pytest.mark.parametrize("rules,reason", [
    ("", "namespace_metadata_unavailable"),
    ("Disallow: /*meta=siteinfo", "namespace_metadata_robots_disallowed"),
])
def test_main_only_fallback_reports_its_limited_coverage(tmp_path, rules, reason):
    db = tmp_path / "wiki.sqlite3"
    source = MediaWikiAPI(ROOT, [batch(0, "Main Orphan")])
    source.robots += rules + "\n"
    child = ROOT.replace("Main_Page", "Main_Orphan")
    source.pages[child] = html("Main Orphan")
    source.run(db, (ROOT,))
    result = source.run(db, (ROOT,))["roots"][ROOT]["api"]
    assert result["complete"] is False and result["reason"] == reason
    assert ("meta" in source.api_calls[0]) == (not rules)
    assert web.read_web_page(db, child, (ROOT,))["status"] == "reference"
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT last_error FROM web_apis").fetchone() == (reason,)


def test_namespace_specific_robots_rule_skips_only_that_namespace(tmp_path):
    db = tmp_path / "wiki.sqlite3"
    source = MediaWikiAPI(ROOT, [batch(0, namespaces=NAMESPACES), batch(104, "Quest:Deep Cavern")])
    source.robots += "Disallow: /api.php?*apnamespace=100\n"
    source.pages[ROOT.replace("Main_Page", "Quest:Deep_Cavern")] = html("Deep Cavern")
    source.run(db, (ROOT,))
    source.run(db, (ROOT,))
    denied = source.run(db, (ROOT,))["roots"][ROOT]["api"]
    assert denied["state"] == "blocked" and denied["namespace"] == 100
    last = source.run(db, (ROOT,))["roots"][ROOT]["api"]
    assert last["complete"] is False and last["reason"] == "robots_disallowed"
    assert [c["apnamespace"] for c in source.api_calls] == [["0"], ["104"]]


def test_legacy_cursor_and_publisher_rate_limit_resume_same_custom_namespace(tmp_path):
    db = tmp_path / "wiki.sqlite3"
    response = web.Response(200, {"Retry-After": "120"}, b'{"error":{"code":"ratelimited"}}')
    source = MediaWikiAPI(ROOT, [batch(0, namespaces=NAMESPACES), response, batch(100)])
    source.run(db, (ROOT,))
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE web_apis SET cursor=?", (json.dumps({"apcontinue": "Old main page"}),))
    source.run(db, (ROOT,))
    assert source.api_calls[0]["apcontinue"] == ["Old main page"]
    assert source.api_calls[0]["meta"] == ["siteinfo"]
    with sqlite3.connect(db) as connection:
        cursor = connection.execute("SELECT cursor FROM web_apis").fetchone()
    assert source.run(db, (ROOT,))["deferred"] >= 1
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT cursor FROM web_apis").fetchone() == cursor
    source.calls.clear()
    source.now += 1
    source.run(db, (ROOT,))
    assert source.calls == []
    source.now += 120
    result = source.run(db, (ROOT,))["roots"][ROOT]["api"]
    assert result["state"] == "advanced" and result["namespace"] == 100
    assert [c["apnamespace"] for c in source.api_calls] == [["0"], ["100"], ["100"]]


@pytest.mark.parametrize("entry", ["Hauptseite", ""])
def test_custom_article_path_reads_unlinked_localized_subpages_and_resumes(tmp_path, entry):
    base = "https://wiki.example/artikel/"
    root, db = base + entry, tmp_path / "wiki.sqlite3"
    names = ["Flughandbuch", "Gegenstand:Gerät/Teil A", "Gegenstand:Gerät/Teil B/Kern"]
    namespaces = {"0": NAMESPACES["0"], "100": {"id": 100, "*": "Gegenstand", "content": ""}}
    source = MediaWikiAPI(root, [batch(0, names[0], namespaces=namespaces),
                                 batch(100, names[1], following="Teil B"), batch(100, names[2])])
    source.general.update(articlepath="/artikel/$1", server="//wiki.example")
    articles = [base + quote(name.replace(" ", "_"), safe=":/") for name in names]
    for name, url in zip(names, articles):
        source.pages[url] = html(name)
    source.run(db, (root,))
    states = [source.run(db, (root,))["roots"][root]["api"] for _ in range(3)]
    assert [state["complete"] for state in states] == [False, False, True]
    assert len(source.route_calls) == 1
    assert [c["apnamespace"] for c in source.api_calls] == [["0"], ["100"], ["100"]]
    assert source.api_calls[-1]["apcontinue"] == ["Teil B"]
    for url in articles:
        assert web.read_web_page(db, url, (root,))["status"] == "reference"
    source.calls.clear()
    source.run(db, (root,))
    assert source.calls == []


def test_custom_article_route_keeps_the_configured_section_boundary(tmp_path):
    root = "https://wiki.example/artikel/Manual/"
    source = MediaWikiAPI(root, [batch(0, "Manual/Engine", namespaces={"0": NAMESPACES["0"]},
                                        following="Other"), batch(0, "Other/Unrelated")])
    source.general["articlepath"] = "/artikel/$1"
    source.pages[root + "Engine"] = html("Engine")
    db = tmp_path / "wiki.sqlite3"
    source.run(db, (root,))
    source.run(db, (root,))
    result = source.run(db, (root,))["roots"][root]["api"]
    assert not result["complete"] and result["reason"] == "listed_titles_outside_crawl_scope"
    assert web.read_web_page(db, root + "Engine", (root,))["status"] == "reference"
    assert not any("/Other/" in url for url, _ in source.calls)


@pytest.mark.parametrize("bad", [
    "/other-game/$1", "//other.example/artikel/$1", "/artikel/../../$1",
    "/artikel/$1?action=edit", None,
])
def test_custom_scope_needs_publisher_route_evidence_before_listing(tmp_path, bad):
    root, db = "https://wiki.example/artikel/Hauptseite", tmp_path / "wiki.sqlite3"
    source = MediaWikiAPI(root, [])
    source.general["articlepath"] = bad
    source.run(db, (root,))
    api = source.run(db, (root,))["roots"][root]["api"]
    assert api["complete"] is False and api["reason"] == "publisher_article_path_unverified"
    assert len(source.route_calls) == 1 and source.api_calls == []
    assert web.read_web_page(db, root, (root,))["status"] == "reference"


def test_robots_can_decline_route_metadata_and_recover_later(tmp_path):
    root, db = "https://wiki.example/artikel/", tmp_path / "wiki.sqlite3"
    source = MediaWikiAPI(root, [batch(0, namespaces={"0": NAMESPACES["0"]})])
    source.general["articlepath"] = "/artikel/$1"
    source.robots += "Disallow: /api.php?*siprop=general\n"
    source.run(db, (root,))
    api = source.run(db, (root,))["roots"][root]["api"]
    assert api["reason"] == "article_path_metadata_robots_disallowed" and not api["complete"]
    assert source.route_calls == source.api_calls == []
    source.now += 86401
    source.robots = "User-agent: *\nAllow: /\n"
    assert source.run(db, (root,))["roots"][root]["api"]["complete"] is True


def test_upgrade_rediscovers_unchanged_landing_page_behind_redirect(tmp_path):
    root = "https://wiki.example/artikel/"
    landing, db = root + "Hauptseite", tmp_path / "wiki.sqlite3"
    source = MediaWikiAPI(root, [batch(0, "Unlinked", namespaces={"0": NAMESPACES["0"]})])
    source.general["articlepath"] = "/artikel/$1"
    source.pages[landing] = web.Response(200, {"ETag": '"unchanged"'}, source.pages[root])
    source.pages[root] = web.Response(301, {"Location": landing}, b"")
    source.pages[root + "Unlinked"] = html("Unlinked")
    source.run(db, (root,))
    with sqlite3.connect(db) as connection:
        # An older installation cached the landing page and ignored its API.
        connection.execute("DELETE FROM web_apis")
        connection.execute("ALTER TABLE web_apis DROP COLUMN article_prefix")
    source.calls.clear()
    source.run(db, (root,))
    assert [h.get("If-None-Match") for u,h in source.calls if u == landing] == [None]
    assert source.run(db, (root,))["roots"][root]["api"]["complete"]
    assert web.read_web_page(db, root + "Unlinked", (root,))["status"] == "reference"


def test_rejected_edit_uri_is_reported_without_contact_or_storage_of_raw_url(tmp_path):
    root, db = "https://wiki.example/artikel/Hauptseite", tmp_path / "wiki.sqlite3"
    source = MediaWikiAPI(root, [])
    source.pages[root] = html("Homepage", extra='<link rel="EditURI" href="https://other.example/api.php?action=rsd">')
    source.run(db, (root,))
    api = source.run(db, (root,))["roots"][root]["api"]
    assert api == {"state": "blocked", "complete": False, "reason": "api_declaration_unsupported"}
    assert web.web_coverage(db, (root,))[root]["api"]["url"] == ""
    assert not any("other.example" in u for u, _ in source.calls)
