# WikiLLM for Wingman

Ask your Wingman about the game while you play. It answers from an offline library, links to the original article in chat, and remembers your field notes between sessions.

![An explorer’s field notebook beside a spacecraft window](docs/field-guide.webp)

## Install

1. [Download the latest skill ZIP](https://github.com/Fowlplaychiken/wingman-wikillm/releases/latest). Extract the **wiki_llm** folder into `%APPDATA%\ShipBit\WingmanAI\custom_skills`.
2. Reopen Wingman and enable **WikiLLM** on your assistant. Star Citizen is selected by default.
3. Ask **“Why’s it so hard to see on Aberdeen?”** The starter guides are ready immediately. Other articles arrive while Wingman runs.

## What’s on board?

| Library | What you can read offline |
| --- | --- |
| **Star Citizen Wiki** | 3,626 complete articles from September 2023, plus titles and short excerpts from newer pages. The archive predates current patches. [Archive and credits](https://archive.org/details/wiki-starcitizen.tools-20230930) |
| **RSI Galactapedia** | English lore articles fetched for personal use through the community API. Answers cite RSI. A publisher placeholder may have no article text yet. [Galactapedia](https://robertsspaceindustries.com/galactapedia) · [API](https://api.star-citizen.wiki/developers) |

Ask **“How much of the Galactapedia do we have on board?”** for actual coverage on your computer. A count of titles includes pages the library has yet to save.

## Add another wiki

Paste its **home address** in Wingman chat and say **“Add this wiki to my library.”** You can also paste one address per line in **WikiLLM settings → Other game wikis**. Keep Star Citizen alongside it or switch off **Use the Star Citizen library**.

Say **“List my extra wikis,” “Pause the Wesnoth wiki,”** or **“Resume the Wesnoth wiki.”** Pausing hides that source and keeps its saved pages for later. WikiLLM follows nested sections, links, sitemaps and accessible MediaWiki article lists. What it can save depends on what the publisher makes available. [Game APIs, full exports and coverage checks](docs/sources.md)

## Your field notes

Tell your Wingman what you observed. It can draft a new note, revise an existing topic when you correct it, and file a finding you confirm. Reviewed notes can appear alongside the article they refer to. A source update preserves your notes and flags changed citations. Ask **“What have we learned about Aberdeen?”** or **“Which of my notes need review?”**

Notes stay in your computer user’s library. If your Wingman uses a hosted model, it can send matching passages and notes to that provider. Turn off **Automatically draft game notes** if you prefer to decide when to save.

To back up, close Wingman and copy `.starcitizen-wiki` from your user folder. To upgrade, replace the skill folder with the latest ZIP. [Community article license and attribution](docs/sources.md#community-archive)
