"""One publisher's unlinked, namespaced game articles through the normal refresh path."""
import json
import sqlite3

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
