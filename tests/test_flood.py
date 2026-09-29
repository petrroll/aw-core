from datetime import datetime, timedelta, timezone

from aw_core.models import Event
from aw_transform import (
    active_periods_v2,
    flood,
    flood_v2,
    merge_subwatcher_fields,
)


now = datetime.now(tz=timezone.utc).replace(microsecond=0)
td1s = timedelta(seconds=1)


def test_flood_forward():
    events = [
        Event(timestamp=now, duration=10, data={"a": 0}),
        Event(timestamp=now + 15 * td1s, duration=5, data={"b": 1}),
    ]
    flooded = flood(events)
    assert (flooded[0].timestamp + flooded[0].duration) - flooded[
        1
    ].timestamp == timedelta(0)


def test_flood_forward_merge():
    events = [
        Event(timestamp=now, duration=10),
        Event(timestamp=now + 15 * td1s, duration=5),
    ]
    flooded = flood(events)
    assert len(flooded) == 1
    assert flooded[0].duration == timedelta(seconds=20)


def test_flood_backward():
    events = [
        Event(timestamp=now, duration=5, data={"a": 0}),
        Event(timestamp=now + 10 * td1s, duration=10, data={"b": 1}),
    ]
    flooded = flood(events)
    assert (flooded[0].timestamp + flooded[0].duration) - flooded[
        1
    ].timestamp == timedelta(0)


def test_flood_backward_merge():
    events = [
        Event(timestamp=now, duration=5),
        Event(timestamp=now + 10 * td1s, duration=10),
    ]
    flooded = flood(events)
    assert len(flooded) == 1
    assert flooded[0].duration == timedelta(seconds=20)


def test_flood_negative_gap_same_data():
    events = [
        Event(timestamp=now, duration=100, data={"a": 0}),
        Event(timestamp=now, duration=5, data={"a": 0}),
    ]
    flooded = flood(events)
    total_duration = sum((e.duration for e in flooded), timedelta(0))
    assert len(flooded) == 1
    assert total_duration == timedelta(seconds=100)


def test_flood_negative_gap_differing_data():
    events = [
        Event(timestamp=now, duration=5, data={"a": 0}),
        Event(timestamp=now, duration=100, data={"b": 1}),
    ]
    flooded = flood(events)
    assert flooded == events


def test_flood_negative_small_gap_differing_data():
    events = [
        Event(timestamp=now, duration=100, data={"b": 1}),
        Event(timestamp=now + 99.99 * td1s, duration=100, data={"a": 0}),
    ]
    flooded = flood(events)
    duration = sum((e.duration for e in flooded), timedelta(0))
    assert duration == timedelta(seconds=100 + 99.99)


def test_flood_v2_assigns_gap_to_longer_neighbor_and_ties_left():
    longer_old = Event(timestamp=now, duration=10, data={"name": "old"})
    short_new = Event(timestamp=now + 14 * td1s, duration=2, data={"name": "new"})
    result = flood_v2([short_new, longer_old], 5)
    assert [(event.timestamp, event.duration, event.data["name"]) for event in result] == [
        (now, timedelta(seconds=10), "old"),
        (now + 10 * td1s, timedelta(seconds=4), "old"),
        (now + 14 * td1s, timedelta(seconds=2), "new"),
    ]

    short_old = Event(timestamp=now, duration=2, data={"name": "old"})
    longer_new = Event(timestamp=now + 6 * td1s, duration=10, data={"name": "new"})
    result = flood_v2([longer_new, short_old], 5)
    assert [(event.timestamp, event.duration, event.data["name"]) for event in result] == [
        (now, timedelta(seconds=2), "old"),
        (now + 2 * td1s, timedelta(seconds=4), "new"),
        (now + 6 * td1s, timedelta(seconds=10), "new"),
    ]

    tied_new = Event(timestamp=now + 6 * td1s, duration=2, data={"name": "new"})
    result = flood_v2([tied_new, short_old], 5)
    assert result[0].duration == timedelta(seconds=2)
    assert result[1].timestamp == now + 2 * td1s
    assert result[1].duration == timedelta(seconds=4)
    assert result[1].data["name"] == "old"
    assert result[2].timestamp == now + 6 * td1s


def test_flood_v2_left_anchor_exact_tie_uses_later_input():
    events = [
        Event(id=1, timestamp=now, duration=10, data={"state": "off"}),
        Event(id=2, timestamp=now, duration=10, data={"state": "on"}),
        Event(timestamp=now + 12 * td1s, duration=1, data={"state": "on"}),
    ]

    flooded = flood_v2(events)

    gap = next(event for event in flooded if event.timestamp == now + 10 * td1s)
    assert gap.duration == timedelta(seconds=2)
    assert gap.data == {"state": "on"}
    active = active_periods_v2(
        [["source", flooded]],
        {
            "type": "regex",
            "source": "source",
            "field": "state",
            "regex": "^on$",
        },
    )
    assert len(active) == 1
    assert active[0].duration == timedelta(seconds=13)


def test_flood_v2_right_anchor_exact_tie_uses_later_input():
    events = [
        Event(timestamp=now, duration=1, data={"state": "on"}),
        Event(id=1, timestamp=now + 3 * td1s, duration=10, data={"state": "off"}),
        Event(id=2, timestamp=now + 3 * td1s, duration=10, data={"state": "on"}),
    ]

    flooded = flood_v2(events)

    gap = next(event for event in flooded if event.timestamp == now + td1s)
    assert gap.duration == timedelta(seconds=2)
    assert gap.data == {"state": "on"}
    active = active_periods_v2(
        [["source", flooded]],
        {
            "type": "regex",
            "source": "source",
            "field": "state",
            "regex": "^on$",
        },
    )
    assert len(active) == 1
    assert active[0].duration == timedelta(seconds=13)


def test_flood_v2_retains_overlapping_originals_and_includes_threshold():
    old = Event(timestamp=now, duration=10, data={"name": "same"})
    overlapping = Event(timestamp=now + 5 * td1s, duration=2, data={"name": "same"})
    assert flood_v2([overlapping, old]) == [old, overlapping]

    at_threshold = Event(timestamp=now + 15 * td1s, duration=1, data={"name": "next"})
    result = flood_v2([old, at_threshold], 5)
    assert [(event.timestamp, event.duration) for event in result] == [
        (now, timedelta(seconds=10)),
        (now + 10 * td1s, timedelta(seconds=5)),
        (now + 15 * td1s, timedelta(seconds=1)),
    ]


def test_flood_v2_uses_original_durations_for_each_component_gap():
    events = [
        Event(timestamp=now, duration=99, data={"name": "a"}),
        Event(timestamp=now + 104 * td1s, duration=100, data={"name": "b"}),
        Event(timestamp=now + 205 * td1s, duration=100.5, data={"name": "c"}),
    ]

    result = flood_v2(events, 5)

    assert [(event.data["name"], event.timestamp, event.duration) for event in result] == [
        ("a", now, timedelta(seconds=99)),
        ("b", now + 99 * td1s, timedelta(seconds=5)),
        ("b", now + 104 * td1s, timedelta(seconds=100)),
        ("c", now + 204 * td1s, timedelta(seconds=1)),
        ("c", now + 205 * td1s, timedelta(seconds=100.5)),
    ]


def test_flood_v2_preserves_original_overlap_precedence_after_backward_fill():
    facts = [
        Event(timestamp=now, duration=3, data={"value": "a"}),
        Event(timestamp=now + 5 * td1s, duration=25, data={"value": "b"}),
        Event(timestamp=now + 5 * td1s, duration=20, data={"value": "c"}),
    ]
    flooded = flood_v2(facts, 5)

    # The gap gets B's data, but B and C keep their common original start.
    assert [(event.timestamp, event.duration, event.data["value"]) for event in flooded] == [
        (now, timedelta(seconds=3), "a"),
        (now + 3 * td1s, timedelta(seconds=2), "b"),
        (now + 5 * td1s, timedelta(seconds=25), "b"),
        (now + 5 * td1s, timedelta(seconds=20), "c"),
    ]
    base = [Event(timestamp=now + 20 * td1s, duration=1, data={})]
    enriched = merge_subwatcher_fields(base, flooded, ["value"])
    assert len(enriched) == 1
    assert enriched[0].data["value"] == "b"


def test_flood_v2_finds_component_boundary_past_nested_records():
    events = [
        Event(timestamp=now, duration=1000, data={"name": "long"}),
        Event(timestamp=now + 10 * td1s, duration=1, data={"name": "nested"}),
        Event(timestamp=now + 1002 * td1s, duration=1, data={"name": "right"}),
    ]

    result = flood_v2(events, 5)

    assert result[0] == events[0]
    assert result[1] == events[1]
    assert result[2].timestamp == now + 1000 * td1s
    assert result[2].duration == timedelta(seconds=2)
    assert result[2].data == {"name": "long"}
    assert result[3] == events[2]
