# WikiLLM for Wingman

Ask your Wingman a Star Citizen question by voice, get a link to the original article, and keep your own discoveries between sessions. Star Citizen is the only game enabled by default. Players who want another game can add its wiki in settings.

**[Download WikiLLM](https://github.com/Fowlplaychiken/wingman-wikillm/releases/latest)**

![An explorer’s field notebook open to a moon chart beside a spacecraft window.](docs/field-guide.webp)

## Get started

1. Download the `wiki_llm-*.zip` release asset. Extract the **wiki_llm** folder into Wingman’s custom skills folder. On Windows, paste `%APPDATA%\ShipBit\WingmanAI\custom_skills` into File Explorer.
2. Reopen Wingman and enable **WikiLLM** for your assistant. If the skill doesn’t appear, check that `default_config.yaml` sits directly inside `custom_skills/wiki_llm/`.
3. Try **“Use my wiki. Why is it hard to see when driving on Aberdeen?”** This works immediately from the starter guides. Aberdeen’s sulfurous haze limits visibility. [CIG Galactapedia](https://robertsspaceindustries.com/galactapedia/article/0qanJarxqa-aberdeen-stanton-1b) · [Star Citizen Wiki](https://starcitizen.tools/Aberdeen)

## Choose your games

Open **WikiLLM’s skill settings** in Wingman.

| What you want | What to set |
| --- | --- |
| Star Citizen | Leave **Use the Star Citizen library** on. |
| Another game too | Paste its public wiki home address into **Other game wikis**, one HTTPS URL per line. |
| Only another game | Add its address and turn **Use the Star Citizen library** off. |
| A particular section | Use its directory URL ending in `/`, such as `https://wiki.openttd.org/en/Manual/`. |
| Pause one wiki | Put `#` before its address and save. Remove `#` to show its saved pages and resume updates. |
| No internet access from the skill | Turn **Check online wikis for updates** off. You can still add or replace a local export. |

Ask **“How much of my wiki is available offline?”** to see what the library contains. WikiLLM follows section menus, article links and sitemaps. On compatible MediaWiki sites it also asks for the publisher’s article list to find unlinked pages. Each pass resumes from its saved position.

## What can it answer offline?

| Source | What WikiLLM saves |
| --- | --- |
| Star Citizen Wiki | Cited starter guides, the September 2023 community article archive and short excerpts from newer community pages. Search also finds terms in lists and tables. |
| Galactapedia | English lore articles for personal offline use, supplied by the Star Citizen Wiki community API. Answers link to the original RSI article. Coverage shows when an entry has only a title or a publisher placeholder. Turn off **Update Galactapedia lore** to keep just the saved edition. [API and usage terms](https://api.star-citizen.wiki/developers) |
| Other public wikis | Article passages on supported openly licensed pages, or a short excerpt where appropriate. Publishers can exclude paths or disallow storage. WikiLLM also checks for unlinked articles on compatible MediaWiki sites with an accessible page-list API. A sign-in, a JavaScript-only page or an unpublished page list can leave a gap. |
| Local exports | Full article text from a MediaWiki XML (plain, gzip or bzip2) or article JSONL export (plain or gzip) you have permission to use, from any public source. Add its original article URL to an enabled source (supported RSI article routes use the Star Citizen preset; other publishers use **Other game wikis**). Place the file in the library’s `snapshots` folder for local import on the next background pass. Importing never contacts that publisher. Replace the same file to update it. |
| Your field notes | Your observations, sources, game builds and corrections. Reviewed notes appear in answers. Drafts and withdrawn notes stay in the revision history. |

Questions search the local index. Opening a publisher link uses the internet. For patch-sensitive questions, include the exact game build. WikiLLM can flag a reviewed claim when it observes a change to a cited source. A reference about a game mechanic describes the mechanic, and current player status still comes from the game.

## Keep your own discoveries

Describe something you observed in the game. With **Automatically draft game notes** on, your usual conversation model can save a private draft during that turn. You can also say **“Save this as a draft field note.”** Turn the setting off if you prefer to ask each time.

Ask **“Which of my notes need review?”** to see drafts and notes whose sources have changed. Once you’ve checked the evidence, ask Wingman to mark a note reviewed. You can correct it later, see its history or withdraw it. Background source updates use no model tokens.

Wingmen under the same computer user account share the library. Other players start with their own notes. If you use a hosted conversation model, Wingman can send it the retrieved passages, including a matching private note. Choose a local conversation model if you want that exchange to stay on your hardware.

## Historical Star Citizen articles

The release includes a compact edition of Star Citizen Wiki articles. When you enable the Star Citizen library, WikiLLM indexes these articles locally, even with online updates off. Ask **“How much of my Star Citizen wiki is available offline?”** to see what your installation contains. [WikiTeam3’s September 2023 source archive](https://archive.org/details/wiki-starcitizen.tools-20230930)

Star Citizen Wiki contributors wrote the articles, distributed under CC BY-SA 4.0. WikiLLM selects eligible article revisions from September 30, 2023 and repackages their source text with article links and credits. Redirects, media and identified third-party text are excluded. Answers identify the edition. Newer dated imports take precedence. Current patch mechanics and official Galactapedia entries need their own evidence. [Contributors and licensing](https://starcitizen.tools/Star_Citizen_Wiki:Copyrights) · [Article license](https://creativecommons.org/licenses/by-sa/4.0/)

## Updates and backups

The updater checks hourly while the skill is loaded. During a first sync it can take another batch of newly discovered community pages after two minutes. Change the interval and batch size in skill settings, or turn off **Build new libraries between scheduled updates**. WikiLLM obtains Galactapedia through the community API. Previously cached RSI Support and Comm-Link titles stay searchable. [Galactapedia API](https://api.star-citizen.wiki/developers) · [RSI terms](https://robertsspaceindustries.com/en/tos) · [Community wiki content signals](https://starcitizen.tools/robots.txt)

**Check what you can read offline.** Ask Wingman for wiki coverage. It counts known titles, short excerpts and full articles separately. For Galactapedia it compares saved English articles with the API’s complete list and shows any missing translations or publisher placeholders. If a title changes, an earlier saved RSI link still opens the article when the publisher keeps its ID.

For a wiki you add, the count covers pages discovered so far. Compare it with a publisher’s article list or import its complete export to account for unlinked pages. Each URL counts once per library. Imports can overlap the Star Citizen totals. Private notes have their own history.

Back up the `.starcitizen-wiki` folder in your user home with Wingman closed. It holds the database, notes and any exports you added. To update the skill, replace only its folder under `custom_skills`. WikiLLM works independently of other skills and requires no Wingman Core changes.

**Later syncs reuse your library.** WikiLLM checks publisher update dates and HTTP change indicators when available. A changed child sitemap can reveal a new article even when its parent index stays the same. Sites without change indicators may require another page download to detect an edit. Replacing a local export updates changed, new and removed articles while preserving your notes. [Sitemap update dates](https://www.sitemaps.org/protocol.html) · [HTTP conditional requests](https://www.rfc-editor.org/rfc/rfc9110.html#name-conditional-requests)

If you already added another game in an earlier version, WikiLLM reads its home page again to recover menu links. Other previously saved pages get the same one-time check as they come due. Galactapedia sends changes in groups of articles. One edit can require downloading its group again.

<details>
<summary>Use the library from another assistant or the command line</summary>

From the source checkout or the parent of the installed skill folder, run:

```shell
python -m wiki_llm lookup "Aberdeen visibility"
python -m wiki_llm lookup "life support load" --build 4.10.3
python -m wiki_llm --no-sc --wiki https://wiki.openttd.org/en/Manual/ refresh
python -m wiki_llm --no-sc --wiki https://wiki.openttd.org/en/Manual/ status
python -m wiki_llm --no-sc --wiki https://wiki.openttd.org/en/Manual/ lookup "railways"
python -m wiki_llm import game-wiki.xml
python -m wiki_llm review
python -m wiki_llm --no-sc --wiki https://wiki.wesnoth.org/ verify publisher-inventory.jsonl --queue-missing
```

These commands return JSON. Supply `--wiki` once per enabled address on each command. A lookup or page read stays offline. A refresh contacts enabled publishers. For JSONL exports, use one object per line with `url`, `title` and `text`. You can also supply `revision` and `game_build`. The optional inventory check compares every listed `url` and, when provided, its exact `revision` against saved article text. Use a current publisher list independent of the crawl. An excerpt cannot pass this check. Use `--db` or `SC_WIKI_DB` to choose another database.

Use `--queue-missing` with an inventory to add undiscovered URLs from your enabled custom wikis to the next refresh. Run the check again afterward to see which pages now have article text.

For a large export, the default limits per file are 512 MiB on disk, 2 GiB expanded and 2 million pages. Set `WIKILLM_SNAPSHOT_MAX_COMPRESSED_BYTES`, `WIKILLM_SNAPSHOT_MAX_EXPANDED_BYTES` or `WIKILLM_SNAPSHOT_MAX_PAGES` before launching Wingman or the command. Each accepts a positive integer, or zero to remove that aggregate limit.

</details>
