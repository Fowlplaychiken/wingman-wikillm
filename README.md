# Star Citizen WikiLLM for Wingman

Ask your Wingman about Star Citizen by voice, hear the answer, and follow it back to the original source. WikiLLM searches a library on your computer. It works with any Wingman using v3 skills, including one with no other Star Citizen modules installed.

**[Download WikiLLM for Wingman](https://github.com/Fowlplaychiken/wingman-wikillm/releases/latest)**

![An explorer’s field notebook open to a moon chart beside a spacecraft window.](docs/field-guide.webp)

## Get flying

1. Under **Assets**, download the ZIP whose name begins with `wiki_llm-`. Extract the **wiki_llm** folder into Wingman’s **custom skills** folder. On Windows, paste `%APPDATA%\ShipBit\WingmanAI\custom_skills` into File Explorer’s address bar.
2. Reopen Wingman and enable **Star Citizen WikiLLM** for the assistant you use. If the skill doesn’t appear, check that `default_config.yaml` sits directly inside `custom_skills/wiki_llm/`.
3. Ask **“Use the Star Citizen wiki. What makes it hard to see when driving on Aberdeen?”**

> **What a sourced answer sounds like**
>
> Aberdeen’s sulfurous haze limits how far you can see from a rover. [CIG Galactapedia](https://robertsspaceindustries.com/galactapedia/article/0qanJarxqa-aberdeen-stanton-1b) · [Star Citizen Wiki](https://starcitizen.tools/Aberdeen)

That example works on a fresh install, even before the first background update. Try **“Where do I find my ship after I retrieve it at an ASOP terminal?”** or **“In 4.10.3, what does life-support load measure?”** Include a game build when a patch could change the answer.

## What’s in your library?

| Source | What you can look up offline |
| --- | --- |
| **Starter field guides** | Flight, cargo, mining, salvage, survival and engineering. The guides ship with the skill and cite the original sources. |
| **Public reference index** | Short attributed excerpts and revision links from the community Star Citizen Wiki, plus titles and links from CIG’s Galactapedia, RSI Support and Comm-Link. A title points you toward further reading. Opening the publisher’s page requires internet. |
| **Your field notes** | Observations and corrections saved by you or your Wingman. Each can carry a source link, game build and revision history. Drafts stay out of ordinary answers. |

## Keep a discovery

Say **“Save this as a draft field note”** and describe what you observed. Add the build and a source link if you have one. When you’ve checked it, ask Wingman to mark it reviewed. You can correct or withdraw it later. Wingman can also save findings as it researches a question.

**Your notes stay in your own library.** Wingmen using the same computer user account share that library. Other players get the public guides and their own empty notes. The updater never publishes your contributions to the project or the public wikis. If you use a hosted conversation model, Wingman sends it the question and any matched excerpts it uses to answer, which can include a matching field note. A local conversation model keeps that exchange on your hardware.

## Updates and backups

While the skill is loaded, **Update the reference library in the background** checks publishers for changes. Lookups continue to work offline. When a source revision changes, WikiLLM flags affected entries for review. For offline-only use, turn off that setting. To update the **skill and bundled guides**, replace the installed skill folder with the one from the latest release. Your notes live separately.

Back up `.starcitizen-wiki` in your user home folder with Wingman closed. WikiLLM reuses an existing Star Citizen Computer wiki database in that location. [CIG Galactapedia](https://robertsspaceindustries.com/galactapedia/) · [Community Star Citizen Wiki](https://starcitizen.tools/) · [Community text licensing](https://starcitizen.tools/Star_Citizen_Wiki:General_disclaimer)

<details>
<summary>Use the same library with another assistant</summary>

Run these commands from the source checkout or the parent of the installed skill folder.

```shell
python -m wiki_llm lookup "Aberdeen visibility"
python -m wiki_llm lookup "life support load" --build 4.10.3
```

Both commands return JSON with the evidence and original links. The bundled guides are searchable Markdown. Set `SC_WIKI_DB` to use a different local database. From a source checkout, `python -m wiki_llm.dependencies.wingman_wiki.refresh` runs a one-time publisher update and reports how many titles and excerpts are available.

</details>
