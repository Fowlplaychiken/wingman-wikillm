# More source options

## Game APIs

Paste a public JSON collection URL in **WikiLLM settings → Other game APIs**. Use one address per line. An endpoint with article titles, text and links usually works directly. You can use it alongside a wiki or on its own.

If the publisher uses different field names, paste a one-line mapping. This example selects the Open5e *Adventurer’s Guide* conditions. [Open5e API documentation](https://open5e.com/api-docs)

```json
{"url":"https://api.open5e.com/v2/conditions/?document__key=a5e-ag","items":"results","title":"name","text":"descriptions.0.desc","id_field":"key","url_template":"https://api.open5e.com/v2/conditions/{id}/","next":"next","total":"count"}
```

`items` identifies the record list. `title` and `text` identify fields in each record. Use `url_field` for its original link or construct a citation from a stable ID as shown. For pagination, WikiLLM follows a same-site next-page URL in the JSON or an HTTP Link header. A declared total lets it detect missing entries. APIs requiring a sign-in or a custom protocol need an export.

## Updates and offline coverage

WikiLLM updates while the skill is loaded. Change **Hours between background passes** to set the schedule. The initial download continues in small batches while the publisher allows it. Unchanged pages can return a change indicator without sending their bodies again. Some sites supply whole groups of articles or lack change indicators. Switch **Check online wikis for updates** off for offline use.

For a whole wiki, supply its home URL. For a section, use its directory URL ending in `/`. The crawler respects robots.txt and publisher storage policies. Links, section menus, sitemaps and a compatible MediaWiki page list help it reach nested and unlinked pages. A site may exclude articles or publish only part of its inventory. Ask Wingman for coverage before relying on a specific article offline.

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
