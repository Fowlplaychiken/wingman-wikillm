"""Wingman v3 entry point for offline Star Citizen references."""
import asyncio
import json
from pathlib import Path
from typing import Literal, Optional

from skills.skill_base import Skill, tool
from wingman_wiki.library import lookup, note_history, record_note
from wingman_wiki.reference import community_db_path
from wingman_wiki.refresh import RefreshScheduler

_GUIDES = Path(__file__).resolve().with_name("guides")


class WingmanWikiLLM(Skill):
    async def prepare(self):
        await super().prepare()
        self._refresh = None
        if self.retrieve_custom_property_value("auto_refresh", []) is True:
            self._refresh = RefreshScheduler(community_db_path())
            self._refresh.start()

    async def unload(self):
        scheduler = getattr(self, "_refresh", None)
        if scheduler:
            await asyncio.to_thread(scheduler.stop, timeout=20)
            self._refresh = None
        await super().unload()

    @tool(name="wikillm_lookup", description="Search the offline Star Citizen wiki. Cite the original links beside each claim. Label local observations. A needs_review result is historical until checked. Discovery links alone are insufficient evidence. Use live readings for current player state.", summarize=False)
    async def wiki_lookup(self, query: str, game_build: Optional[str] = None) -> str:
        """Search local game references.

        Args:
            query: Topic or question, such as Aberdeen visibility or ship life support.
            game_build: Exact game version if specified by the player.
        """
        return json.dumps(await asyncio.to_thread(lookup, _GUIDES, query, game_build), ensure_ascii=False)

    @tool(name="wikillm_record_note", description="Maintain this player's local wiki as you discover a useful new fact or correction during research. Save a draft for uncertainty or conflicting evidence. Use reviewed for checked evidence or an explicit player observation. Cite the original public URL where available. Exclude secrets and unrelated conversation.", summarize=False)
    async def wiki_note(self, slug: str, title: str, finding: str,
                        status: Literal["draft", "reviewed", "withdrawn"],
                        source: Optional[str] = None, game_build: Optional[str] = None,
                        previous_revision: int = 0) -> str:
        """Maintain local notes with a revision trail.

        Args:
            slug: Stable lowercase topic ID. Keep the same ID for corrections.
            title: Short human-readable title.
            finding: The observed fact, scope and evidence, or reason for withdrawal.
            status: Draft, reviewed or withdrawn.
            source: Original public evidence URL. Omit for local observations.
            game_build: Observed build, if known.
            previous_revision: Latest revision of this note, or zero for a new topic.
        """
        try:
            result = await asyncio.to_thread(record_note, slug, title, finding, status=status,
                                             source=source, game_build=game_build,
                                             previous_revision=previous_revision)
        except ValueError as error:
            result = {"status": "not_saved", "reason": str(error)}
        return json.dumps(result)

    @tool(name="wikillm_note_history", description="Read local note revisions, including open drafts, corrections and withdrawals, before editing.", summarize=False)
    async def wiki_note_history(self, slug: str, before: Optional[int] = None) -> str:
        """Read the newest ten revisions.

        Args:
            slug: The stable topic ID.
            before: Read earlier revisions below this number.
        """
        return json.dumps(await asyncio.to_thread(note_history, slug, before=before), ensure_ascii=False)
