# More source options

## Add a source in chat

Say **“Add this to my library”** and paste one public HTTPS address. Use the wiki’s home address for the whole wiki or a directory ending in `/` for a section. For an API, use the URL that lists articles. WikiLLM also recognizes a public MediaWiki `api.php` address and follows it to the wiki it serves. Ask **“Check this address first”** to preview the source without turning on updates.

For a JSON API, WikiLLM checks the first page for article titles, text, links and pagination. When the format is unclear, it asks for the missing fields. When a publisher redirects you to a different site, paste the final public address. Sites that require a login, a browser to render their articles, or a custom API protocol can use a permitted offline export.

## Map a game API

In chat, you can give Wingman the collection address and tell it which fields contain the title, text and citation link. Wingman can turn those fields into a one-line mapping. You can paste an address or mapping directly into **WikiLLM settings → Other game APIs**, one per line. This example selects the Open5e *Adventurer’s Guide* conditions. [Open5e API documentation](https://open5e.com/api-docs)

```json
{"url":"https://api.open5e.com/v2/conditions/?document__key=a5e-ag","items":"results","title":"name","text":"descriptions.0.desc","id_field":"key","url_template":"https://api.open5e.com/v2/conditions/{id}/","next":"next","total":"count"}
```

`items` identifies the record list. `title` and `text` identify fields in each record. Use `url_field` for its original link or construct a citation from a stable ID as shown. For pagination, WikiLLM follows a same-site next-page URL in the JSON or an HTTP Link header. A declared total lets it detect missing entries.

If the API returns a cursor instead of a URL, name the response field and the request parameter. For example, `"next":"nextPageToken","next_param":"pageToken"` passes a returned token into the next request while retaining your filters. For an API that returns the next page number, use its field and parameter, such as `"next":"next_page","next_param":"page"`. [Example of cursor pagination](https://developers.google.com/ad-manager/api/beta/pagination)

The example saved all six advertised articles in an isolated test. The offline reader returned their original links. Pausing hid them and resuming restored them. The unchanged check sent the publisher's validator and received HTTP 304. [Open5e collection](https://api.open5e.com/v2/conditions/?document__key=a5e-ag)

```shell
python -m wiki_llm sources preview https://wiki.openttd.org/en/Manual/Tutorial/
python -m wiki_llm sources add https://wiki.openttd.org/en/Manual/Tutorial/
```

## Updates and offline coverage

For a wiki that declares the GNU Free Documentation License in its copyright footer or document license link, WikiLLM checks the complete license at the publisher's own address. When storage is permitted, your local library keeps the original article HTML, including its notices and source links, and one copy of the publisher's license. The reader cites the article and links to the license. WikiLLM rechecks the terms and withdraws affected copies when the publisher restricts storage. On OpenTTD, all nine English Tutorial articles were readable with DNS disabled, including the closing guidance on the Buses page. [OpenTTD tutorial](https://wiki.openttd.org/en/Manual/Tutorial/) · [Publisher license](https://wiki.openttd.org/License)

A separate import used the publisher's full English OpenTTD article tree. All 1,531 nonempty article files matched their saved text byte for byte with networking disabled, including nested manuals and archives. The only remaining file in the 1,532-page inventory was empty at the source. The count covers the English article tree. [Publisher revision](https://github.com/OpenTTD/wiki-data/tree/48523bd6d32b66f21d5b5602da34e3f6164609ce/Page/en)

WikiLLM updates while the skill is loaded. Change **Hours between background passes** to set the schedule. The initial download continues in small batches while the publisher allows it. Unchanged pages can return a change indicator without sending their bodies again. Some sites supply whole groups of articles or lack change indicators. Switch **Check online wikis for updates** off for offline use.

For a whole wiki, supply its home URL. For a section, use its directory URL ending in `/`. The crawler respects robots.txt and publisher storage policies. Links, section menus, sitemaps and a compatible MediaWiki page list help it reach nested and unlinked pages. A MediaWiki home page can also advertise a separate API for custom article addresses such as `/view/` or `/artikel/`. If an article list is missing or unreadable, Wingman can still use the pages already saved. Ask which pages are available offline. A full export from the publisher can fill gaps.

## Import or verify a full edition

Place a permitted **MediaWiki XML** export (plain, gzip or bzip2) or **article JSONL** export (plain or gzip) in `.starcitizen-wiki/snapshots` in your user folder. Enable that publisher in WikiLLM. The next background pass picks up the file even if online updates are off. Replace the same file to update the edition. Each JSONL line needs `url`, `title` and `text`. Optional `revision` and `game_build` identify the edition.

With Python 3.11 or newer, you can check against the publisher’s URL list from the source checkout or the parent of the installed skill folder. The Wingman skill uses Wingman’s own runtime. Use one JSON object with `url` and optional `revision` per line. The check reports missing, excerpt-only or unverified entries. Adding `--queue-missing` schedules undiscovered URLs within an enabled custom wiki for the next pass.

```shell
py -3.11 -m wiki_llm --no-sc --wiki https://wiki.wesnoth.org/ verify inventory.jsonl --queue-missing
py -3.11 -m wiki_llm --help
```

On macOS or Linux, use `python3 -m wiki_llm` with Python 3.11 or newer.

For a large local export, the defaults per file are 512 MiB on disk, 2 GiB expanded and two million pages. Set `WIKILLM_SNAPSHOT_MAX_COMPRESSED_BYTES`, `WIKILLM_SNAPSHOT_MAX_EXPANDED_BYTES` or `WIKILLM_SNAPSHOT_MAX_PAGES` before starting Wingman to change a limit. Zero removes that aggregate limit.

## Community archive

The September 30, 2023 starter edition contains eligible Star Citizen Wiki contributor articles distributed under CC BY-SA 4.0, with original links and credits. Redirects, media and identified third-party material were excluded. Newer dated imports take precedence. [Source archive](https://archive.org/details/wiki-starcitizen.tools-20230930) · [Contributor credits](https://starcitizen.tools/Star_Citizen_Wiki:Copyrights) · [License](https://creativecommons.org/licenses/by-sa/4.0/) · [Current publisher content signals](https://starcitizen.tools/robots.txt)

## Galactapedia feed observation (2026-11-22)

On 22 November 2026, WikiLLM followed all 15 pages of RSI’s official article catalog and found **1,479 distinct article IDs**. The local community API edition had English text for **1,478 of those 1,479**. Three article URLs had changed slugs and still matched by ID. **Odin** (`RMNaBDyx6W`) appeared in both catalogs. Its official RSI page and the English community API both displayed an editorial placeholder pending review. Every other official article ID returned offline text through the installed reader. This checks availability by ID. The feed may lag later edits to individual articles. [RSI Galactapedia](https://robertsspaceindustries.com/galactapedia) · [Odin](https://robertsspaceindustries.com/galactapedia/article/RMNaBDyx6W-anvil-odin)

The community API supplied 1,480 distinct records, including 1,479 English bodies and the Odin placeholder. Its extra saved body was **Terrence Akari** (`V3qgjK7Jor`), which did not appear in that day’s RSI catalog. A conditional check of all 15 cached community API pages returned HTTP 304. The counts describe this edition and date. Players can keep private field notes alongside the saved articles. [Community API](https://api.star-citizen.wiki/developers)

### Packaged revision retention check (2026-11-22)

If the Galactapedia feed revokes offline storage, WikiLLM withdraws its saved feed articles and validators. Your notes and separately imported editions stay available. During a temporary outage, WikiLLM keeps the last allowed edition.

An isolated upgrade from the packaged 0.3.4 skill to 0.3.5 reused the same external settings profile and personal library. Extra wiki/API source choices, a paused API source, and the Star Citizen/Galactapedia switches remained effective. A controlled feed update for the real Odin ID (`RMNaBDyx6W`) changed its saved state from publisher placeholder to article body while the private database note, a deployment-curated Markdown note, and the bundled curated guide remained readable. The next unchanged pass used the saved validator and downloaded no body. This checks package replacement and revision handling, not a new live publisher edit or an installed Wingman acceptance; the current live feed observation above returned 304 for all 15 stored pages.
