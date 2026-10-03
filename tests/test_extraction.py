"""Stage 3: validation (§6.3), number check (§13.8), Gemini provider, golden transcripts.

Unit tests need no network. The golden test runs Gemini through the record/replay
cache: with GEMINI_API_KEY it records missing responses to fixtures/recorded/gemini/;
after that it replays offline. With neither a key nor recordings, it skips.
"""

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models.conversation import ConstraintField as F
from app.models.conversation import ConstraintKind as K
from app.models.conversation import PseudonymousMessage
from app.planning import explain as explain_mod
from app.planning.explain import checked, explain, passes_number_check, template
from app.planning.extraction import WIRE_SCHEMA, validate
from app.providers.cache import RECORDED_DIR, RecordReplayCache, cache_key
from app.providers.real.gemini import (
    EXTRACT_SYSTEM,
    GeminiLLM,
    transcript_prompt,
)
from app.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
TRANSCRIPTS = sorted((ROOT / "fixtures" / "transcripts").glob("*.json"))
NOW = datetime.fromisoformat("2026-10-03T18:00:00-04:00")


def msgs(*items: tuple[str, str, str]) -> list[PseudonymousMessage]:
    return [PseudonymousMessage(message_id=m, pid=p, text=t, ts_local="18:00") for m, p, t in items]


TRANSCRIPT = msgs(("m1", "p1", "I'm starving"), ("m2", "p2", "no sushi pls"))


def item(**overrides) -> dict:
    base = {
        "pid": "p2",
        "field": "cuisine",
        "value": "sushi",
        "polarity": "avoid",
        "kind": "veto",
        "confidence": 0.9,
        "evidence_msg_ids": ["m2"],
    }
    return {**base, **overrides}


def one(raw_item: dict, transcript=TRANSCRIPT):
    result = validate({"constraints": [raw_item], "group_intent": "food"}, transcript)
    return result.constraints[0] if result.constraints else None


# --- §6.3 validation ----------------------------------------------------------------


def test_valid_constraint_is_kept() -> None:
    c = one(item())
    assert (c.pid, c.field, c.value, c.kind, c.polarity) == (
        "p2",
        F.CUISINE,
        "sushi",
        K.VETO,
        "avoid",
    )


def test_evidence_must_exist_and_come_from_that_person() -> None:
    assert one(item(evidence_msg_ids=["m9"])) is None  # no such message
    assert one(item(evidence_msg_ids=["m1"])) is None  # m1 was said by p1, not p2
    assert one(item(evidence_msg_ids=[])) is None  # nothing cited


def test_weak_hard_becomes_soft() -> None:
    c = one(item(field="available_until", value="21:00", kind="hard", confidence=0.5))
    assert c.kind == K.SOFT
    assert one(item(field="available_until", value="21:00", kind="hard")).kind == K.HARD


@pytest.mark.parametrize(("raw", "expected"), [("21:00", "21:00"), ("9:05", "09:05")])
def test_times_are_normalized(raw: str, expected: str) -> None:
    assert one(item(field="available_until", value=raw, kind="hard")).value == expected


@pytest.mark.parametrize("raw", ["9pm", "25:00", "tonight", ""])
def test_bad_times_are_dropped(raw: str) -> None:
    assert one(item(field="available_until", value=raw, kind="hard")) is None


def test_categories_map_to_allowed_values_or_drop() -> None:
    assert one(item(field="category", value="Food", kind="inferred")).value == "food"
    assert one(item(field="category", value="coffee", kind="soft")).value == "cafe"
    assert one(item(field="category", value="spa day", kind="soft")) is None


def test_modes_numbers_and_novelty() -> None:
    assert one(item(field="mode_preference", value="uber", kind="soft")).value == "rideshare"
    assert one(item(field="mode_preference", value="teleport", kind="soft")) is None
    assert one(item(field="max_walking_minutes", value="15", kind="hard")).value == 15.0
    assert one(item(field="max_walking_minutes", value="a while", kind="hard")) is None
    assert one(item(field="novelty", value="3", kind="soft")).value == 1.0


def test_unknown_field_kind_and_intent() -> None:
    assert one(item(field="vibe")) is None
    assert one(item(kind="maybe")) is None
    assert (
        validate({"constraints": [], "group_intent": "party"}, TRANSCRIPT).group_intent == "unknown"
    )
    assert validate({}, TRANSCRIPT).constraints == []


def test_cuisine_list_stays_a_list() -> None:
    assert one(item(value="Sushi, Ramen")).value == ["sushi", "ramen"]


# --- §13.8 number check ---------------------------------------------------------------

FACT = {
    "label": "A",
    "venue": "Ithaca 5 & Dime",
    "max_travel_min": 13,
    "next_best_max_travel_min": 15,
    "arrival_window_min": 0,
    "fits_all_budgets": True,
    "vetoes_respected": ["sushi"],
}


def test_number_check_allows_only_fact_numbers() -> None:
    assert passes_number_check("Nobody travels more than 13 min.", FACT)
    assert passes_number_check("Cocktails at 5 & Dime, 2 min faster than 15.", FACT) is False
    assert passes_number_check("Ithaca 5 & Dime: everyone's there within 13 minutes.", FACT)
    assert passes_number_check("Only about $12 each!", FACT) is False


def test_checked_falls_back_to_template() -> None:
    assert (
        checked("Great pick, max 13 min for everyone.", FACT)
        == "Great pick, max 13 min for everyone."
    )
    assert checked("It costs $25.", FACT) == template(FACT)
    assert checked("", FACT) == template(FACT)
    assert checked(" ".join(["word"] * 30), FACT) == template(FACT)


class StubLLM:
    def __init__(self, texts=None, error=None) -> None:
        self.texts, self.error = texts, error

    async def phrase_explanations(self, facts):
        if self.error:
            raise self.error
        return self.texts


def deps_with(llm) -> SimpleNamespace:
    return SimpleNamespace(llm=llm, settings=Settings(_env_file=None))


async def test_explain_uses_checked_llm_text_and_templates_otherwise() -> None:
    facts = [FACT, {**FACT, "label": "B", "max_travel_min": 15}]
    blurbs = await explain(deps_with(StubLLM(["Close by: 13 min max.", "Only $9!"])), facts)
    assert blurbs == ["Close by: 13 min max.", template(facts[1])]


@pytest.mark.parametrize(
    "llm", [None, StubLLM(error=TimeoutError()), StubLLM(texts=["only one"])], ids=str
)
async def test_explain_falls_back_to_templates(llm) -> None:
    facts = [FACT, {**FACT, "label": "B"}]
    assert await explain(deps_with(llm), facts) == [template(f) for f in facts]


def test_explain_module_has_no_private_imports() -> None:
    assert "app.private" not in Path(explain_mod.__file__).read_text()


# --- Gemini provider (no network) --------------------------------------------------------


class FakeModels:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[dict] = []

    async def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(text=self.text)


def gemini_with(text: str, tmp_path, mode="record") -> tuple[GeminiLLM, FakeModels]:
    llm = GeminiLLM(Settings(_env_file=None, gemini_api_key="k"), RecordReplayCache(mode, tmp_path))
    models = FakeModels(text)
    llm._client = SimpleNamespace(aio=SimpleNamespace(models=models))
    return llm, models


async def test_extract_sends_structured_request_and_validates(tmp_path) -> None:
    reply = json.dumps(
        {
            "constraints": [
                item(),
                item(pid="p1", evidence_msg_ids=["m2"]),  # wrong person: dropped
            ],
            "group_intent": "food",
        }
    )
    llm, models = gemini_with(reply, tmp_path)
    prefs = await llm.extract_preferences(TRANSCRIPT, NOW)

    assert [c.pid for c in prefs.constraints] == ["p2"]
    config = models.calls[0]["config"]
    assert config.temperature == 0
    assert config.response_mime_type == "application/json"
    assert config.response_json_schema == WIRE_SCHEMA
    prompt = models.calls[0]["contents"]
    assert "[m2] p2 at 18:00: no sushi pls" in prompt
    assert "2026-10-03 18:00" in prompt


async def test_platform_message_ids_are_shortened_and_mapped_back(tmp_path) -> None:
    real = msgs(("sim-aaa", "p1", "I'm starving"), ("sim-bbb", "p2", "no sushi pls"))
    reply = json.dumps({"constraints": [item(evidence_msg_ids=["m2"])], "group_intent": "food"})
    llm, models = gemini_with(reply, tmp_path)
    prefs = await llm.extract_preferences(real, NOW)
    assert "sim-" not in models.calls[0]["contents"]
    assert prefs.constraints[0].evidence_msg_ids == ["sim-bbb"]


async def test_extract_replays_without_calling_gemini(tmp_path) -> None:
    llm, _ = gemini_with(json.dumps({"constraints": [item()], "group_intent": "food"}), tmp_path)
    await llm.extract_preferences(TRANSCRIPT, NOW)

    replay, models = gemini_with("not json", tmp_path, mode="replay")
    prefs = await replay.extract_preferences(TRANSCRIPT, NOW)
    assert len(prefs.constraints) == 1
    assert models.calls == []


async def test_phrase_explanations_orders_by_label(tmp_path) -> None:
    reply = {"explanations": [{"label": "B", "text": "bee"}, {"label": "A", "text": "ay"}]}
    llm, _ = gemini_with(json.dumps(reply), tmp_path)
    facts = [{"label": "A"}, {"label": "B"}, {"label": "C"}]
    assert await llm.phrase_explanations(facts) == ["ay", "bee", ""]


# --- Golden transcripts (exit criterion: ≥ 90% of expected constraints) -------------------


def _matches(expected: dict, constraint) -> bool:
    if (constraint.pid, constraint.field.value, constraint.kind.value) != (
        expected["pid"],
        expected["field"],
        expected["kind"],
    ):
        return False
    actual = constraint.value if isinstance(constraint.value, list) else [constraint.value]
    want = expected["value"]
    if isinstance(want, int | float):
        return any(isinstance(a, int | float) and float(a) == float(want) for a in actual)
    return str(want).lower() in [str(a).lower() for a in actual]


def _recorded(llm: GeminiLLM, transcript, now) -> bool:
    request = {
        "model": llm.model,
        "system": EXTRACT_SYSTEM,
        "prompt": transcript_prompt(transcript, now),
        "schema": WIRE_SCHEMA,
    }
    return (RECORDED_DIR / "gemini" / f"{cache_key('gemini', 'extract', request)}.json").exists()


async def test_golden_transcripts() -> None:
    settings = Settings()  # reads GEMINI_API_KEY / GEMINI_MODEL from .env
    llm = GeminiLLM(settings, RecordReplayCache("replay"))
    total = found = 0
    misses: list[str] = []
    wrong: list[str] = []
    for path in TRANSCRIPTS:
        data = json.loads(path.read_text())
        now = datetime.fromisoformat(data["now_local"])
        transcript = [
            PseudonymousMessage(ts_local=now.strftime("%H:%M"), **m) for m in data["messages"]
        ]
        if not settings.gemini_api_key and not _recorded(llm, transcript, now):
            pytest.skip("no GEMINI_API_KEY and no recording; add the key to .env to record")
        prefs = await llm.extract_preferences(transcript, now)
        for exp in data["expected"]:
            total += 1
            if any(_matches(exp, c) for c in prefs.constraints):
                found += 1
            else:
                got = [(c.pid, c.field.value, c.value, c.kind.value) for c in prefs.constraints]
                misses.append(f"{path.stem}: missed {exp} (got {got})")
        for bad in data.get("must_not", []):
            if any(
                c.pid == bad["pid"] and c.field.value == bad["field"] for c in prefs.constraints
            ):
                wrong.append(f"{path.stem}: wrongly attributed {bad}")
    recall = found / total
    print(f"\nGolden extraction: {found}/{total} = {recall:.0%}\n" + "\n".join(misses + wrong))
    assert not wrong, "\n".join(wrong)
    assert recall >= 0.9, "\n".join(misses)
