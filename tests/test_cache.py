"""Record/replay cache (§14.1): key stability and replay with no live calls."""

import json
import logging

from app.providers.cache import RecordReplayCache, cache_key


class LiveCall:
    def __init__(self, response) -> None:
        self.response = response
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        return self.response


def test_key_ignores_dict_order() -> None:
    a = cache_key("ors", "matrix", {"x": 1, "y": [1, 2], "z": {"b": 2, "a": 1}})
    b = cache_key("ors", "matrix", {"z": {"a": 1, "b": 2}, "y": [1, 2], "x": 1})
    assert a == b


def test_key_depends_on_provider_method_and_request() -> None:
    base = cache_key("ors", "matrix", {"x": 1})
    assert cache_key("ors", "directions", {"x": 1}) != base
    assert cache_key("nominatim", "matrix", {"x": 1}) != base
    assert cache_key("ors", "matrix", {"x": 2}) != base


def test_depart_at_rounds_to_nearest_15_minutes() -> None:
    def key(t: str) -> str:
        return cache_key("ors", "matrix", {"depart_at": t, "o": [1, 2]})

    assert key("2026-10-03T18:01:00-04:00") == key("2026-10-03T18:07:00-04:00")
    assert key("2026-10-03T18:08:00-04:00") == key("2026-10-03T18:14:59-04:00")
    assert key("2026-10-03T18:07:00-04:00") != key("2026-10-03T18:08:00-04:00")


async def test_replay_hit_makes_no_live_call(tmp_path) -> None:
    request = {"q": "Olin Library"}
    first = LiveCall({"answer": 1})
    await RecordReplayCache("record", tmp_path).call("nominatim", "search", request, first)
    assert first.calls == 1

    second = LiveCall({"answer": "should not be used"})
    result = await RecordReplayCache("replay", tmp_path).call(
        "nominatim", "search", request, second
    )
    assert result == {"answer": 1}
    assert second.calls == 0


async def test_replay_miss_calls_live_and_records(tmp_path, caplog) -> None:
    caplog.set_level(logging.INFO)
    cache = RecordReplayCache("replay", tmp_path)
    live = LiveCall([1, 2, 3])
    assert await cache.call("ors", "matrix", {"k": 1}, live) == [1, 2, 3]
    assert live.calls == 1
    assert "cache_miss" in caplog.text

    path = cache.path_for("ors", cache_key("ors", "matrix", {"k": 1}))
    saved = json.loads(path.read_text())
    assert saved["response"] == [1, 2, 3]
    assert path.parent.name == "ors"

    again = LiveCall("nope")
    assert await cache.call("ors", "matrix", {"k": 1}, again) == [1, 2, 3]
    assert again.calls == 0


async def test_off_always_calls_live_and_writes_nothing(tmp_path) -> None:
    cache = RecordReplayCache("off", tmp_path)
    live = LiveCall("x")
    await cache.call("ors", "matrix", {"k": 1}, live)
    await cache.call("ors", "matrix", {"k": 1}, live)
    assert live.calls == 2
    assert not any(tmp_path.iterdir())


async def test_record_overwrites_with_fresh_response(tmp_path) -> None:
    await RecordReplayCache("record", tmp_path).call("ors", "m", {}, LiveCall("old"))
    await RecordReplayCache("record", tmp_path).call("ors", "m", {}, LiveCall("new"))
    replayed = await RecordReplayCache("replay", tmp_path).call("ors", "m", {}, LiveCall("x"))
    assert replayed == "new"
