"""Independent public-wiki fixtures; no live contacts or model calls."""
from pathlib import Path
import socket
import sqlite3
import threading
from unittest.mock import patch

import pytest

from wiki_llm.dependencies.wingman_wiki import web_sources as web
from wiki_llm.dependencies.wingman_wiki.library import record_note

A = "https://wiki.example/game-a/Main_Page"
B = "https://wiki.example/game-b/Main_Page"


def html(title, text=None, links=(), license="by-sa/4.0", extra=""):
    text = text or ("This wiki describes a fictional game mechanic with enough original words "
                    "for a useful local reference and a clearly attributable publisher.")
    footer = ('<footer><a rel="license" href="https://creativecommons.org/licenses/' + license + '/">License</a></footer>') if license else ''
    return ('<html><head><meta property="og:title" content="' + title + '">' + extra +
            '</head><body><article><p>' + text + '</p>' + ''.join(
            '<p><a href="' + link + '">Read next</a></p>' for link in links) +
            '</article>' + footer + '</body></html>').encode()


class Fixture:
    def __init__(self):
        self.now = 1_800_000_000.0
        self.calls = []
        self.pages = {}
        self.robots = "User-agent: *\nAllow: /\n"
        self.robots_status = 200

    def clock(self):
        return self.now

    def __call__(self, url, headers):
        self.calls.append((url, headers))
        if url.endswith('/robots.txt'):
            return web.Response(self.robots_status, {}, self.robots.encode())
        value = self.pages.get(url)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, web.Response):
            return value
        return web.Response(200, {'Content-Type': 'text/html'}, value) if value is not None else web.Response(404, {}, b'')

    def run(self, path, roots=(A,), batch_size=5, **kwargs):
        return web.refresh_web_sources(path, roots, batch_size=batch_size,
            _transport=self, _clock=self.clock, **kwargs)


@pytest.fixture(autouse=True)
def no_fixture_delay():
    with patch.object(web.corpus, 'robots_delay', return_value=0):
        yield


def test_root_and_article_scope_matches_home_page_and_excludes_native_sources():
    assert web.parse_roots('  ' + A + '#intro\n' + A + '\n' + B) == (A, B)
    assert web.in_scope('https://wiki.example/game-a/Deep_Article', A)
    assert not web.in_scope('https://wiki.example/game-b/Deep_Article', A)
    for value in ('http://wiki.example/wiki/', 'https://localhost/wiki/', 'https://127.0.0.1/wiki/',
                  'https://10.0.0.1/wiki/', 'https://wiki.local/wiki/', 'https://user:secret@wiki.example/',
                  'https://wiki.example:8443/', 'https://wiki.example/?token=private',
                  'https://starcitizen.tools/Aberdeen', 'https://sitemap.starcitizen.tools/',
                  'https://support.robertsspaceindustries.com/', 'https://www.robertsspaceindustries.com/',
                  'https://wiki.example/wiki/%2e%2e/secret', 'https://wiki.example/wiki/x%2Fy'):
        with pytest.raises(ValueError):
            web.parse_roots(value)
    for value in ('/game-a/Special:Login', '/game-a/File:Picture.png', '/game-a/api.php',
                  '/game-a/Page?action=edit', '/game-a/image.svg', 'https://other.example/game-a/Article'):
        assert not web.in_scope(value if value.startswith('http') else 'https://wiki.example'+value, A)


def test_nested_frontier_full_text_natural_search_and_two_game_isolation(tmp_path):
    source, db = Fixture(), tmp_path / 'wiki.sqlite3'
    source.pages[A] = html('Game A', links=['/game-a/Depth1', B, 'https://other.example/Page', '/game-a/File:Photo.png'])
    source.pages[B] = html('Game B', 'A totally different game has a remarkable cobalt resource and separate mechanics for its players to research and remember.')
    for depth in range(1, 7):
        url = 'https://wiki.example/game-a/Depth' + str(depth)
        source.pages[url] = html('Depth ' + str(depth),
            'Deep chronicles describe the amber relic and its recharge mechanism in a fictional game. ' + 'Details are available here. '*15,
            links=['/game-a/Depth' + str(depth+1)] if depth < 6 else [])
    for _ in range(4):
        source.run(db, (A, B), batch_size=2)
    found = web.search_web_sources('How does the amber relic recharge?', db, (A,), limit=3)
    assert found and all(row['storage_mode'] == 'full_text' and row['license'] == 'CC-BY-SA-4.0' for row in found)
    assert all(row['citation'].startswith('https://wiki.example/game-a/') for row in found)
    assert web.search_web_sources('cobalt', db, (B,))
    assert not web.search_web_sources('cobalt', db, (A,))
    deepest = web.read_web_page(db, 'https://wiki.example/game-a/Depth6', (A,))
    assert deepest['status'] == 'reference' and deepest['passages']
    assert web.read_web_page(db, B, (A,))['status'] == 'unavailable'
    assert not any('other.example' in call[0] or 'File:' in call[0] for call in source.calls)
    assert web.web_coverage(db, (A,))[A]['pages']['indexed'] == 7


def test_no_license_only_one_short_excerpt_and_denied_signals_store_nothing(tmp_path):
    source, db = Fixture(), tmp_path / 'wiki.sqlite3'
    source.pages[A] = html('Unlicensed', ' '.join('word'+str(i) for i in range(80)), license=None)
    assert source.run(db)['indexed'] == 1
    with sqlite3.connect(db) as conn:
        passages = conn.execute('SELECT text FROM web_passages').fetchall()
        assert len(passages) == 1 and len(passages[0][0].split()) == 25
        assert 'word79' not in passages[0][0]
    source.now += 604_801
    source.pages[A] = html('Denied', extra='<meta name="robots" content="noindex">')
    source.run(db)
    assert not web.search_web_sources('word', db, (A,))
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT count(*) FROM web_passages').fetchone() == (0,)
        assert conn.execute('SELECT status FROM web_pages WHERE url=?', (A,)).fetchone() == ('blocked',)


def test_added_renamed_removed_recovered_pages_and_stable_change_observation(tmp_path):
    source, db = Fixture(), tmp_path / 'wiki.sqlite3'
    old = 'https://wiki.example/game-a/Old'
    new = 'https://wiki.example/game-a/New'
    gone = 'https://wiki.example/game-a/Gone'
    source.pages[A] = html('Home', links=[old, gone])
    source.pages[old] = html('Original name')
    source.pages[gone] = html('Removable page')
    source.run(db)
    record_note('personal', 'Personal observation', 'This is an untouched private note', status='reviewed', db_path=db)
    with sqlite3.connect(db) as conn:
        notes = conn.execute('SELECT * FROM note_revisions').fetchall()
    source.now += 604_801
    source.pages[A] = html('Changed home title', links=[new])
    source.pages[old] = web.Response(301, {'Location': new}, b'')
    source.pages[new] = html('New article name')
    source.pages[gone] = web.Response(410, {}, b'')
    source.run(db)
    assert web.search_web_sources('new article name', db, (A,))[0]['citation'] == new
    assert web.read_web_page(db, old, (A,))['status'] == 'unavailable'
    with sqlite3.connect(db) as conn:
        changes = dict(conn.execute('SELECT url,reason FROM source_changes'))
        assert changes == {A: 'web_page_changed', old: 'web_url_redirected', gone: 'web_page_removed'}
        assert conn.execute('SELECT storage_mode FROM web_pages WHERE url=?', (gone,)).fetchone() == (None,)
        assert conn.execute('SELECT count(*) FROM web_passages WHERE url=?', (gone,)).fetchone() == (0,)
        first = conn.execute('SELECT * FROM source_changes ORDER BY url').fetchall()
        assert conn.execute('SELECT * FROM note_revisions').fetchall() == notes
    source.now += 604_801
    source.run(db)
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT * FROM source_changes ORDER BY url').fetchall() == first
    source.now += 604_801
    source.pages[gone] = html('Recovered article')
    source.run(db)
    assert web.search_web_sources('Recovered', db, (A,))


def test_failure_or_incomplete_read_retains_good_and_toggle_makes_zero_calls(tmp_path):
    source, db = Fixture(), tmp_path / 'wiki.sqlite3'
    source.pages[A] = html('Durable evidence')
    source.run(db)
    source.calls.clear()
    assert source.run(db, roots=())['coverage'] == {} and source.calls == []
    assert not web.search_web_sources('Durable', db, ())
    assert web.search_web_sources('Durable', db, (A,))
    source.now += 604_801
    source.pages[A] = html('Corrupt truncated page')[:-30]  # article/html must actually close
    # Remove every closing article boundary, rather than only a trailing footer.
    source.pages[A] = b'<html><article><p>Incomplete replacement text must never overwrite the last good reference even if it is quite long'
    assert source.run(db)['failed'] == 1
    assert web.search_web_sources('Durable', db, (A,))[0]['needs_review']
    source.now += 901
    source.pages[A] = TimeoutError('Private exception detail must not enter diagnostic metadata')
    assert source.run(db)['failed'] == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT last_error FROM web_pages').fetchone() == ('TimeoutError',)
    source.now += 901
    source.pages[A] = html('Durable evidence')
    assert source.run(db)['indexed'] == 1
    assert not web.search_web_sources('Durable', db, (A,))[0]['needs_review']


def test_closed_html_does_not_make_truncated_custom_article_complete(tmp_path):
    source, db = Fixture(), tmp_path / 'wiki.sqlite3'
    source.pages[A] = html('Durable article')
    source.run(db)
    source.now += 604_801
    source.pages[A] = (b'<html><body><article><p>Truncated replacement contains enough words and characters '
                       b'to pass the minimum text check but never closes the article.</p></body></html>')

    report = source.run(db)

    assert report['failed'] == 1
    assert web.search_web_sources('Durable article', db, (A,))[0]['needs_review']


def test_nested_sitemaps_are_hints_and_partial_failure_does_not_delete(tmp_path):
    source, db = Fixture(), tmp_path / 'wiki.sqlite3'
    sitemap = 'https://wiki.example/sitemap.xml'
    child = 'https://wiki.example/sitemap-game.xml'
    hidden = 'https://wiki.example/game-a/Unlinked'
    source.pages[sitemap] = web.Response(200, {}, ('<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>'+child+'</loc></sitemap></sitemapindex>').encode())
    source.pages[child] = web.Response(200, {}, ('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>'+hidden+'</loc></url><url><loc>'+B+'</loc></url></urlset>').encode())
    source.pages[A], source.pages[hidden] = html('Home'), html('Unlinked discovery')
    assert source.run(db)['indexed'] == 1
    assert source.run(db)['indexed'] == 1
    assert web.search_web_sources('Unlinked', db, (A,))
    assert not any(call[0] == B for call in source.calls)
    source.now += 604_801
    source.pages[child] = web.Response(200, {}, b'<urlset broken')
    report = source.run(db)
    assert report['roots'][A]['discovery_error'] == 'ParseError'
    assert report['indexed'] == 2 and web.search_web_sources('Unlinked', db, (A,))


@pytest.mark.parametrize('status', [429, 503])
def test_host_retry_after_prevents_any_more_requests_and_survives_caller(tmp_path, status):
    source, db = Fixture(), tmp_path / 'wiki.sqlite3'
    source.pages[A] = html('Home', links=['/game-a/Retry', '/game-a/Zebra'])
    source.run(db, batch_size=1)
    source.calls.clear()
    retry = 'https://wiki.example/game-a/Retry'
    source.pages[retry] = web.Response(status, {'Retry-After': '120'}, b'not retained')
    report = source.run(db, batch_size=5)
    assert report['deferred'] >= 1
    assert [call[0] for call in source.calls] == [retry]
    source.calls.clear()
    source.now += 1
    source.run(db)
    assert source.calls == []
    source.now += 121
    source.pages[retry] = html('Recovered retry')
    assert source.run(db)['indexed'] == 1


def test_content_signals_block_storage_and_new_contacts_are_public_ip_pinned(tmp_path):
    source, db = Fixture(), tmp_path / 'wiki.sqlite3'
    source.robots = 'User-agent: *\nAllow: /\nContent-signal: search=no\n'
    source.pages[A] = html('Forbidden storage')
    source.run(db)
    assert [call[0] for call in source.calls] == ['https://wiki.example/robots.txt']
    assert not web.search_web_sources('Forbidden', db, (A,))
    with patch.object(socket, 'getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]), \
         patch.object(web.http.client, 'HTTPSConnection') as connection:
        with pytest.raises(ValueError, match='non-public'):
            web._transport(A, {})
        connection.assert_not_called()


def test_unvetted_redirect_or_native_canonical_cannot_bypass_restrictions(tmp_path):
    source, db = Fixture(), tmp_path / 'wiki.sqlite3'
    source.pages[A] = web.Response(302, {'Location': 'https://127.0.0.1/private'}, b'')
    assert source.run(db)['failed'] == 1
    assert not any('127.0.0.1' in call[0] for call in source.calls)
    source.now += 901
    source.pages[A] = html('Mirror', extra='<link rel="canonical" href="https://starcitizen.tools/Aberdeen">')
    source.run(db)
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT count(*) FROM web_passages').fetchone() == (0,)
    assert not any('starcitizen.tools' in call[0] for call in source.calls)


def test_disabled_sources_and_cancelled_pass_start_no_network(tmp_path):
    source = Fixture()
    stop = threading.Event()
    stop.set()
    db = tmp_path / 'wiki.sqlite3'
    assert source.run(db, stop_event=stop)['cancelled']
    assert not source.calls
    empty = tmp_path / 'not-created.sqlite3'
    assert source.run(empty, roots=())['indexed'] == 0
    assert not empty.exists()


def test_many_roots_and_large_pending_frontiers_get_persistent_fair_turns(tmp_path):
    source, db = Fixture(), tmp_path / 'wiki.sqlite3'
    roots = tuple('https://wiki.example/game-'+str(i)+'/Main_Page' for i in range(25))
    assert len(web.parse_roots('\n'.join(roots))) == 25
    for i, root in enumerate(roots):
        source.pages[root] = html('Game '+str(i), links=['./Nested'+str(n) for n in range(30)])
    for _ in roots:
        assert source.run(db, roots, batch_size=1)['indexed'] == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(DISTINCT root) FROM web_pages WHERE status='indexed'").fetchone() == (25,)
        assert conn.execute("SELECT count(*) FROM web_pages WHERE status='pending'").fetchone() == (750,)
    many = roots + tuple('https://extra'+str(i)+'.example/wiki/' for i in range(1100))
    assert web.search_web_sources('game', db, many)
    assert web.read_web_page(db, roots[0], many)['status'] == 'reference'
    coverage = web.web_coverage(db, many)
    assert len(coverage) == 1125
    assert all(coverage[root]['storage_modes'] == {'full_text': 1} for root in roots)


def test_failed_first_root_does_not_starve_other_root_in_one_item_cycles(tmp_path):
    source, db = Fixture(), tmp_path / 'wiki.sqlite3'
    first, second = 'https://broken.example/wiki/', 'https://good.example/wiki/'
    source.pages[first] = TimeoutError()
    source.pages[second] = html('Successful second game')
    assert source.run(db, (first, second), batch_size=1)['failed'] == 1
    assert source.run(db, (first, second), batch_size=1)['indexed'] == 1
    assert web.search_web_sources('successful', db, (second,))


def test_persistent_sitemap_frontier_reaches_more_than_one_cycle_budget(tmp_path):
    import gzip
    source, db = Fixture(), tmp_path / 'wiki.sqlite3'
    source.pages[A] = html('Home')
    index = 'https://wiki.example/sitemap.xml'
    children = ['https://wiki.example/part'+str(i).zfill(2)+'.xml.gz' for i in range(12)]
    source.pages[index] = web.Response(200, {}, ('<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + ''.join(
        '<sitemap><loc>'+child+'</loc></sitemap>' for child in children) + '</sitemapindex>').encode())
    for i, child in enumerate(children):
        target = 'https://wiki.example/game-a/Article'+str(i)
        source.pages[target] = html('Article '+str(i))
        source.pages[child] = web.Response(200, {}, gzip.compress(('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>'+target+'</loc></url></urlset>').encode()))
    for _ in range(8):
        source.run(db, batch_size=1)
    assert all(any(call[0] == child for call in source.calls) for child in children)
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT count(*) FROM web_sitemaps').fetchone() == (13,)
        assert conn.execute('SELECT count(*) FROM web_pages').fetchone() == (13,)


def test_known_new_robots_denial_purges_cached_not_due_child(tmp_path):
    source, db = Fixture(), tmp_path / 'wiki.sqlite3'
    child = 'https://wiki.example/game-a/Restricted'
    source.pages[A], source.pages[child] = html('Home', links=[child]), html('Restricted facts')
    source.run(db)
    source.now += 86_401  # robots due; the article's seven-day revisit is not due
    source.robots += 'Disallow: /game-a/Restricted\n'
    source.calls.clear()
    source.run(db)
    assert child not in [call[0] for call in source.calls]
    assert web.read_web_page(db, child, (A,))['status'] == 'unavailable'
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT count(*) FROM web_passages WHERE url=?', (child,)).fetchone() == (0,)


def test_conditional_revisit_and_304_cannot_override_current_storage_signals(tmp_path):
    source, db = Fixture(), tmp_path / 'wiki.sqlite3'
    source.pages[A] = web.Response(200, {'ETag': 'revision-1'}, html('Verified snapshot'))
    source.run(db)
    source.now += 604_801
    source.pages[A] = web.Response(304, {}, b'')
    source.run(db)
    assert [headers for url, headers in source.calls if url == A][-1]['If-None-Match'] == 'revision-1'
    assert web.search_web_sources('snapshot', db, (A,))
    source.now += 604_801
    source.pages[A] = web.Response(304, {'X-Robots-Tag': 'noindex'}, b'')
    source.run(db)
    assert not web.search_web_sources('snapshot', db, (A,))


def test_full_document_truncation_preserves_previous_licensed_text(tmp_path):
    source, db = Fixture(), tmp_path / 'wiki.sqlite3'
    source.pages[A] = html('Good evidence')
    source.run(db)
    source.now += 604_801
    source.pages[A] = html('Partial new evidence').removesuffix(b'</body></html>')
    assert source.run(db)['failed'] == 1
    assert web.search_web_sources('Good', db, (A,))


def test_read_pagination_is_bounded_and_citation_is_retained(tmp_path):
    source, db = Fixture(), tmp_path / 'wiki.sqlite3'
    source.pages[A] = html('Long licensed article', 'a'*20_000)
    source.run(db)
    first = web.read_web_page(db, A, (A,))
    assert first['status'] == 'reference' and first['next_offset'] == 10
    assert len(first['passages']) == 10 and all(len(p['text']) <= 1600 for p in first['passages'])
    second = web.read_web_page(db, A, (A,), first['next_offset'])
    assert second['citation'] == A and second['next_offset'] is None
    assert ''.join(p['text'] for p in first['passages'] + second['passages']) == 'a'*20_000


@pytest.mark.parametrize('status', [404, 410])
def test_absent_robots_is_cached_empty_rules_with_normal_pacing(tmp_path, status):
    source, db = Fixture(), tmp_path / 'wiki.sqlite3'
    source.robots_status = status
    source.pages[A] = html('Allowed without robots file')
    assert source.run(db)['indexed'] == 1
    assert web.search_web_sources('Allowed', db, (A,))
    source.calls.clear()
    source.run(db)
    assert source.calls == []
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT robots FROM web_hosts').fetchone() == ('',)


@pytest.mark.parametrize('status', [401, 403, 500])
def test_absent_robots_exception_cannot_weaken_actual_refusal_or_outage(tmp_path, status):
    source, db = Fixture(), tmp_path / 'wiki.sqlite3'
    source.robots_status = status
    source.pages[A] = html('Must not fetch')
    assert source.run(db)['failed'] == 1
    assert [call[0] for call in source.calls] == ['https://wiki.example/robots.txt']
    assert not web.search_web_sources('Must', db, (A,))


def test_mediawiki_article_region_excludes_global_and_in_content_chrome(tmp_path):
    source, db = Fixture(), tmp_path / 'wiki.sqlite3'
    source.pages[A] = b'''<html><head><meta property="og:title" content="Getting Started"></head>
      <body><div id="site-navigation">Home News Play Create Forums About Page Discussion</div>
      <a href="/game-a/Irrelevant_Navigation">Site chrome destination</a>
      <main><div class="page-tools">Read Edit View history</div>
      <div id="mw-content-text"><div id="catlinks">Categories and toolbar chrome</div>
      <div class="mw-parser-output"><p>Recruit a resilient fighter before moving toward the village.
      Rest wounded units behind the front line and keep the leader close enough to recruit replacements.</p>
      <p><a href="/game-a/Gameplay_Controls">Learn gameplay controls</a></p></div></div></main>
      <footer><a rel="license" href="https://creativecommons.org/licenses/by-sa/4.0/">CC licence</a></footer>
      </body></html>'''
    source.run(db, batch_size=1)
    row = web.search_web_sources('Recruit fighter', db, (A,))[0]
    assert row['excerpt'].startswith('Recruit a resilient fighter')
    assert row['storage_mode'] == 'full_text' and row['license'] == 'CC-BY-SA-4.0'
    assert 'Home News' not in str(web.read_web_page(db, A, (A,)))
    assert 'Categories and toolbar' not in str(web.read_web_page(db, A, (A,)))
    with sqlite3.connect(db) as conn:
        queued = {url for (url,) in conn.execute('SELECT url FROM web_pages WHERE status=?', ('pending',))}
        assert queued == {'https://wiki.example/game-a/Gameplay_Controls', 'https://wiki.example/game-a/Irrelevant_Navigation'}
    # An unlicensed page must also spend its single 25 words on gameplay text.
    source.now += 604_801
    source.pages[A] = source.pages[A].replace(b'rel="license"', b'rel="external"').replace(b'<footer>', b'<div>').replace(b'</footer>', b'</div>')
    source.run(db, batch_size=1)
    row = web.search_web_sources('Recruit fighter', db, (A,))[0]
    assert row['storage_mode'] == 'short_excerpt' and row['excerpt'].startswith('Recruit a resilient fighter')


def test_absent_robots_does_not_remove_pacing_across_independent_calls(tmp_path):
    source, db = Fixture(), tmp_path / 'wiki.sqlite3'
    source.robots_status = 404
    source.pages[A], source.pages[B] = html('Game A'), html('Game B')
    waits = []
    class AdvancingStop:
        def is_set(self):
            return False
        def wait(self, seconds):
            waits.append(seconds)
            source.now += seconds
            return False
    with patch.object(web.corpus, 'robots_delay', side_effect=lambda rules, floor: floor):
        source.run(db, (A,), stop_event=AdvancingStop())
        source.run(db, (B,), stop_event=AdvancingStop())
    assert waits == [2, 2, 2, 2]
    assert sum(call[0].endswith('/robots.txt') for call in source.calls) == 1


class RedirectRobots(Fixture):
    def __init__(self, responses):
        super().__init__()
        self.responses = responses

    def __call__(self, url, headers):
        if url in self.responses:
            self.calls.append((url, headers))
            return self.responses[url]
        return super().__call__(url, headers)


@pytest.mark.parametrize('status', [301, 302, 303, 307, 308])
def test_same_origin_robots_redirect_to_absent_case_variant_is_cached(tmp_path, status):
    original = 'https://wiki.example/robots.txt'
    case_variant = 'https://wiki.example/Robots.txt'
    source = RedirectRobots({original: web.Response(status, {'Location': case_variant}, b''),
                             case_variant: web.Response(404, {}, b'')})
    db = tmp_path / 'wiki.sqlite3'
    source.pages[A] = html('Gameplay after absent robots redirect')
    assert source.run(db)['indexed'] == 1
    assert [call[0] for call in source.calls][:2] == [original, case_variant]
    assert web.search_web_sources('gameplay', db, (A,))
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT robots FROM web_hosts').fetchone() == ('',)
    source.calls.clear()
    source.run(db)
    assert source.calls == []


def test_relative_robots_redirect_honors_returned_rules(tmp_path):
    original = 'https://wiki.example/robots.txt'
    target = 'https://wiki.example/Robots.txt'
    source = RedirectRobots({original: web.Response(301, {'Location': '/Robots.txt'}, b''),
        target: web.Response(200, {}, b'User-agent: *\nDisallow: /game-a/\n')})
    source.pages[A] = html('Must stay blocked')
    assert source.run(tmp_path / 'wiki.sqlite3')['failed'] == 1
    assert [call[0] for call in source.calls] == [original, target]


@pytest.mark.parametrize('target', ['https://other.example/robots.txt', 'http://wiki.example/Robots.txt',
    'https://127.0.0.1/Robots.txt', 'https://user:secret@wiki.example/Robots.txt',
    'https://wiki.example:8443/Robots.txt', 'https://wiki.example/robots.txt', ''])
def test_robots_redirect_rejects_unvetted_target_before_contact(tmp_path, target):
    original = 'https://wiki.example/robots.txt'
    source = RedirectRobots({original: web.Response(301, {'Location': target}, b'')})
    assert source.run(tmp_path / 'wiki.sqlite3')['failed'] == 1
    assert [call[0] for call in source.calls] == [original]


def test_robots_redirect_allows_five_hops_but_stops_sixth_and_cycles(tmp_path):
    urls = ['https://wiki.example/robots.txt'] + ['https://wiki.example/robots'+str(i)+'.txt' for i in range(1, 7)]
    responses = {urls[i]: web.Response(301, {'Location': urls[i+1]}, b'') for i in range(5)}
    responses[urls[5]] = web.Response(404, {}, b'')
    source = RedirectRobots(responses)
    source.pages[A] = html('At five robots redirects')
    assert source.run(tmp_path / 'allowed.sqlite3')['indexed'] == 1
    responses[urls[5]] = web.Response(301, {'Location': urls[6]}, b'')
    source = RedirectRobots(responses)
    assert source.run(tmp_path / 'bounded.sqlite3')['failed'] == 1
    assert [call[0] for call in source.calls] == urls[:6]
    source = RedirectRobots({urls[0]: web.Response(301, {'Location': urls[1]}, b''),
                             urls[1]: web.Response(301, {'Location': urls[0]}, b'')})
    assert source.run(tmp_path / 'cycle.sqlite3')['failed'] == 1
    assert [call[0] for call in source.calls] == urls[:2]


def test_short_excerpt_prefers_prose_over_nested_main_div_menus_and_inline_links(tmp_path):
    source, db = Fixture(), tmp_path / 'wiki.sqlite3'
    source.pages[A] = b'''<html><head><title>Aircraft manual</title></head><body><main>
      <div><div><a href="/game-a/Installation">Installation</a>Development Manuals Community</div>
      <div>English Deutsch Espanol Francais Nederlands Polski Portugues Italiano</div></div>
      <p>English French Italian</p>
      <p>Aircraft move passengers between <a href="/game-a/Airports">airports</a> and can transport
      valuable cargo over long distances without needing tracks or roads.</p>
      <p>A later paragraph describes operating costs and replacement schedules for the same fictional vehicles.</p>
      </main></body></html>'''
    source.run(db, batch_size=1)
    row = web.search_web_sources('aircraft airports passengers', db, (A,))[0]
    assert row['storage_mode'] == 'short_excerpt'
    assert row['excerpt'].startswith('Aircraft move passengers between airports and can transport')
    assert 'Installation' not in row['excerpt'] and 'English' not in row['excerpt']
    assert len(row['excerpt'].split()) <= 25
    with sqlite3.connect(db) as conn:
        queued = {url for url, in conn.execute("SELECT url FROM web_pages WHERE status='pending'")}
    assert 'https://wiki.example/game-a/Airports' in queued
    assert 'https://wiki.example/game-a/Installation' in queued
    # Choosing the excerpt must not remove any licensed full-text passages.
    parsed = web._Page(source.pages[A].decode())
    assert any('Installation' in block for block in parsed.blocks)
    assert any('later paragraph' in block for block in parsed.blocks)


def test_short_excerpt_without_substantial_prose_retains_nonempty_block_fallback():
    parsed = web._Page('<html><main><div>A brief useful definition without a paragraph remains available in this small reference page.</div></main></html>')
    assert parsed.short_excerpt().startswith('A brief useful definition')


def test_sitemap_same_origin_redirect_and_failure_leave_article_links_working(tmp_path):
    source, db = Fixture(), tmp_path / 'wiki.sqlite3'
    sitemap, target = 'https://wiki.example/sitemap.xml', 'https://wiki.example/renamed-sitemap.xml'
    unlinked = 'https://wiki.example/game-a/Unlinked'
    source.pages[sitemap] = web.Response(301, {'Location': '/renamed-sitemap.xml'}, b'')
    source.pages[target] = web.Response(200, {}, ('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>'+unlinked+'</loc></url></urlset>').encode())
    source.pages[A], source.pages[unlinked] = html('Home'), html('Sitemap discovery')
    assert source.run(db)['indexed'] == 2
    assert [url for url, _ in source.calls][:3] == ['https://wiki.example/robots.txt', sitemap, target]
    assert web.search_web_sources('Sitemap discovery', db, (A,))
    source.now += 604_801
    source.pages[sitemap] = web.Response(302, {'Location': 'https://other.example/sitemap.xml'}, b'')
    linked = 'https://wiki.example/game-a/Linked'
    source.pages[A], source.pages[linked] = html('Home', links=[linked]), html('Fresh linked gameplay')
    report = source.run(db)
    assert report['roots'][A]['discovery_error'] == 'ValueError'
    assert web.search_web_sources('Fresh linked gameplay', db, (A,))
    assert not any(url.startswith('https://other.example') for url, _ in source.calls)


def test_mediawiki_title_query_canonicalization_scope_and_reader_roundtrip(tmp_path):
    from urllib.parse import parse_qs, urlsplit
    source, db = Fixture(), tmp_path / 'wiki.sqlite3'
    root = 'https://wiki.example/index.php?title=Main_Page'
    article = 'https://wiki.example/index.php?title=Aircraft_Manual'
    assert web.parse_roots(root.replace('_', '%20')) == (root,)
    assert web.in_scope(article, root)
    assert web.in_scope('https://wiki.example/wiki/Aircraft_Manual', 'https://wiki.example/w/index.php?title=Main_Page')
    assert web.in_scope('https://wiki.example/w/index.php?title=Aircraft_Manual', 'https://wiki.example/wiki/Main_Page')
    source.pages[root] = html('Main page', links=['?title=Aircraft%20Manual', '?title=Special:Login', '?title=Aircraft&action=edit'])
    source.pages[article] = html('Aircraft Manual', 'Aircraft can move passengers between airports and carry valuable cargo across long distances in the game world.')
    assert source.run(db, (root,))['indexed'] == 2
    result = web.search_web_sources('airports passengers', db, (root,))[0]
    assert result['citation'] == article
    assert web.read_web_page(db, article, (root,))['status'] == 'reference'
    assert all('action' not in parse_qs(urlsplit(url).query) for url, _ in source.calls)
    for bad in (root+'&action=edit', root+'&title=Other', root.replace('Main_Page', ''),
                root.replace('title=', 'action='), root.replace('Main_Page', 'Special%3ALogin'),
                root.replace('Main_Page', 'User:Account'), root.replace('Main_Page', '%252e%252e%252fPrivate')):
        with pytest.raises(ValueError):
            web.parse_roots(bad)


def test_title_query_reaches_validated_transport_and_robots_matching():
    from types import SimpleNamespace
    url = 'https://wiki.example/index.php?title=Aircraft_Manual'
    public = (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))
    with patch.object(socket, 'getaddrinfo', return_value=[public]), \
         patch.object(web.http.client, 'HTTPSConnection') as connection:
        connection.return_value.getresponse.return_value = SimpleNamespace(
            status=200, read=lambda limit: b'HTML', getheaders=lambda: [])
        web._transport(url, {})
        connection.return_value.request.assert_called_once_with('GET', '/index.php?title=Aircraft_Manual', headers={})
    assert not web._allowed('User-agent: *\nDisallow: /index.php?title=Aircraft\n', url)


class MediaWikiAPI(Fixture):
    def __init__(self, root, batches):
        super().__init__()
        self.batches = list(batches)
        self.api_calls = []
        self.pages[root] = html('Declared wiki', extra='<link rel="EditURI" type="application/rsd+xml" href="/api.php?action=rsd">')

    def __call__(self, url, headers):
        from urllib.parse import parse_qs, urlsplit
        if urlsplit(url).path == '/api.php':
            self.calls.append((url, headers))
            self.api_calls.append(parse_qs(urlsplit(url).query))
            value = self.batches.pop(0)
            return value if isinstance(value, web.Response) else web.Response(200, {}, __import__('json').dumps(value).encode())
        return super().__call__(url, headers)


def test_declared_mediawiki_api_enumerates_orphans_across_persisted_batches(tmp_path):
    root = 'https://wiki.example/index.php?title=Main_Page'
    first = {'query': {'allpages': [{'pageid': 1, 'ns': 0, 'title': 'Orphan Aircraft'}]},
             'continue': {'apcontinue': 'Orphan Ships', 'continue': '-||'}}
    last = {'query': {'allpages': [{'pageid': 2, 'ns': 0, 'title': 'Orphan Ships'}]}}
    source, db = MediaWikiAPI(root, [first, last]), tmp_path / 'wiki.sqlite3'
    for name in ('Orphan_Aircraft', 'Orphan_Ships'):
        source.pages[root.replace('Main_Page', name)] = html(name.replace('_', ' '))
    source.run(db, (root,), batch_size=1)  # the API is discovered from actual HTML, not guessed
    assert source.api_calls == []
    assert source.run(db, (root,), batch_size=1)['indexed'] == 1
    assert source.run(db, (root,), batch_size=1)['indexed'] == 1
    assert len(source.api_calls) == 2
    assert source.api_calls[0]['list'] == ['allpages'] and source.api_calls[0]['aplimit'] == ['50']
    assert source.api_calls[1]['apcontinue'] == ['Orphan Ships']
    assert web.search_web_sources('Orphan Aircraft', db, (root,))
    with sqlite3.connect(db) as conn:
        cursor, retry = conn.execute('SELECT cursor,next_attempt FROM web_apis').fetchone()
        assert cursor is None and retry > source.now
    source.run(db, (root,), batch_size=1)
    assert len(source.api_calls) == 2


def test_mediawiki_api_robots_refusal_and_off_origin_declarations_never_contact(tmp_path):
    source, db = MediaWikiAPI(A, []), tmp_path / 'wiki.sqlite3'
    # This endpoint is outside the /game-a/ family and must not be registered.
    source.run(db)
    source.run(db)
    assert source.api_calls == []
    root = 'https://wiki.example/wiki/Main_Page'
    db = tmp_path / 'robots.sqlite3'
    source = MediaWikiAPI(root, [])
    source.robots += 'Disallow: /api.php\n'
    source.run(db, (root,))
    report = source.run(db, (root,))
    assert report['roots'][root]['api']['reason'] == 'robots_disallowed'
    assert source.api_calls == []
    source.pages[root] = html('No alien API', extra='<link rel="EditURI" href="https://other.example/api.php?action=rsd">')
    source.now += 604_801
    source.run(db, (root,))
    assert not any('other.example' in url for url, _ in source.calls)


def test_mediawiki_api_partial_batch_and_bad_continuation_keep_last_good(tmp_path):
    root = 'https://wiki.example/index.php?title=Main_Page'
    token = {'apcontinue': 'Continue', 'continue': '-||'}
    good = {'query': {'allpages': [{'pageid': 1, 'ns': 0, 'title': 'Preserved'}]}, 'continue': token}
    malformed = {'query': {'allpages': [{'pageid': 2, 'ns': 0, 'title': 'Must not queue'}, {'title': 'Malformed'}]}}
    repeated = {'query': {'allpages': []}, 'continue': token}
    source, db = MediaWikiAPI(root, [good, malformed, repeated]), tmp_path / 'wiki.sqlite3'
    source.pages[root.replace('Main_Page', 'Preserved')] = html('Preserved article')
    source.run(db, (root,))
    source.run(db, (root,))
    report = source.run(db, (root,))
    assert report['roots'][root]['api']['state'] == 'failed'
    with sqlite3.connect(db) as conn:
        before = conn.execute('SELECT cursor FROM web_apis').fetchone()
        assert not conn.execute('SELECT 1 FROM web_pages WHERE url LIKE ?', ('%Must_not_queue%',)).fetchone()
    source.now += 901
    source.run(db, (root,))
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT cursor FROM web_apis').fetchone() == before
    assert web.search_web_sources('Preserved', db, (root,))


@pytest.mark.parametrize('request_url', [
    'https://wiki.example/api.php?action=edit&format=json&list=allpages',
    'https://wiki.example/api.php?action=query&format=json&list=allusers',
    'https://starcitizen.tools/api.php?action=query&format=json&list=allpages&apnamespace=0&aplimit=50&apfilterredir=nonredirects',
    'https://robertsspaceindustries.com/api.php?action=query&format=json&list=allpages&apnamespace=0&aplimit=50&apfilterredir=nonredirects',
])
def test_unapproved_api_requests_fail_before_dns(request_url):
    with patch.object(socket, 'getaddrinfo') as dns:
        with pytest.raises(ValueError):
            web._transport(request_url, {})
        dns.assert_not_called()


def test_mediawiki_api_rate_limit_keeps_cursor_and_stops_other_host_contacts(tmp_path):
    root = 'https://wiki.example/index.php?title=Main_Page'
    source, db = MediaWikiAPI(root, [web.Response(429, {'Retry-After': '120'}, b'')]), tmp_path / 'wiki.sqlite3'
    source.run(db, (root,))
    source.calls.clear()
    report = source.run(db, (root,))
    assert report['deferred'] >= 1
    assert len(source.calls) == 1 and '/api.php?' in source.calls[0][0]
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT cursor FROM web_apis').fetchone() == (None,)
    source.calls.clear()
    source.now += 1
    source.run(db, (root,))
    assert source.calls == []


def test_sparse_home_page_still_discovers_links_and_declared_api(tmp_path):
    root = 'https://wiki.example/index.php?title=Main_Page'
    source = MediaWikiAPI(root, [{'query': {'allpages': []}}])
    child = root.replace('Main_Page', 'Manual')
    source.pages[root] = ('<html><head><link rel="EditURI" href="/api.php?action=rsd"></head>'
                          '<body><main><a href="?title=Manual">Manual</a></main></body></html>').encode()
    source.pages[child] = html('Useful gameplay manual')
    db = tmp_path / 'wiki.sqlite3'
    first = source.run(db, (root,), batch_size=1)
    assert first['failed'] == 1  # a tiny link-only page is not invented evidence
    second = source.run(db, (root,), batch_size=1)
    assert second['indexed'] == 1 and len(source.api_calls) == 1
    assert web.search_web_sources('gameplay manual', db, (root,))


def test_publisher_nofollow_does_not_register_api_or_guess_one(tmp_path):
    root = 'https://wiki.example/index.php?title=Main_Page'
    source = MediaWikiAPI(root, [])
    source.pages[root] = html('Do not follow', extra='<meta name="robots" content="nofollow"><link rel="EditURI" href="/api.php?action=rsd">')
    db = tmp_path / 'wiki.sqlite3'
    source.run(db, (root,))
    source.run(db, (root,))
    assert source.api_calls == []
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT count(*) FROM web_apis').fetchone() == (0,)
