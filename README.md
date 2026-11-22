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
| **RSI Galactapedia** | The 22 November 2026 library held English text for 1,478 of RSI’s 1,479 listed article IDs. RSI lists the remaining entry, Odin, as pending review. Answers cite RSI. [Coverage and sources](docs/sources.md#galactapedia-feed-observation-2026-11-22) |

Ask **“How much of the Galactapedia do we have on board?”** for actual coverage on your computer. A count of titles includes pages the library has yet to save.

## Add a wiki or game API

Paste the wiki’s home address or the API’s public collection address in Wingman chat and say **“Add this to my library.”** WikiLLM checks the address and starts saving pages in the background. Say **“Check this address first”** to see what it found before adding it. If WikiLLM needs more information, it asks for the missing address or API fields.

Say **“List my sources,” “Pause the Wesnoth wiki,” “Resume the Wesnoth wiki,”** or **“Remove the Wesnoth wiki.”** Pausing stops updates and hides its articles. Removing it also takes it off your list. Add it again to use its saved pages. To use only your own sources, switch off **Use the Star Citizen library**.

Say **“How much of this wiki can I read offline?”** Coverage depends on the pages and download permissions the publisher provides. [API examples, offline exports and coverage checks](docs/sources.md)

## Your field notes

- **Add a note:** “Remember that I saw fog on Aberdeen near the ridge.”
- **Correct it later:** “Update my Aberdeen note. I saw the fog on a different route.” Wingman looks for the existing topic and keeps its earlier revisions.
- **Review drafts:** “Which notes need review?” Confirm a finding before Wingman uses it as a fact.

With **Automatically draft game notes** on, Wingman can draft notes from concrete observations you share. Turn it off to save only when you ask. Notes stay in your local library. A hosted Wingman model can receive the notes it uses in an answer.

To back up, close Wingman and copy `.starcitizen-wiki` from your user folder. To upgrade, replace the skill folder with the latest ZIP. [Community article license and attribution](docs/sources.md#community-archive)
