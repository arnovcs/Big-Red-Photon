"""LLMProvider on Google Gemini via the official `google-genai` SDK (§9.2).

Structured output: `response_mime_type="application/json"` + `response_json_schema`
(checked against google-genai 2.x `GenerateContentConfig`). Temperature 0.

Privacy (§11): extraction sees only pseudonymous pids and message text; explanation
sees only group-safe facts. Prompts and replies are never logged.

Retries live in the callers: the pipeline retries extraction once, and explanation
falls back to templates. So one @go costs exactly 2 Gemini calls when all goes well.
"""

import json
import time
from datetime import datetime
from typing import Any

from google import genai
from google.genai import types

from app.logging import get_logger, kv
from app.models.conversation import GroupPreferences, PseudonymousMessage
from app.planning.extraction import CATEGORIES, WIRE_SCHEMA, validate
from app.providers.cache import RecordReplayCache
from app.settings import Settings

log = get_logger(__name__)

EXTRACT_SYSTEM = """You extract outing preferences from a group's messages to a planning bot.
Each message is from one person, identified only by a pseudonym like p1.

Kinds:
- hard: a firm requirement. "I have to be back by 9" → available_until 21:00, hard.
- soft: a preference. "I'd prefer Korean" → cuisine korean, soft.
- veto: something to rule out. "no sushi" → cuisine sushi, polarity avoid, veto.
- inferred: implied, not stated. "I'm starving" → category food, inferred.

Fields and their value format (value is always a string):
- max_walking_minutes, max_travel_minutes: a number of minutes, e.g. "15".
  "nothing too far" with no number → max_travel_minutes is unknown: put it in unresolved.
- available_until, available_from: 24-hour local "HH:MM". Use the current local time
  to resolve "by 9" (9 PM in the evening, 9 AM only if that is still ahead and fits).
- cuisine: one lowercase word, e.g. "korean", "sushi", "pizza".
- category: one of {categories}. "sports" for anything athletic ("something sporty",
  "let's play a game of basketball"); "activity" for things to do that aren't food or drink.
- activity: the specific thing to do, as a short lowercase phrase, e.g. "pickleball",
  "bowling", "karaoke", "rock climbing", "mini golf". Also give its category.
- novelty: "1" for wanting something new or untried, "0" for wanting somewhere familiar.
- mode_preference: walk, bike, drive, or rideshare. Use polarity avoid for "no driving".

Rules:
- Only attribute a constraint to the person who said it. Cite the message ids it comes from.
- Only extract what the speaker wants for themselves. Ignore what they report about
  people who are not in this chat ("my roommate hates thai" is not a constraint).
- Do not invent constraints. If nothing applies, return an empty list.
- Do not output any prices, distances, or times other than those stated by a person.
- group_intent: food, activity, either, or unknown, from the conversation as a whole.
  Anything to do that isn't eating or drinking is "activity", including sports and vague
  asks like "something fun" (then category activity, soft). Never turn an activity
  request into food.
""".format(categories=", ".join(CATEGORIES))

EXPLAIN_SYSTEM = """You write one short, friendly sentence per plan for a group chat poll.
Use only the facts given for that plan. Do not add any number that is not in that
plan's facts, and do not mention money amounts, people, or anyone's location.
At most 25 words each. Return one explanation per plan, matching its label."""

EXPLAIN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "explanations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"label": {"type": "string"}, "text": {"type": "string"}},
                "required": ["label", "text"],
            },
        }
    },
    "required": ["explanations"],
}


def transcript_prompt(transcript: list[PseudonymousMessage], now_local: datetime) -> str:
    lines = [f"[{m.message_id}] {m.pid} at {m.ts_local}: {m.text}" for m in transcript]
    return (
        f"Current local time: {now_local:%A %Y-%m-%d %H:%M} ({now_local.tzinfo}).\n\n"
        "Messages:\n" + "\n".join(lines)
    )


class GeminiLLM:
    def __init__(self, settings: Settings, cache: RecordReplayCache) -> None:
        self.settings = settings
        self.cache = cache
        self.model = settings.gemini_model
        self._client: genai.Client | None = None

    @property
    def client(self) -> genai.Client:
        # Created lazily so replay mode works without a key.
        if self._client is None:
            self._client = genai.Client(
                api_key=self.settings.gemini_api_key,
                http_options=types.HttpOptions(timeout=int(self.settings.llm_timeout_sec * 1000)),
            )
        return self._client

    async def _json_call(self, method: str, system: str, prompt: str, schema: dict) -> Any:
        async def live() -> str:
            start = time.monotonic()
            response = await self.client.aio.models.generate_content(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    temperature=0,
                    response_mime_type="application/json",
                    response_json_schema=schema,
                    # No tools are used; turning this off also silences an SDK warning.
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                ),
            )
            log.info(
                kv(
                    "provider_call",
                    provider="gemini",
                    method=method,
                    latency_ms=round((time.monotonic() - start) * 1000),
                )
            )
            return response.text or ""

        request = {"model": self.model, "system": system, "prompt": prompt, "schema": schema}
        text = await self.cache.call("gemini", method, request, live)
        return json.loads(text)

    async def extract_preferences(
        self, transcript: list[PseudonymousMessage], now_local: datetime
    ) -> GroupPreferences:
        # The LLM sees short ids (m1, m2, …) instead of platform message ids, so the
        # prompt (and its cache key) depends only on what was said.
        short = [m.model_copy(update={"message_id": f"m{i}"}) for i, m in enumerate(transcript, 1)]
        real_id = {s.message_id: m.message_id for s, m in zip(short, transcript, strict=True)}
        raw = await self._json_call(
            "extract", EXTRACT_SYSTEM, transcript_prompt(short, now_local), WIRE_SCHEMA
        )
        prefs = validate(raw if isinstance(raw, dict) else {}, short)
        for c in prefs.constraints:
            c.evidence_msg_ids = [real_id[e] for e in c.evidence_msg_ids]
        return prefs

    async def phrase_explanations(self, facts: list[dict]) -> list[str]:
        prompt = "Plans:\n" + json.dumps(facts, ensure_ascii=False, default=str)
        raw = await self._json_call("explain", EXPLAIN_SYSTEM, prompt, EXPLAIN_SCHEMA)
        by_label = {
            str(e.get("label")): str(e.get("text", ""))
            for e in (raw.get("explanations") or [] if isinstance(raw, dict) else [])
            if isinstance(e, dict)
        }
        # Missing labels become "" and get the template in the number check.
        return [by_label.get(f["label"], "") for f in facts]
