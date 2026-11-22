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
| **Star Citizen Wiki** | 3,626 full articles from September 2023. Current pages add titles and short excerpts under the publisher’s indexing rules. [Archive and credits](https://archive.org/details/wiki-starcitizen.tools-20230930) |
| **RSI Galactapedia** | The 22 November 2026 library held English text for 1,478 of RSI’s 1,479 listed article IDs. Odin was still a title-only entry in the community feed. Answers cite RSI. [Coverage and sources](docs/sources.md#galactapedia-feed-observation-2026-11-22) |

Ask **“How much of the Galactapedia do we have on board?”** for actual coverage on your computer. A count of titles includes pages the library has yet to save.

## Add another wiki

Paste its **home address** in Wingman chat and say **“Add this wiki to my library.”** You can also paste one address per line in **WikiLLM settings → Other game wikis**. Keep Star Citizen alongside it or switch off **Use the Star Citizen library**.

Say **“List my extra wikis,” “Pause the Wesnoth wiki,” “Resume the Wesnoth wiki,”** or **“Remove the Wesnoth wiki.”** Pausing stops updates and hides its articles. Removing it also takes it off your list. You can add it back to use its saved pages again. WikiLLM follows nested links, sitemaps and accessible MediaWiki article lists. [Game APIs, full exports and coverage checks](docs/sources.md)

## Your field notes

Tell your Wingman what you observed. It can draft a new note, revise an existing topic when you correct it, and file a finding you confirm. Reviewed notes can appear alongside the article they refer to. A source update preserves your notes and flags changed citations. Ask **“What have we learned about Aberdeen?”** or **“Which of my notes need review?”**

Notes stay in your computer user’s library. If your Wingman uses a hosted model, it can send matching passages and notes to that provider. Turn off **Automatically draft game notes** if you prefer to decide when to save.

To back up, close Wingman and copy `.starcitizen-wiki` from your user folder. To upgrade, replace the skill folder with the latest ZIP. [Community article license and attribution](docs/sources.md#community-archive)
