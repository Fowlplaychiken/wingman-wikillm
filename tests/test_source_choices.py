import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch


import pytest

from wiki_llm.dependencies.wingman_wiki.json_feeds import parse_feeds
from wiki_llm.dependencies.wingman_wiki.source_choices import effective_sources, manage_source

WIKI = "https://game.example.org/wiki/Main_Page"
API = "https://api.example.org/collections/lore"
MAPPING = json.dumps({"url": API, "items": "records", "text": "lore", "title": "name"})


def test_player_can_paste_addresses_without_typing_https(tmp_path):
    wiki = "wiki.example.org/space/"
    api = "data.example.org/lore"
    db = tmp_path / "library.sqlite"
    assert manage_source(db, "add", wiki)["sources"][0]["url"] == "https://" + wiki
    assert manage_source(db, "add", api, kind="api")["api_count"] == 1
    assert manage_source(db, "pause", wiki)["wiki_count"] == 0


def test_read_only_start_and_paused_setting_lines(tmp_path):
    path = tmp_path / "new" / "library.sqlite"
    typed = parse_feeds(MAPPING)
    assert effective_sources(path, (WIKI,), typed) == ((WIKI,), typed)
    sources = manage_source(path, "list", roots="# My wikis\n# " + WIKI, feeds="# " + MAPPING)
    assert [(s["url"], s["enabled"]) for s in sources["sources"]] == [(WIKI, False), (API, False)]
    assert sources["wiki_count"] == sources["api_count"] == 0
    assert effective_sources(path, "# " + WIKI, "# " + MAPPING) == ((), ())
    assert not path.parent.exists()
    path.parent.mkdir()
    path.touch()
    assert effective_sources(path, WIKI, MAPPING) == ((WIKI,), typed)
    with sqlite3.connect(path) as conn:
        assert not conn.execute("SELECT name FROM sqlite_master").fetchall()


def test_voice_only_mapped_feed_survives_reload_and_is_separate_per_library(tmp_path):
    path = tmp_path / "a.sqlite"
    with patch("socket.getaddrinfo", side_effect=AssertionError("unexpected network")):
        added = manage_source(path, "add", MAPPING, kind="api")
        assert added["status"] == "active" and added["api_count"] == 1
        assert added["sources"] == [{"url": API, "kind": "api", "enabled": True, "origin": "voice"}]
        assert manage_source(path, "pause", API, kind="api")["api_count"] == 0
        assert manage_source(path, "add", API, kind="api")["status"] == "paused"
        assert effective_sources(path) == ((), ())
        assert manage_source(path, "resume", API, kind="api")["status"] == "active"
        assert effective_sources(path)[1] == parse_feeds(MAPPING)
        assert manage_source(tmp_path / "b.sqlite", "list")["sources"] == []
        assert manage_source(path, "pause", WIKI)["status"] == "not_found"


def test_voice_can_pause_and_resume_settings_urls_without_reviving_removed_entries(tmp_path):
    path = tmp_path / "a.sqlite"
    assert manage_source(path, "pause", WIKI + "#fragment", roots=WIKI)["wiki_count"] == 0
    assert effective_sources(path, (WIKI,), ()) == ((), ())
    assert not manage_source(path, "list")["sources"]
    paused = "# " + MAPPING
    result = manage_source(path, "resume", API, kind="api", feeds=paused)
    assert result["api_count"] == 1 and result["sources"][0]["origin"] == "settings"
    assert effective_sources(path, (), paused) == ((), parse_feeds(MAPPING))
    assert effective_sources(path) == ((), ())


def test_settings_keep_new_mapping_and_manual_pause_after_voice_add(tmp_path):
    path = tmp_path / "a.sqlite"
    manage_source(path, "add", API, kind="api")
    active = effective_sources(path, (), (parse_feeds(MAPPING)[0],))
    assert active == ((), parse_feeds(MAPPING))
    assert effective_sources(path, (), "# " + MAPPING) == ((), ())
    manage_source(path, "resume", API, kind="api", feeds=MAPPING)
    assert effective_sources(path, (), "# " + MAPPING) == ((), ())
    assert effective_sources(path, (), "# " + MAPPING + "\n" + MAPPING) == active
    assert manage_source(path, "add", "# " + WIKI)["wiki_count"] == 0
    assert manage_source(path, "resume", WIKI)["wiki_count"] == 1


@pytest.mark.parametrize(("action", "url", "kind"), [
    ("configure", WIKI, "wiki"), ("add", WIKI, "other"), ("add", "", "wiki"),
    ("add", WIKI + "\n" + WIKI, "wiki"), ("add", "https://localhost/wiki", "wiki"),
    ("add", "https://127.0.0.1/items", "api"), ("add", '{"url":"http://api.example.org/x"}', "api"),
])
def test_invalid_commands_do_not_create_or_change_library(tmp_path, action, url, kind):
    path = tmp_path / "test.sqlite"
    with pytest.raises(ValueError):
        manage_source(path, action, url, kind=kind)
    assert not path.exists()


def test_concurrent_retry_retains_distinct_wikis_and_one_row_per_address(tmp_path):
    path = tmp_path / "a.sqlite"
    urls = [WIKI, WIKI, WIKI.replace("/Main_Page", "/Second")]
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda u: manage_source(path, "add", u), urls))
    assert all(result["status"] == "active" for result in results)
    assert set(effective_sources(path)[0]) == set(urls)
    assert len(manage_source(path, "list")["sources"]) == 2
