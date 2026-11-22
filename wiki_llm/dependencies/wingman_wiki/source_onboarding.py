"""Inspect one public source before adding it to the player's library."""
from contextlib import closing
from dataclasses import asdict, replace
from http.client import HTTPException
import json
import re
import sqlite3
from time import time
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

from . import json_feeds, web_sources
from .reference import community_db_path
from .refresh import snapshot_limits
from .source_choices import manage_source

_ACCEPT = "text/html, application/xhtml+xml, application/json, */*;q=0.1"
_CREDENTIAL = re.compile(
    r"(?:api[-_]?key|api[-_]?token|auth(?:orization)?|access[-_]?token|refresh[-_]?token|"
    r"bearer|client[-_]?secret|key|token|password|secret|session(?:id)?|signature|sig|x-amz-.+|x-goog-.+)",
    re.I,
)
_MUTATING_ACTIONS = {"delete", "edit", "login", "logout", "move", "protect", "purge", "rollback", "upload"}
_PRIVATE_ROUTE = re.compile(r"(?:^|/)(?:admin|login|logout|wp-admin|wp-login\.php|xmlrpc\.php)(?:/|$)", re.I)
_BAD_PAGE_TITLE = re.compile(r"^(?:just a moment|access denied|attention required|page not found|404|403)\b", re.I)


class _Blocked(ValueError):
    pass


class _NeedsInput(ValueError):
    pass


def _guided_url(address):
    parts = urlsplit(web_sources.pasted_address(address))
    if parts.username is not None or parts.password is not None:
        raise ValueError("Paste a public HTTPS address without credentials.")
    query = parse_qsl(parts.query, keep_blank_values=True, max_num_fields=100)
    if any(_CREDENTIAL.fullmatch(key) for key, _ in query):
        raise ValueError("Paste a public HTTPS address without keys, tokens or signed links.")
    if (_PRIVATE_ROUTE.search(unquote(parts.path))
            or any(key.lower() == "action" and value.lower() in _MUTATING_ACTIONS for key, value in query)):
        raise ValueError("Paste the public wiki or collection address.")
    host = (parts.hostname or "").lower().rstrip(".")
    if any(host == native or host.endswith("." + native) for native in web_sources._NATIVE):
        raise ValueError("Use the built-in Star Citizen library setting for this publisher.")
    normalized = urlunsplit((
        parts.scheme, parts.netloc, quote(parts.path, safe="/:@-._~!$&'()*+,;=%"),
        quote(parts.query, safe="/?:@-._~!$&'()*+,;=%"), "",
    ))
    return json_feeds._feed_url(normalized)


def _fetch(contact, url, rules, accept=_ACCEPT):
    host = urlsplit(url).netloc
    seen = set()
    for _ in range(6):
        if not web_sources._allowed(rules, url):
            raise _Blocked("The publisher's robots policy disallows checking this address.")
        seen.add(url)
        response = contact.get(url, rules=rules, headers={"Accept": accept})
        if response.status not in (301, 302, 303, 307, 308):
            break
        location = response.headers.get("location")
        if not location:
            raise ValueError("The publisher returned a redirect without a destination.")
        target = _guided_url(urljoin(url, location))
        if urlsplit(target).netloc != host:
            raise _NeedsInput("This address redirects to a different site. Can you paste the final public address directly?")
        if target in seen:
            raise ValueError("The publisher redirected this address in a loop.")
        url = target
    else:
        raise ValueError("The publisher redirected this address too many times.")
    if response.status in (401, 403):
        raise _Blocked("This address requires access the public library does not have.")
    if response.status != 200:
        raise ValueError(f"The publisher returned HTTP {response.status}.")
    return url, response


def _name(label, url):
    return " ".join("".join(char for char in label if char.isprintable()).split())[:120] or urlsplit(url).hostname


def _wiki_preview(url, response, rules):
    root, = web_sources.parse_roots(url)
    _, excerpts_only = web_sources._storage_policy(response.headers)
    page = web_sources._Page(web_sources._payload(response, 4_000_000).decode("utf-8-sig"))
    if page.denied:
        raise _Blocked("The publisher disallows offline use of this page.")
    if not page.complete or _BAD_PAGE_TITLE.match(page.title):
        raise _NeedsInput("I could not see readable wiki content here. Can you paste a public page from inside the wiki?")
    if page.canonical:
        canonical = web_sources._article(root, page.canonical)
        if not canonical or not web_sources._allowed(rules, canonical):
            raise _NeedsInput("The publisher identifies a different article address. Can you paste the canonical public wiki address?")
        # A differing canonical address will be checked by the regular crawler.
    follow = page.follow and not re.search(r"\bnofollow\b", response.headers.get("x-robots-tag", ""), re.I)
    links = {target for href in (page.links if follow else ())
             if (target := web_sources._article(root, href)) and target != root and web_sources._allowed(rules, target)}
    mediawiki = bool(follow and page.edit_uri and web_sources._api_endpoint(root, page.edit_uri))
    readable_page = bool(page.blocks and (page.has_article_text or sum(map(len, page.blocks)) >= 42))
    if not (readable_page or links or mediawiki):
        raise _NeedsInput("I could not see readable wiki content here. Can you paste a public page from inside the wiki?")
    return root, _name(page.title or web_sources.corpus.wiki_title(root), root), {
        "scope": "landing_page", "article_text_found": readable_page,
        "in_scope_links_on_page": len(links), "mediawiki_api_declared": mediawiki,
        "publisher_requires_excerpts": bool(page.no_full_text or excerpts_only or web_sources._reference_only(rules)),
    }


def _collection_paths(payload, prefix="", depth=0):
    if not isinstance(payload, dict) or depth >= 3:
        return []
    paths = []
    for key, value in payload.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", key):
            continue
        path = prefix + key
        if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
            paths.append(path)
        elif isinstance(value, dict):
            paths.extend(_collection_paths(value, path + ".", depth + 1))
    return paths


def _article_links(items, feed):
    links = set()
    for item in items:
        try:
            article = json_feeds._normalized(item, feed)
        except ValueError as exc:
            details = str(exc)
            if "identifier" in details or "URL" in details or "article link" in details:
                raise _NeedsInput("I could not find a usable public article link for each entry. Which field gives the link, or what public address pattern uses the article ID?") from exc
            raise _NeedsInput("I could not find a title and readable text for each entry. Which fields contain the article title and text?") from exc
        if article["url"] in links:
            raise _NeedsInput("Some entries share an article address. Which field gives each entry its own public article link?")
        links.add(article["url"])
    return links


def _feed_preview(feed, response, rules, allow_inference):
    if json_feeds._policy_denial(response.headers, rules):
        raise _Blocked("The publisher disallows offline use of this collection.")
    payload, _ = json_feeds._feed_json(response)
    try:
        items = json_feeds._items(payload, feed)
    except ValueError as exc:
        options = _collection_paths(payload)
        if not allow_inference or len(options) != 1:
            question = "Which collection should I use?" if options else "Can you share the address that lists multiple articles?"
            raise _NeedsInput("I could not pick an article collection. " + question
                              + (" Detected fields: " + ", ".join(options[:8]) + "." if options else "")) from exc
        feed = replace(feed, items=options[0])
        items = json_feeds._items(payload, feed)
    try:
        links = _article_links(items, feed)
    except _NeedsInput:
        if not allow_inference:
            raise
        # Preserve the legacy mapping when its aliases already fit the feed.
        feed = replace(feed, title=feed.title + "|label|headline|page_title|article_title",
                       text=feed.text + "|markdown|abstract|article_text|attributes.body|descriptions.0.desc|desc.0.desc|desc",
                       url_field=feed.url_field + "|permalink|article_url|web_url")
        links = _article_links(items, feed)
    try:
        total = json_feeds._extract_total(payload, feed, snapshot_limits().get("max_pages", json_feeds.MAX_ARTICLES))
        next_url = json_feeds._extract_next(payload, feed, feed.url, response, None)
        if next_url:
            _guided_url(next_url)
    except ValueError as exc:
        raise _NeedsInput("I could not verify the publisher's total or next page. Can you share the public collection address or its pagination mapping?") from exc
    if next_url and (next_url == feed.url or not web_sources._allowed(rules, next_url)):
        raise _NeedsInput("The next page is unavailable under this collection's URL or publisher rules. Can you share a public collection address with usable pagination?")
    if (total is not None and total < len(items)) or (not next_url and total not in (None, len(items))):
        raise _NeedsInput("The publisher's total and pagination do not match the entries I could read. Can you share the collection's paginated address or field mapping?")
    if not (items or next_url or total == 0):
        raise _NeedsInput("This first page is empty and gives no total or continuation. Can you share a public collection address with some entries?")
    route = unquote(urlsplit(feed.url).path.rstrip("/").rsplit("/", 1)[-1]).replace("-", " ").replace("_", " ")
    return feed, _name(route.title() or "Article collection", feed.url), {
        "scope": "first_page", "valid_entries_on_first_page": len(links), "publisher_reported_total": total,
        "more_pages": bool(next_url),
    }


def _mediawiki_url(contact, endpoint, rules):
    parts = urlsplit(endpoint)
    endpoint = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    if web_sources._api_endpoint(endpoint, endpoint) != endpoint:
        raise ValueError("The MediaWiki API address is invalid.")
    request = web_sources._network_url(endpoint + "?" + urlencode(web_sources._SITE_ROUTES))
    _, response = _fetch(contact, request, rules, "application/json")
    if web_sources._storage_policy(response.headers)[0]:
        raise _Blocked("The publisher disallows use of this API response.")
    payload, _ = json_feeds._feed_json(response)
    query = payload.get("query") if isinstance(payload, dict) else None
    general = query.get("general") if isinstance(query, dict) else None
    base = general.get("base") if isinstance(general, dict) else None
    if not isinstance(base, str):
        raise _NeedsInput("I could not find a public wiki page from this API. Can you paste the wiki's home or article address?")
    root, = web_sources.parse_roots(urljoin(endpoint, base))
    if urlsplit(root).netloc != parts.netloc or not (
        web_sources._standard_api_family(root, endpoint) or web_sources._publisher_article_prefix(root, endpoint, general)
    ):
        raise _NeedsInput("I could not verify which wiki belongs to this API. Can you paste the wiki's home or article address?")
    return root


def _inspect(db, url, requested_kind, mapped_feed):
    path = community_db_path(db)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=3)) as connection:
        connection.executescript(web_sources.SCHEMA)

        def transport(address, headers):
            return web_sources._transport(address, headers, validate_url=_guided_url)

        contact = web_sources._Contact(connection, transport, time, None)
        rules = contact.robots(url)
        if mapped_feed is None and urlsplit(url).path.endswith("/api.php"):
            url = _mediawiki_url(contact, url, rules)
            requested_kind = "wiki"
        accept = "application/json" if requested_kind == "api" else _ACCEPT
        url, response = _fetch(contact, url, rules, accept)
    if web_sources._storage_policy(response.headers)[0]:
        raise _Blocked("The publisher disallows offline use of this source.")
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    is_json = content_type == "application/json" or content_type.endswith("+json")
    if not content_type:
        body = web_sources._payload(response, 4_000_000).decode("utf-8-sig").lstrip()
        is_json = body.startswith(("[", "{"))
    if is_json and requested_kind != "wiki":
        feed, name, coverage = _feed_preview(replace(mapped_feed, url=url) if mapped_feed else json_feeds.JsonFeed(url),
                                             response, rules, mapped_feed is None)
        return "api", url, name, coverage, json.dumps(asdict(feed))
    if not is_json and requested_kind != "api" and content_type in ("", "text/html", "application/xhtml+xml"):
        root, name, coverage = _wiki_preview(url, response, rules)
        return "wiki", root, name, coverage, root
    raise _NeedsInput("This address did not return the expected wiki page or JSON collection. Can you paste its public wiki page or collection address?")


def setup_source(db, action, source=None, kind="auto", roots=(), feeds=()):
    """Preview or add a public source, or manage an already-saved selection."""
    if action == "list":
        return manage_source(db, "list", roots=roots, feeds=feeds)
    if action not in {"preview", "add", "pause", "resume", "remove"} or kind not in {"auto", "wiki", "api"}:
        raise ValueError("Choose list, preview, add, pause, resume or remove.")
    if not isinstance(source, str) or len(source.strip().splitlines()) != 1:
        raise ValueError("Paste one public wiki or API address.")
    declaration = source.strip()
    mapped = declaration.startswith("{")
    if mapped and kind == "wiki":
        raise ValueError("A JSON field mapping is for a public JSON API.")
    feed = json_feeds.parse_feeds(declaration)[0] if mapped else None
    listed = manage_source(db, "list", roots=roots, feeds=feeds)
    candidates = [entry for entry in listed["sources"] if kind in ("auto", entry["kind"])
                  and _same_saved_address(declaration, feed, entry)]
    if len(candidates) > 1:
        return {"status": "needs_input", "message": "Which saved source did you mean, the wiki or the API?"}
    current = candidates[0] if candidates else None
    if action not in {"preview", "add"}:
        if current is None:
            return {**listed, "status": "not_found", "message": "That address is not in your extra sources."}
        result = manage_source(db, action, current["url"], current["kind"], roots, feeds)
        result["source_changed"] = result["status"] != "not_found"
        return result
    if current:
        return {**listed, "status": "active" if current["enabled"] else "paused", "source": current,
                "message": "This source is already in your library." if current["enabled"]
                else "This source is paused. Ask to resume it if you want updates.",
                "source_changed": False}
    url = _guided_url(feed.url if feed else declaration)
    try:
        kind, url, name, coverage, value = _inspect(db, url, "api" if mapped else kind, feed)
    except _Blocked as exc:
        return {"status": "blocked", "message": str(exc)}
    except _NeedsInput as exc:
        return {"status": "needs_input", "message": str(exc)}
    except web_sources._Cooldown:
        return {"status": "retry_later", "message": "The publisher asked us to try again later."}
    except (OSError, ValueError, HTTPException, RecursionError):
        return {"status": "unavailable", "message": "I could not read this public address. Check the link or try again later."}
    preview = {"source": {"url": url, "kind": kind, "name": name}, "preview": coverage}
    if action == "preview":
        return {"status": "ready", "message": "This source is ready to add.", **preview}
    result = manage_source(db, "add", value, kind, roots, feeds)
    return {**result, **preview, "source_changed": True}


def _same_saved_address(declaration, feed, entry):
    try:
        if entry["kind"] == "wiki":
            return feed is None and web_sources.parse_roots(declaration) == (entry["url"],)
        return (feed or json_feeds.parse_feeds(declaration)[0]).url == entry["url"]
    except (ValueError, IndexError):
        return False
