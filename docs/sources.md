# More source options

## Game APIs

Paste a public JSON collection URL in **WikiLLM settings → Other game APIs**. Use one address per line. An endpoint with article titles, text and links usually works directly. You can use it alongside a wiki or on its own.

If the publisher uses different field names, paste a one-line mapping. This example selects the Open5e *Adventurer’s Guide* conditions. [Open5e API documentation](https://open5e.com/api-docs)

```json
{"url":"https://api.open5e.com/v2/conditions/?document__key=a5e-ag","items":"results","title":"name","text":"descriptions.0.desc","id_field":"key","url_template":"https://api.open5e.com/v2/conditions/{id}/","next":"next","total":"count"}
```

`items` identifies the record list. `title` and `text` identify fields in each record. Use `url_field` for its original link or construct a citation from a stable ID as shown. For pagination, WikiLLM follows a same-site next-page URL in the JSON or an HTTP Link header. A declared total lets it detect missing entries. APIs requiring a sign-in or a custom protocol need an export.

## Updates and offline coverage

For a wiki that declares the GNU Free Documentation License in its copyright footer or document license link, WikiLLM checks the complete license at the publisher's own address. When storage is permitted, your local library keeps the original article HTML, including its notices and source links, and one copy of the publisher's license. The reader cites the article and links to the license. WikiLLM rechecks the terms and withdraws affected copies when the publisher restricts storage. On OpenTTD, all nine English Tutorial articles were readable with DNS disabled, including the closing guidance on the Buses page. [OpenTTD tutorial](https://wiki.openttd.org/en/Manual/Tutorial/) · [Publisher license](https://wiki.openttd.org/License)

WikiLLM updates while the skill is loaded. Change **Hours between background passes** to set the schedule. The initial download continues in small batches while the publisher allows it. Unchanged pages can return a change indicator without sending their bodies again. Some sites supply whole groups of articles or lack change indicators. Switch **Check online wikis for updates** off for offline use.

For a whole wiki, supply its home URL. For a section, use its directory URL ending in `/`. The crawler respects robots.txt and publisher storage policies. Links, section menus, sitemaps and a compatible MediaWiki page list help it reach nested and unlinked pages. A MediaWiki home page can also advertise a separate API for custom article addresses such as `/view/` or `/artikel/`. If an article list is missing or unreadable, Wingman can still use the pages already saved. Ask which pages are available offline. A full export from the publisher can fill gaps.

## Import or verify a full edition

Place a permitted **MediaWiki XML** export (plain, gzip or bzip2) or **article JSONL** export (plain or gzip) in `.starcitizen-wiki/snapshots` in your user folder. Enable that publisher in WikiLLM. The next background pass picks up the file even if online updates are off. Replace the same file to update the edition. Each JSONL line needs `url`, `title` and `text`. Optional `revision` and `game_build` identify the edition.

From the source checkout or the parent of the installed skill folder, you can check against the publisher’s own URL list. Use one JSON object with `url` and optional `revision` per line. The check reports missing, excerpt-only or unverified entries. Adding `--queue-missing` schedules undiscovered URLs within an enabled custom wiki for the next pass.

```shell
python -m wiki_llm --no-sc --wiki https://wiki.wesnoth.org/ verify inventory.jsonl --queue-missing
python -m wiki_llm --help
```

For a large local export, the defaults per file are 512 MiB on disk, 2 GiB expanded and two million pages. Set `WIKILLM_SNAPSHOT_MAX_COMPRESSED_BYTES`, `WIKILLM_SNAPSHOT_MAX_EXPANDED_BYTES` or `WIKILLM_SNAPSHOT_MAX_PAGES` before starting Wingman to change a limit. Zero removes that aggregate limit.

## Community archive

The September 30, 2023 starter edition contains eligible Star Citizen Wiki contributor articles distributed under CC BY-SA 4.0, with original links and credits. Redirects, media and identified third-party material were excluded. Newer dated imports take precedence. [Source archive](https://archive.org/details/wiki-starcitizen.tools-20230930) · [Contributor credits](https://starcitizen.tools/Star_Citizen_Wiki:Copyrights) · [License](https://creativecommons.org/licenses/by-sa/4.0/) · [Current publisher content signals](https://starcitizen.tools/robots.txt)

## Galactapedia feed observation (2026-11-22)

The local feed uses the public [Star Citizen Wiki community API](https://api.star-citizen.wiki/developers), not a directly enumerated RSI catalog. A conditional check of all 15 stored API pages returned `304 Not Modified`: pages 1–14 each contain 100 records and page 15 contains 80. The 1,480 API IDs and original URLs are unique. Of those records, 1,479 have English text and one title-only placeholder is **Odin** (`RMNaBDyx6W`); all 1,479 available API bodies are present in the checked local snapshot. This is API feed coverage, not a claim that RSI currently lists exactly 1,480 articles. A separate metadata-only RSI app count returned 1,479, but this check did not enumerate its full ID set and cannot identify any difference between the two inventories. The offline feed preserves the placeholder without inventing article text. Users can add their own field notes offline; those notes remain private, separate from publisher article bodies, and do not fill a missing article.

### Packaged revision retention check (2026-11-22)

An isolated upgrade from the packaged 0.3.4 skill to 0.3.5 reused the same external settings profile and personal library. Extra wiki/API source choices, a paused API source, and the Star Citizen/Galactapedia switches remained effective. A controlled feed update for the real Odin ID (`RMNaBDyx6W`) changed its saved state from publisher placeholder to article body while the private database note, a deployment-curated Markdown note, and the bundled curated guide remained readable. The next unchanged pass used the saved validator and downloaded no body. This checks package replacement and revision handling, not a new live publisher edit or an installed Wingman acceptance; the current live feed observation above returned 304 for all 15 stored pages.
