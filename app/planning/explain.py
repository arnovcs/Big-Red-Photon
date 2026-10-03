"""Plan explanations (§13.8): LLM phrasing, checked against computed facts.

The LLM only sees group-safe facts and only rephrases them. Every number in its
sentence must appear in that plan's facts, or the sentence is replaced with the
template. Any failure (no LLM, timeout, wrong count) also falls back to templates.
"""

import asyncio
import re

from app.conversation import copy
from app.deps import Deps
from app.logging import get_logger, kv

log = get_logger(__name__)

MAX_WORDS = 25
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def template(fact: dict) -> str:
    return copy.plan_blurb(fact["max_travel_min"])


def _numbers_in(value: object) -> set[float]:
    """Every number in a fact value, including digits inside strings ("5 & Dime")."""
    if isinstance(value, bool) or value is None:
        return set()
    if isinstance(value, int | float):
        return {float(value)}
    if isinstance(value, str):
        return {float(n) for n in _NUMBER.findall(value)}
    if isinstance(value, list | tuple):
        return set().union(*(_numbers_in(v) for v in value)) if value else set()
    if isinstance(value, dict):
        return set().union(*(_numbers_in(v) for v in value.values())) if value else set()
    return set()


def passes_number_check(text: str, fact: dict) -> bool:
    allowed = _numbers_in(fact)
    return all(float(n) in allowed for n in _NUMBER.findall(text))


def checked(text: object, fact: dict) -> str:
    """The LLM's sentence if it's short, non-empty, and only uses the fact's numbers."""
    if not isinstance(text, str) or not text.strip():
        return template(fact)
    sentence = " ".join(text.split())
    if len(sentence.split()) > MAX_WORDS or not passes_number_check(sentence, fact):
        log.info(kv("explanation_replaced", label=fact.get("label")))
        return template(fact)
    return sentence


async def explain(deps: Deps, facts: list[dict]) -> list[str]:
    """One blurb per plan, in the same order as `facts`."""
    if not facts:
        return []
    if deps.llm is None:
        return [template(f) for f in facts]
    try:
        texts = await asyncio.wait_for(
            deps.llm.phrase_explanations(facts), timeout=deps.settings.llm_timeout_sec
        )
    except Exception:
        log.warning(kv("explain_failed"), exc_info=True)
        return [template(f) for f in facts]
    if len(texts) != len(facts):
        log.warning(kv("explain_wrong_count", got=len(texts), want=len(facts)))
        return [template(f) for f in facts]
    return [checked(t, f) for t, f in zip(texts, facts, strict=True)]
