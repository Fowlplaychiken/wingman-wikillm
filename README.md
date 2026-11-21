# WikiLLM for Wingman

Give your Wingman an offline library for the games you play. Ask a question by voice, get an answer with a link to the original article, and keep your own discoveries between sessions. Star Citizen comes preconfigured. You can use another game on its own or keep several wikis together.

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
| No internet access from the skill | Turn **Update the reference library in the background** off after saving the pages you want. |

Save the settings. While the skill is loaded, WikiLLM checks the site’s sitemap and follows links to deeper pages within the chosen wiki. Each pass resumes where it left off. Ask **“How much of my wiki is available offline?”** to see what has been discovered and saved. Removing a URL hides that source from answers and stops its updates. Adding it back reuses its saved pages. Your field notes survive either change.

## What can it answer offline?

| Source | What WikiLLM saves |
| --- | --- |
| Star Citizen preset | Cited starter guides covering flight, survival, industry and engineering. Updates add community wiki excerpts and revision links, plus a title index for Galactapedia, RSI Support and Comm-Link. A title alone is a lead for further reading. |
| Other public wikis | Article passages on supported openly licensed pages, or a short excerpt where appropriate. Publishers can exclude paths or disallow storage. WikiLLM also checks for unlinked articles on compatible MediaWiki sites with an accessible page-list API. A sign-in, a JavaScript-only page or an unpublished page list can leave a gap. |
| Local exports | Full article text from a MediaWiki XML (plain, gzip or bzip2) or article JSONL export (plain or gzip) you have permission to use. Add its original wiki URL to your enabled sources. Place the file in the library’s `snapshots` folder for automatic import on the next background pass. Replace the same file to update it. |
| Your field notes | Your observations, sources, game builds and corrections. Reviewed notes appear in answers. Drafts and withdrawn notes stay in the revision history. |

Questions search the local index. Opening a publisher link uses the internet. For patch-sensitive questions, include the exact game build. WikiLLM can flag a reviewed claim when it observes a change to a cited source. A reference about a game mechanic describes the mechanic, and current player status still comes from the game.

## Keep your own discoveries

Say **“Save this as a draft field note”** and describe what you saw. When you’ve checked it, ask Wingman to mark it reviewed and cite the source. You can later ask for its history, correct it or withdraw it. Your Wingman’s selected conversation model can also save findings during research. Local and API models use the same tools. Background updates fetch and index sources without using model tokens.

Wingmen under the same computer user account share the library. Other players start with their own notes. If you use a hosted conversation model, Wingman can send it the retrieved passages, including a matching private note. Choose a local conversation model if you want that exchange to stay on your hardware.

## Updates and backups

The updater checks hourly while the skill is loaded. While it is making progress on newly discovered pages, it can take another batch after two minutes. Turn off **Build new libraries between scheduled updates** to use only your chosen interval. You can change the interval and batch size in the skill settings. Site delays and retries still apply. Coverage shows the number of discovered URLs still missing full article text. Only a publisher inventory or complete export can also account for unlinked pages. Full Star Citizen wiki mirrors are not included in the download. [Community wiki content signals](https://starcitizen.tools/robots.txt) · [Community wiki licensing](https://starcitizen.tools/Star_Citizen_Wiki:Copyrights) · [RSI terms](https://robertsspaceindustries.com/en/tos)

Back up the `.starcitizen-wiki` folder in your user home with Wingman closed. It holds the database, notes and any exports you added. To update the skill, replace only its folder under `custom_skills`. WikiLLM works independently of other skills and requires no Wingman Core changes.

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
python -m wiki_llm --no-sc --wiki https://wiki.wesnoth.org/ verify publisher-inventory.jsonl
```

These commands return JSON. Supply `--wiki` once per enabled address on each command. A lookup or page read stays offline. A refresh contacts enabled publishers. For JSONL exports, use one object per line with `url`, `title` and `text`. You can also supply `revision` and `game_build`. The optional inventory check compares every listed `url` and, when provided, its exact `revision` against saved article text. Use a current publisher list independent of the crawl. An excerpt cannot pass this check. Use `--db` or `SC_WIKI_DB` to choose another database.

For a large export, the default limits per file are 512 MiB on disk, 2 GiB expanded and 2 million pages. Set `WIKILLM_SNAPSHOT_MAX_COMPRESSED_BYTES`, `WIKILLM_SNAPSHOT_MAX_EXPANDED_BYTES` or `WIKILLM_SNAPSHOT_MAX_PAGES` before launching Wingman or the command. Each accepts a positive integer, or zero to remove that aggregate limit.

</details>
