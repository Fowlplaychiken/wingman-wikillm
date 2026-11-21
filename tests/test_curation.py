import asyncio
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
DEPENDENCIES = ROOT / "wiki_llm" / "dependencies"


class _SkillStub:
    def __init__(self, config=None, settings=None, wingman=None):
        self.config = config or SimpleNamespace(prompt="Base WikiLLM prompt.")
        self.wingman = wingman
        self.properties = {}

    def retrieve_custom_property_value(self, property_id, errors):
        return self.properties.get(property_id)

    async def get_prompt(self):
        return self.config.prompt

    async def prepare(self):
        pass

    async def unload(self):
        pass


def _load_main_module():
    skill_package = ModuleType("skills")
    skill_package.__path__ = []
    skill_base = ModuleType("skills.skill_base")
    skill_base.Skill = _SkillStub
    skill_base.tool = lambda **_kwargs: lambda function: function
    skill_package.skill_base = skill_base
    with patch.dict(sys.modules, {
        "skills": skill_package,
        "skills.skill_base": skill_base,
    }):
        sys.path.insert(0, str(DEPENDENCIES))
        try:
            spec = importlib.util.spec_from_file_location(
                "wiki_llm_curation_main", ROOT / "wiki_llm" / "main.py"
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        finally:
            sys.path.remove(str(DEPENDENCIES))


class CurationTests:
    def test_game_api_is_optional_and_remains_readable_with_updates_paused(self):
        module = _load_main_module()
        skill = module.WingmanWikiLLM()
        url = "https://api.example.org/game/conditions"
        skill.properties.update({"include_star_citizen": False, "api_sources": url})
        for online in (True, False):
            skill.properties["auto_refresh"] = online
            with patch.object(module, "RefreshScheduler") as scheduler:
                asyncio.run(skill.prepare())
            self.assertEqual([f.url for f in skill._feeds], [url])
            self.assertEqual([f.url for f in scheduler.call_args.kwargs["feeds"]], [url] if online else [])
            self.assertFalse(scheduler.call_args.kwargs["sync_galactapedia"])
        skill.properties["api_sources"] = "# " + url
        with patch.object(module, "RefreshScheduler"):
            asyncio.run(skill.prepare())
        self.assertFalse(skill._feeds)
        skill.properties["api_sources"] = "invalid\n" + url
        with patch.object(module, "RefreshScheduler"):
            asyncio.run(skill.prepare())
        self.assertEqual([f.url for f in skill._feeds], [url])
        self.assertIn("line 1", skill._api_setup_errors[0])

    def test_galactapedia_refresh_respects_online_game_and_source_switches(self):
        module = _load_main_module()
        for settings in ({}, {"include_star_citizen": False}, {"auto_refresh": False},
                         {"sync_galactapedia": False}):
            skill = module.WingmanWikiLLM()
            skill.properties.update(settings)
            with patch.object(module, "RefreshScheduler") as scheduler:
                asyncio.run(skill.prepare())
                assert scheduler.call_args.kwargs["sync_galactapedia"] is (not settings)

    def test_enabled_prompt_limits_private_drafts_to_player_evidence(self):
        module = _load_main_module()
        skill = module.WingmanWikiLLM()
        skill.properties["auto_curation"] = True
        prompt = asyncio.run(skill.get_prompt())
        assert "concrete, game-specific observation/correction" in prompt
        assert "A retrieved wiki article by itself is never evidence" in prompt
        assert "never reviewed" in prompt

    def test_disabled_or_unavailable_setting_requires_explicit_save_request(self):
        module = _load_main_module()
        skill = module.WingmanWikiLLM()
        skill.properties["auto_curation"] = False
        prompt = asyncio.run(skill.get_prompt())
        assert "Automatic private drafts are disabled" in prompt
        assert "only when the player explicitly asks" in prompt
        assert "retrieved wiki article by itself is never evidence" in prompt

        skill.properties.clear()
        prompt = asyncio.run(skill.get_prompt())
        assert "Automatic private drafts are disabled" in prompt

    def test_standard_record_note_tool_remains_the_only_write_path(self):
        module = _load_main_module()
        skill = module.WingmanWikiLLM()
        record = Mock(return_value={"topic": "ship-observation", "revision": 1,
                                    "status": "draft"})
        with patch.object(module, "record_note", record):
            result = asyncio.run(skill.wiki_note(
                "ship-observation", "Ship observation", "Observed component behavior.",
                "draft", source="https://starcitizen.tools/Ship", game_build="4.5.0",
            ))
        record.assert_called_once()
        assert '"status": "draft"' in result
        assert record.call_args.args[:3] == (
            "ship-observation", "Ship observation", "Observed component behavior."
        )

class TestCuration(unittest.TestCase, CurationTests):
    pass
