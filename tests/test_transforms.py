from pprint import pprint
from datetime import datetime, timedelta, timezone

import pytest
from aw_core.models import Event
from timeslot import Timeslot
from aw_transform import (
    active_periods_v2,
    filter_period_intersect,
    filter_keyvals_regex,
    filter_keyvals,
    merge_subwatcher_fields,
    map_event_fields,
    period_union,
    sort_by_timestamp,
    sort_by_duration,
    sum_durations,
    merge_events_by_keys,
    chunk_events_by_key,
    split_url_events,
    simplify_string,
    union,
    union_no_overlap,
    categorize,
    categorize_v2,
    categorize_v2_explain,
    tag,
    Rule,
)
from aw_transform.filter_period_intersect import _intersecting_eventpairs


def test_map_event_fields_copies_canonical_values():
    event = Event(data={"subject": "Planning", "provider": "Teams"})
    result = map_event_fields([event], {"title": "subject", "app": "provider"})[0]

    assert result.data["title"] == "Planning"
    assert result.data["app"] == "Teams"


def test_map_event_fields_reads_all_values_before_writing_targets():
    event = Event(data={"provider": "Teams", "app": "Original"})
    result = map_event_fields([event], {"app": "provider", "title": "app"})[0]

    assert result.data["app"] == "Teams"
    assert result.data["title"] == "Original"


def test_simplify_string():
    events = [
        Event(data={"label": "(99) Facebook"}),
        Event(data={"label": "(14) YouTube"}),
    ]
    assert simplify_string(events, "label")[0].data["label"] == "Facebook"
    assert simplify_string(events, "label")[1].data["label"] == "YouTube"

    events = [Event(data={"app": "Cemu.exe", "title": "Cemu - FPS: 133.7 - BotW"})]
    assert simplify_string(events, "title")[0].data["title"] == "Cemu - FPS: ... - BotW"

    events = [
        Event(data={"app": "VSCode.exe", "title": "● report.md - Visual Studio Code"})
    ]
    assert (
        simplify_string(events, "title")[0].data["title"]
        == "report.md - Visual Studio Code"
    )


def test_active_periods_v2_intersects_overlapping_rules():
    start = datetime.now(timezone.utc).replace(microsecond=0)
    sources = [
        [
            "window",
            [
                Event(
                    timestamp=start,
                    duration=timedelta(minutes=10),
                    data={"app": "Teams"},
                )
            ],
        ],
        [
            "afk",
            [
                Event(
                    timestamp=start + timedelta(minutes=3),
                    duration=timedelta(minutes=4),
                    data={"status": "not-afk"},
                )
            ],
        ],
    ]
    result = active_periods_v2(
        sources,
        {
            "type": "all",
            "rules": [
                {
                    "type": "regex",
                    "source": "window",
                    "field": "app",
                    "regex": "Teams",
                },
                {
                    "type": "regex",
                    "source": "afk",
                    "field": "status",
                    "regex": "^not-afk$",
                },
            ],
        },
    )

    assert len(result) == 1
    assert result[0].timestamp == start + timedelta(minutes=3)
    assert result[0].duration == timedelta(minutes=4)


def test_active_periods_v2_unions_and_negates_rules():
    start = datetime.now(timezone.utc).replace(microsecond=0)
    sources = [
        [
            "window",
            [
                Event(
                    timestamp=start,
                    duration=timedelta(minutes=2),
                    data={"app": "Editor"},
                ),
                Event(
                    timestamp=start + timedelta(minutes=4),
                    duration=timedelta(minutes=2),
                    data={"app": "Blocked"},
                ),
            ],
        ],
        [
            "meeting",
            [
                Event(
                    timestamp=start + timedelta(minutes=2),
                    duration=timedelta(minutes=2),
                    data={"subject": "Standup"},
                )
            ],
        ],
    ]
    result = active_periods_v2(
        sources,
        {
            "type": "any",
            "rules": [
                {
                    "type": "regex",
                    "source": "window",
                    "field": "app",
                    "regex": "^Blocked$",
                    "negate": True,
                },
                {
                    "type": "regex",
                    "source": "meeting",
                    "field": "subject",
                    "regex": "Standup",
                },
            ],
        },
    )

    assert len(result) == 1
    assert result[0].timestamp == start
    assert result[0].duration == timedelta(minutes=4)


def test_active_periods_v2_rejects_unknown_sources():
    with pytest.raises(ValueError, match="unknown source"):
        active_periods_v2(
            [],
            {
                "type": "regex",
                "source": "missing",
                "field": "status",
                "regex": "active",
            },
        )


def test_active_periods_v2_matches_optional_host():
    event = Event(data={"app": "editor"})
    expression = {
        "type": "regex",
        "source": "window",
        "host": "workstation",
        "field": "app",
        "regex": "editor",
    }

    assert active_periods_v2(
        [["window", [event]]], expression, "workstation"
    )
    assert not active_periods_v2(
        [["window", [event]]], expression, "laptop"
    )

    events = [Event(data={"app": "Gedit", "title": "*test.md - gedit"})]
    assert simplify_string(events, "title")[0].data["title"] == "test.md - gedit"


def test_filter_keyval():
    labels = ["aa", "cc"]
    events = [
        Event(data={"label": "aa"}),
        Event(data={"label": "bb"}),
        Event(data={"label": "cc"}),
    ]
    included_events = filter_keyvals(events, "label", labels)
    excluded_events = filter_keyvals(events, "label", labels, exclude=True)
    assert len(included_events) == 2
    assert len(excluded_events) == 1


def test_filter_keyval_regex():
    events = [
        Event(data={"label": "aa"}),
        Event(data={"label": "bb"}),
        Event(data={"label": "cc"}),
    ]
    events_re = filter_keyvals_regex(events, "label", "aa|cc")
    assert len(events_re) == 2


def test_filter_keyval_regex_keyerror():
    events = [
        Event(data={"label": "aa"}),
        Event(),
        Event(data={"label": "cc"}),
    ]
    events_re = filter_keyvals_regex(events, "label", "aa|cc")
    assert len(events_re) == 2


def test_intersecting_eventpairs():
    td1h = timedelta(hours=1)
    now = datetime.now()

    # Test with two identical lists
    e1 = [
        Event(timestamp=now, duration=td1h),
        Event(timestamp=now + td1h, duration=td1h),
    ]
    e2 = [
        Event(timestamp=now, duration=td1h),
        Event(timestamp=now + td1h, duration=td1h),
    ]
    intersecting = list(_intersecting_eventpairs(e1, e2))
    assert len(intersecting) == 2

    # Test with events in first list being in between events of second list
    e1 = [
        Event(timestamp=now + td1h, duration=td1h),
    ]
    e2 = [
        Event(timestamp=now, duration=td1h),
        Event(timestamp=now + 2 * td1h, duration=td1h),
    ]
    intersecting = list(_intersecting_eventpairs(e1, e2))
    assert not intersecting

    # Test with event in first list being identical to middle event in second list
    e1 = [
        Event(timestamp=now + td1h, duration=td1h),
    ]
    e2 = [
        Event(timestamp=now, duration=td1h),
        Event(timestamp=now + 1 * td1h, duration=td1h),
        Event(timestamp=now + 2 * td1h, duration=td1h),
    ]
    intersecting = list(_intersecting_eventpairs(e1, e2))
    assert len(intersecting) == 1

    # Test same as before, but reversed
    e1 = list(reversed(e1))
    e2 = list(reversed(e2))
    intersecting = list(_intersecting_eventpairs(e1, e2))
    assert len(intersecting) == 1


def test_filter_period_intersect():
    td1h = timedelta(hours=1)
    td30min = timedelta(minutes=30)
    now = datetime.now()

    # Filter 1h event with another 1h event at a 30min offset
    to_filter = [Event(timestamp=now, duration=td1h)]
    filter_with = [Event(timestamp=now + td30min, duration=td1h)]
    filtered_events = filter_period_intersect(to_filter, filter_with)
    assert filtered_events[0].duration == td30min

    # Filter 2x 30min events with a 15min gap with another 45min event in between intersecting both
    to_filter = [
        Event(timestamp=now, duration=td30min),
        Event(timestamp=now + timedelta(minutes=45), duration=td30min),
    ]
    filter_with = [
        Event(timestamp=now + timedelta(minutes=15), duration=timedelta(minutes=45))
    ]
    filtered_events = filter_period_intersect(to_filter, filter_with)
    assert len(filtered_events) == 2
    assert filtered_events[0].duration == timedelta(minutes=15)
    assert filtered_events[1].duration == timedelta(minutes=15)

    # Same as previous intersection, but reversing filter and to_filter events
    to_filter = [
        Event(timestamp=now + timedelta(minutes=15), duration=timedelta(minutes=45))
    ]
    filter_with = [
        Event(timestamp=now, duration=td30min),
        Event(timestamp=now + timedelta(minutes=45), duration=td30min),
    ]
    filtered_events = filter_period_intersect(to_filter, filter_with)
    assert len(filtered_events) == 2
    assert filtered_events[0].duration == timedelta(minutes=15)
    assert filtered_events[1].duration == timedelta(minutes=15)


def test_period_union():
    now = datetime.now(timezone.utc)

    # Events overlapping
    events1 = [Event(timestamp=now, duration=timedelta(seconds=10))]
    events2 = [
        Event(timestamp=now + timedelta(seconds=9), duration=timedelta(seconds=10))
    ]
    unioned_events = period_union(events1, events2)
    assert len(unioned_events) == 1

    # Events adjacent but not overlapping
    events1 = [Event(timestamp=now, duration=timedelta(seconds=10))]
    events2 = [
        Event(timestamp=now + timedelta(seconds=10), duration=timedelta(seconds=10))
    ]
    unioned_events = period_union(events1, events2)
    assert len(unioned_events) == 1

    # Events not overlapping or adjacent
    events1 = [Event(timestamp=now, duration=timedelta(seconds=10))]
    events2 = [
        Event(timestamp=now + timedelta(seconds=11), duration=timedelta(seconds=10))
    ]
    unioned_events = period_union(events1, events2)
    assert len(unioned_events) == 2


def test_sort_by_timestamp():
    now = datetime.now(timezone.utc)
    events = []
    events.append(
        Event(timestamp=now + timedelta(seconds=2), duration=timedelta(seconds=1))
    )
    events.append(
        Event(timestamp=now + timedelta(seconds=1), duration=timedelta(seconds=2))
    )
    events_sorted = sort_by_timestamp(events)
    assert events_sorted == events[::-1]


def test_sort_by_duration():
    now = datetime.now(timezone.utc)
    events = []
    events.append(
        Event(timestamp=now + timedelta(seconds=2), duration=timedelta(seconds=1))
    )
    events.append(
        Event(timestamp=now + timedelta(seconds=1), duration=timedelta(seconds=2))
    )
    events_sorted = sort_by_duration(events)
    assert events_sorted == events[::-1]


def test_sum_durations():
    now = datetime.now(timezone.utc)
    events = []
    for i in range(10):
        events.append(
            Event(timestamp=now + timedelta(seconds=i), duration=timedelta(seconds=1))
        )
    result = sum_durations(events)
    assert result == timedelta(seconds=10)


def test_merge_events_by_keys_1():
    now = datetime.now(timezone.utc)
    events = []
    e1_data = {"label": "a"}
    e2_data = {"label": "b"}
    e1 = Event(data=e1_data, timestamp=now, duration=timedelta(seconds=1))
    e2 = Event(data=e2_data, timestamp=now, duration=timedelta(seconds=1))
    events = events + [e1] * 10
    events = events + [e2] * 5

    # Check that an empty key list has no effect
    assert merge_events_by_keys(events, []) == events

    # Check that trying to merge on unavailable key has no effect
    assert len(merge_events_by_keys(events, ["unknown"])) == 1

    result = merge_events_by_keys(events, ["label"])
    result = sort_by_duration(result)
    print(result)
    print(len(result))
    assert len(result) == 2
    assert result[0].duration == timedelta(seconds=10)
    assert result[1].duration == timedelta(seconds=5)


def test_merge_events_by_keys_2():
    now = datetime.now(timezone.utc)
    events = []
    e1_data = {"k1": "a", "k2": "a"}
    e2_data = {"k1": "a", "k2": "c"}
    e3_data = {"k1": "b", "k2": "a"}
    e1 = Event(data=e1_data, timestamp=now, duration=timedelta(seconds=1))
    e2 = Event(data=e2_data, timestamp=now, duration=timedelta(seconds=1))
    e3 = Event(data=e3_data, timestamp=now, duration=timedelta(seconds=1))
    events = events + [e1] * 10
    events = events + [e2] * 9
    events = events + [e3] * 8
    result = merge_events_by_keys(events, ["k1", "k2"])
    result = sort_by_duration(result)
    print(result)
    print(len(result))
    assert len(result) == 3
    assert result[0].data == e1_data
    assert result[0].duration == timedelta(seconds=10)
    assert result[1].data == e2_data
    assert result[1].duration == timedelta(seconds=9)
    assert result[2].data == e3_data
    assert result[2].duration == timedelta(seconds=8)


def test_chunk_events_by_key():
    now = datetime.now(timezone.utc)
    events = []
    e1_data = {"label1": "1a", "label2": "2a"}
    e2_data = {"label1": "1a", "label2": "2b"}
    e3_data = {"label1": "1b", "label2": "2b"}
    e1 = Event(data=e1_data, timestamp=now, duration=timedelta(seconds=1))
    e2 = Event(data=e2_data, timestamp=now, duration=timedelta(seconds=1))
    e3 = Event(data=e3_data, timestamp=now, duration=timedelta(seconds=1))
    events = [e1, e2, e3]
    result = chunk_events_by_key(events, "label1")
    print(len(result))
    pprint(result)
    assert len(result) == 2
    # Check root label
    assert result[0].data["label1"] == "1a"
    assert result[1].data["label1"] == "1b"
    # Check timestamp
    assert result[0].timestamp == e1.timestamp
    assert result[1].timestamp == e3.timestamp
    # Check duration
    assert result[0].duration == e1.duration + e2.duration
    assert result[1].duration == e3.duration
    # Check subevents
    assert result[0].data["subevents"][0] == e1
    assert result[0].data["subevents"][1] == e2
    assert result[1].data["subevents"][0] == e3


def test_url_parse_event():
    now = datetime.now(timezone.utc)
    e = Event(
        data={"url": "http://asd.com/test/?a=1"},
        timestamp=now,
        duration=timedelta(seconds=1),
    )
    result = split_url_events([e])
    print(result)
    assert result[0].data["$protocol"] == "http"
    assert result[0].data["$domain"] == "asd.com"
    assert result[0].data["$path"] == "/test/"
    assert result[0].data["$params"] == ""
    assert result[0].data["$options"] == "a=1"
    assert result[0].data["$identifier"] == ""

    e2 = Event(
        data={"url": "https://www.asd.asd.com/test/test2/meh;meh2?asd=2&asdf=3#id"},
        timestamp=now,
        duration=timedelta(seconds=1),
    )
    result = split_url_events([e2])
    print(result)
    assert result[0].data["$protocol"] == "https"
    assert result[0].data["$domain"] == "asd.asd.com"
    assert result[0].data["$path"] == "/test/test2/meh"
    assert result[0].data["$params"] == "meh2"
    assert result[0].data["$options"] == "asd=2&asdf=3"
    assert result[0].data["$identifier"] == "id"

    e3 = Event(
        data={"url": "file:///home/johan/myfile.txt"},
        timestamp=now,
        duration=timedelta(seconds=1),
    )
    result = split_url_events([e3])
    print(result)
    assert result[0].data["$protocol"] == "file"
    assert result[0].data["$domain"] == "file"
    assert result[0].data["$path"] == "/home/johan/myfile.txt"
    assert result[0].data["$params"] == ""
    assert result[0].data["$options"] == ""
    assert result[0].data["$identifier"] == ""

    # Test about: URLs
    e4 = Event(
        data={"url": "about:blank"},
        timestamp=now,
        duration=timedelta(seconds=1),
    )
    result = split_url_events([e4])
    assert result[0].data["$protocol"] == "about"
    assert result[0].data["$domain"] == "about"


def test_union():
    now = datetime.now(timezone.utc)

    e1 = Event(timestamp=now - timedelta(seconds=20), duration=timedelta(seconds=5))
    e2 = Event(timestamp=now - timedelta(seconds=10), duration=timedelta(seconds=5))
    e3 = Event(timestamp=now, duration=timedelta(seconds=1))
    e4 = Event(timestamp=now + timedelta(seconds=20), duration=timedelta(seconds=1))

    # union separate event lists with duplicates
    events_union = union([e1, e2, e4], [e2, e3])
    assert events_union == [e1, e2, e3, e4]

    e1 = Event(timestamp=now - timedelta(seconds=20), duration=timedelta(seconds=5))
    e2 = Event(timestamp=now - timedelta(seconds=10), duration=timedelta(seconds=5))
    e3 = Event(timestamp=now - timedelta(seconds=10), duration=timedelta(seconds=10))
    e4 = Event(timestamp=now - timedelta(seconds=5), duration=timedelta(seconds=5))
    e5 = Event(timestamp=now, duration=timedelta(seconds=10))

    # union event lists with intersecting duplicates
    events_union = union([e3, e2, e5], [e1, e3, e4, e5])
    assert events_union == [e1, e2, e3, e4, e5]

    e1 = Event(timestamp=now - timedelta(seconds=30), duration=timedelta(seconds=15))
    e2 = Event(timestamp=now, duration=timedelta(seconds=3))
    e3 = Event(timestamp=now, duration=timedelta(seconds=5))
    e4 = Event(timestamp=now, duration=timedelta(seconds=10))

    # union event lists with same timestamp but different duration duplicates
    events_union = union([e1, e2, e4], [e3, e2, e1])
    assert events_union == [e1, e2, e3, e4]


def test_categorize():
    now = datetime.now(timezone.utc)

    classes = [
        (["Test"], Rule({"regex": "^just"})),
        (["Test", "Subtest"], Rule({"regex": "subtest$"})),
        (["Test", "Ignorecase"], Rule({"regex": "ignorecase", "ignore_case": True})),
    ]
    events = [
        Event(timestamp=now, duration=0, data={"key": "just a test"}),
        Event(timestamp=now, duration=0, data={"key": "just a subtest"}),
        Event(timestamp=now, duration=0, data={"key": "just a IGNORECASE test"}),
        Event(timestamp=now, duration=0, data={}),
    ]
    events = categorize(events, classes)

    assert events[0].data["$category"] == ["Test"]
    assert events[1].data["$category"] == ["Test", "Subtest"]
    assert events[2].data["$category"] == ["Test", "Ignorecase"]
    assert events[3].data["$category"] == ["Uncategorized"]


def test_categorize_v2_nested_rules_and_scores():
    event = Event(
        data={
            "app": "devenv.exe",
            "$source.vdesktop.vdesktop": "Personal",
        }
    )
    categories = [
        {
            "id": "work",
            "name": ["Work"],
            "rule": {"type": "regex", "field": "app", "regex": "devenv"},
        },
        {
            "id": "personal",
            "name": ["Personal"],
            "rule": {
                "type": "all",
                "rules": [
                    {"type": "regex", "field": "app", "regex": "devenv"},
                    {
                        "type": "regex",
                        "source": "vdesktop",
                        "field": "vdesktop",
                        "regex": "^Personal$",
                        "weight": 10,
                    },
                ],
            },
        },
    ]

    result = categorize_v2([event], categories)[0]
    assert result.data["$category"] == ["Personal"]
    assert result.data["$category_score"] == 10
    assert result.data["$category_rule"] == "personal"


def test_categorize_v2_any_uses_highest_matching_branch_only():
    event = Event(data={"app": "devenv.exe", "title": "ActivityWatch"})
    categories = [
        {
            "name": ["Work"],
            "rule": {
                "type": "any",
                "rules": [
                    {
                        "type": "regex",
                        "field": "app",
                        "regex": "devenv",
                        "weight": 2,
                    },
                    {
                        "type": "regex",
                        "field": "title",
                        "regex": "ActivityWatch",
                        "weight": 5,
                    },
                ],
            },
        }
    ]

    result = categorize_v2([event], categories)[0]
    assert result.data["$category_score"] == 5


def test_categorize_v2_explain_includes_branches_and_parent_gates():
    categories = [
        {
            "id": "parent",
            "name": ["Work"],
            "rule": {"type": "regex", "field": "app", "regex": "Code"},
        },
        {
            "id": "child",
            "name": ["Work", "Project"],
            "requires": ["parent"],
            "rule": {
                "type": "any",
                "rules": [
                    {"type": "regex", "field": "title", "regex": "Other", "weight": 1},
                    {"type": "regex", "field": "title", "regex": "Project", "weight": 5},
                ],
            },
        },
    ]
    result = categorize_v2_explain(
        [Event(data={"app": "Code", "title": "Project"})], categories
    )[0]
    explain = result.data["$category_explain"]

    assert explain["winner"] == "child"
    child = next(candidate for candidate in explain["candidates"] if candidate["id"] == "child")
    assert child["eligible"] is True
    assert child["requirements"] == {"parent": True}
    assert child["expression"]["selected"] == 1


def test_categorize_v2_parent_requirements_are_gates():
    event = Event(data={"app": "devenv.exe", "title": "Project Alpha"})
    categories = [
        {
            "id": "project",
            "name": ["Project Alpha"],
            "rule": {"type": "regex", "field": "title", "regex": "Project Alpha"},
        },
        {
            "id": "project-code",
            "name": ["Project Alpha", "Code"],
            "requires": ["project"],
            "rule": {"type": "regex", "field": "app", "regex": "devenv"},
        },
        {
            "id": "other-code",
            "name": ["Other", "Code"],
            "requires": ["missing-project-match"],
            "rule": {"type": "regex", "field": "app", "regex": "devenv"},
        },
        {
            "id": "missing-project-match",
            "name": ["Other"],
            "rule": {"type": "regex", "field": "title", "regex": "Other"},
        },
    ]

    result = categorize_v2([event], categories)[0]
    assert result.data["$category"] == ["Project Alpha", "Code"]


def test_categorize_v2_negative_requires_a_present_field():
    categories = [
        {
            "name": ["Not Personal"],
            "rule": {
                "type": "regex",
                "field": "vdesktop",
                "regex": "^Personal$",
                "negate": True,
            },
        }
    ]
    missing = categorize_v2([Event(data={"app": "test"})], categories)[0]
    present = categorize_v2(
        [Event(data={"app": "test", "vdesktop": "Work"})], categories
    )[0]

    assert missing.data["$category"] == ["Uncategorized"]
    assert present.data["$category"] == ["Not Personal"]


def test_categorize_v2_weight_zero_preserves_depth_and_last_wins():
    event = Event(data={"title": "matching"})
    categories = [
        {
            "id": "first",
            "name": ["First"],
            "rule": {"type": "regex", "regex": "matching"},
        },
        {
            "id": "deep",
            "name": ["First", "Deep"],
            "rule": {"type": "regex", "regex": "matching"},
        },
        {
            "id": "last",
            "name": ["Last", "Deep"],
            "rule": {"type": "regex", "regex": "matching"},
        },
    ]

    result = categorize_v2([event], categories)[0]
    assert result.data["$category"] == ["Last", "Deep"]


def test_categorize_v2_preserves_legacy_field_selection():
    categories = [
        {
            "id": "all-fields",
            "name": ["All fields"],
            "rule": {"type": "regex", "regex": "match"},
        },
        {
            "id": "selected-fields",
            "name": ["Selected fields"],
            "rule": {
                "type": "regex",
                "fields": ["app", "title"],
                "regex": "match",
            },
        },
    ]

    all_fields = categorize_v2(
        [Event(data={"project": "match", "app": "other"})], categories
    )[0]
    selected_fields = categorize_v2(
        [Event(data={"project": "other", "title": "match"})], categories
    )[0]

    assert all_fields.data["$category"] == ["All fields"]
    assert selected_fields.data["$category"] == ["Selected fields"]


def test_categorize_v2_scalar_matching_is_opt_in():
    string_only = [
        {
            "name": ["String only"],
            "rule": {"type": "regex", "field": "audible", "regex": "^true$"},
        }
    ]
    scalar = [
        {
            "name": ["Scalar"],
            "rule": {
                "type": "regex",
                "field": "audible",
                "regex": "^true$",
                "value_mode": "scalar",
            },
        }
    ]

    assert categorize_v2([Event(data={"audible": True})], string_only)[0].data[
        "$category"
    ] == [
        "Uncategorized"
    ]
    assert categorize_v2([Event(data={"audible": True})], scalar)[0].data[
        "$category"
    ] == ["Scalar"]


def test_categorize_v2_rejects_dependency_cycles():
    categories = [
        {
            "id": "a",
            "name": ["A"],
            "requires": ["b"],
            "rule": {"type": "regex", "regex": "test"},
        },
        {
            "id": "b",
            "name": ["B"],
            "requires": ["a"],
            "rule": {"type": "regex", "regex": "test"},
        },
    ]

    with pytest.raises(ValueError, match="dependency cycle"):
        categorize_v2([Event(data={"title": "test"})], categories)


def test_categorize_v2_supports_maximum_length_dependency_chain():
    categories = []
    for index in range(1000):
        categories.append(
            {
                "id": f"rule-{index}",
                "name": [f"Rule {index}"],
                "requires": [f"rule-{index - 1}"] if index else [],
                "rule": {"type": "regex", "field": "title", "regex": "match"},
            }
        )

    result = categorize_v2([Event(data={"title": "match"})], categories)[0]

    assert result.data["$category"] == ["Rule 999"]


def test_categorize_v2_source_named_base_uses_its_namespace():
    categories = [
        {
            "name": ["Base"],
            "rule": {
                "type": "regex",
                "source": "base",
                "regex": "Personal",
            },
        }
    ]
    event = Event(data={"app": "devenv.exe", "$source.base.name": "Personal"})

    result = categorize_v2([event], categories)[0]
    assert result.data["$category"] == ["Base"]


def test_categorize_v2_default_source_excludes_enriched_fields():
    categories = [
        {
            "name": ["Base"],
            "rule": {
                "type": "regex",
                "regex": "Personal",
            },
        }
    ]
    event = Event(data={"app": "devenv.exe", "$source.vdesktop.name": "Personal"})

    result = categorize_v2([event], categories)[0]
    assert result.data["$category"] == ["Uncategorized"]


def test_categorize_v2_is_repeatable():
    categories = [
        {
            "id": "first-rule",
            "name": ["First"],
            "rule": {"type": "regex", "field": "title", "regex": "match"},
        },
        {
            "id": "matches-only-metadata",
            "name": ["First", "Metadata"],
            "rule": {"type": "regex", "regex": "first-rule"},
        },
    ]
    event = Event(data={"title": "match"})

    first = categorize_v2([event], categories)[0]
    assert first.data["$category"] == ["First"]
    second = categorize_v2([first], categories)[0]
    assert second.data["$category"] == ["First"]


def test_categorize_v2_rejects_invalid_regex():
    categories = [
        {
            "name": ["Invalid"],
            "rule": {"type": "regex", "regex": "["},
        }
    ]

    with pytest.raises(ValueError, match="invalid regex"):
        categorize_v2([Event(data={"title": "test"})], categories)


def test_categorize_v2_enforces_expression_limits():
    nested = {"type": "regex", "regex": "test"}
    for _ in range(32):
        nested = {"type": "all", "rules": [nested]}

    with pytest.raises(ValueError, match="maximum depth"):
        categorize_v2(
            [Event(data={"title": "test"})],
            [{"name": ["Too deep"], "rule": nested}],
        )
    with pytest.raises(ValueError, match="maximum length"):
        categorize_v2(
            [Event(data={"title": "test"})],
            [
                {
                    "name": ["Too long"],
                    "rule": {"type": "regex", "regex": "x" * 4097},
                }
            ],
        )


def test_categorize_v2_matches_optional_host():
    categories = [
        {
            "name": ["Workstation"],
            "rule": {
                "type": "regex",
                "host": "workstation",
                "field": "app",
                "regex": "editor",
            },
        }
    ]

    matching = categorize_v2(
        [Event(data={"app": "editor"})], categories, "workstation"
    )[0]
    other = categorize_v2(
        [Event(data={"app": "editor"})], categories, "laptop"
    )[0]
    assert matching.data["$category"] == ["Workstation"]
    assert other.data["$category"] == ["Uncategorized"]
    assert "$host" not in matching.data


def test_tags():
    now = datetime.now(timezone.utc)

    classes = [
        ("Test", Rule({"regex": "value$"})),
        ("Test", Rule({"regex": "^just"})),
    ]
    events = [
        Event(timestamp=now, duration=0, data={"key": "just a test value"}),
        Event(timestamp=now, duration=0, data={}),
    ]
    events = tag(events, classes)

    assert len(events[0].data["$tags"]) == 2
    assert len(events[1].data["$tags"]) == 0


def test_union_no_overlap():
    from pprint import pprint

    now = datetime(2018, 1, 1, 0, 0)
    td1h = timedelta(hours=1)
    events1 = [
        Event(timestamp=now + 2 * i * td1h, duration=td1h, data={"test": 1})
        for i in range(3)
    ]
    events2 = [
        Event(timestamp=now + (2 * i + 0.5) * td1h, duration=td1h, data={"test": 2})
        for i in range(3)
    ]

    events_union = union_no_overlap(events1, events2)
    # pprint(events_union)
    dur = sum((e.duration for e in events_union), timedelta(0))
    assert dur == timedelta(hours=4, minutes=30)
    assert sorted(events_union, key=lambda e: e.timestamp)

    events_union = union_no_overlap(events2, events1)
    # pprint(events_union)
    dur = sum((e.duration for e in events_union), timedelta(0))
    assert dur == timedelta(hours=4, minutes=30)
    assert sorted(events_union, key=lambda e: e.timestamp)

    events1 = [
        Event(timestamp=now + (2 * i) * td1h, duration=td1h, data={"test": 1})
        for i in range(3)
    ]
    events2 = [Event(timestamp=now, duration=5 * td1h, data={"test": 2})]
    events_union = union_no_overlap(events1, events2)
    pprint(events_union)
    dur = sum((e.duration for e in events_union), timedelta(0))
    assert dur == timedelta(hours=5, minutes=0)
    assert sorted(events_union, key=lambda e: e.timestamp)


def test_merge_subwatcher_fields_basic():
    """Fully overlapping subwatcher fields are injected without changing duration."""
    now = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    td1h = timedelta(hours=1)

    base = [
        Event(
            timestamp=now,
            duration=td1h,
            data={"app": "vim", "title": "file.py"},
        )
    ]
    sub = [
        Event(
            timestamp=now,
            duration=td1h,
            data={"project": "myproject", "file": "file.py", "language": "python"},
        )
    ]
    result = merge_subwatcher_fields(base, sub, ["project", "file", "language"])

    assert len(result) == 1
    # Original base fields preserved
    assert result[0].data["app"] == "vim"
    assert result[0].data["title"] == "file.py"
    # Subwatcher fields injected
    assert result[0].data["project"] == "myproject"
    assert result[0].data["language"] == "python"
    # Exact overlap means no extra segmentation
    assert result[0].timestamp == now
    assert result[0].duration == td1h


def test_merge_subwatcher_fields_partial_overlap_splits_base():
    """Partial overlap only enriches the covered slice of the base event."""
    now = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    td15m = timedelta(minutes=15)
    td30m = timedelta(minutes=30)
    td1h = timedelta(hours=1)

    base = [Event(timestamp=now, duration=td1h, data={"app": "vim"})]
    sub = [
        Event(
            timestamp=now + td15m,
            duration=td30m,
            data={"project": "myproject"},
        )
    ]

    result = merge_subwatcher_fields(base, sub, ["project"])

    assert len(result) == 3
    assert [event.duration for event in result] == [td15m, td30m, td15m]
    assert [event.timestamp for event in result] == [
        now,
        now + td15m,
        now + td15m + td30m,
    ]
    assert "project" not in result[0].data
    assert result[1].data["project"] == "myproject"
    assert "project" not in result[2].data
    assert sum_durations(result) == td1h


def test_merge_subwatcher_fields_overlapping_subwatchers_prefer_latest_start():
    """Newer overlapping subwatcher events take over from their own start time."""
    now = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    td20m = timedelta(minutes=20)
    td40m = timedelta(minutes=40)
    td1h = timedelta(hours=1)

    base = [Event(timestamp=now, duration=td1h, data={"app": "vim"})]
    sub = [
        Event(timestamp=now, duration=td40m, data={"project": "alpha"}),
        Event(timestamp=now + td20m, duration=td40m, data={"project": "beta"}),
    ]

    result = merge_subwatcher_fields(base, sub, ["project"])

    assert len(result) == 2
    assert [event.timestamp for event in result] == [now, now + td20m]
    assert [event.duration for event in result] == [td20m, td40m]
    assert [event.data.get("project") for event in result] == ["alpha", "beta"]

    by_project = {
        event.data.get("project"): event.duration
        for event in merge_events_by_keys(result, ["project"])
    }
    assert by_project["alpha"] == td20m
    assert by_project["beta"] == td40m
    assert sum_durations(result) == td1h


def test_merge_subwatcher_fields_sweep_preserves_long_lived_overlap():
    now = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    base = [
        Event(
            timestamp=now + timedelta(hours=2),
            duration=timedelta(hours=1),
            data={"app": "vim", "base": "second"},
        ),
        Event(
            timestamp=now,
            duration=timedelta(hours=1),
            data={"app": "vim", "base": "first"},
        ),
    ]
    sub = [
        Event(
            timestamp=now,
            duration=timedelta(hours=4),
            data={"project": "long-lived"},
        ),
        Event(
            timestamp=now + timedelta(minutes=30),
            duration=timedelta(minutes=15),
            data={"project": "newer"},
        ),
    ]

    result = merge_subwatcher_fields(base, sub, ["project"])

    assert result[0].timestamp == now + timedelta(hours=2)
    assert result[0].data["project"] == "long-lived"
    assert [event.data["project"] for event in result[1:]] == [
        "long-lived",
        "newer",
        "long-lived",
    ]
    assert sum_durations(result) == timedelta(hours=2)


def test_merge_subwatcher_fields_expiration_reveals_older_active_events():
    now = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    base = [
        Event(timestamp=now, duration=timedelta(minutes=60), data={"app": "vim"})
    ]
    sub = [
        Event(
            timestamp=now,
            duration=timedelta(minutes=60),
            data={"project": "oldest"},
        ),
        Event(
            timestamp=now + timedelta(minutes=10),
            duration=timedelta(minutes=20),
            data={"project": "middle"},
        ),
        Event(
            timestamp=now + timedelta(minutes=15),
            duration=timedelta(minutes=5),
            data={"project": "newest"},
        ),
    ]

    result = merge_subwatcher_fields(base, sub, ["project"])

    assert [event.data["project"] for event in result] == [
        "oldest",
        "middle",
        "newest",
        "middle",
        "oldest",
    ]
    assert [event.duration for event in result] == [
        timedelta(minutes=10),
        timedelta(minutes=5),
        timedelta(minutes=5),
        timedelta(minutes=10),
        timedelta(minutes=30),
    ]


def test_merge_subwatcher_fields_same_start_prefers_longest_end():
    now = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    base = [
        Event(timestamp=now, duration=timedelta(minutes=60), data={"app": "vim"})
    ]
    sub = [
        Event(
            timestamp=now,
            duration=timedelta(minutes=20),
            data={"project": "short"},
        ),
        Event(
            timestamp=now,
            duration=timedelta(minutes=40),
            data={"project": "long"},
        ),
    ]

    result = merge_subwatcher_fields(base, sub, ["project"])

    assert len(result) == 2
    assert result[0].data["project"] == "long"
    assert result[0].duration == timedelta(minutes=40)
    assert "project" not in result[1].data
    assert result[1].duration == timedelta(minutes=20)


def test_merge_subwatcher_fields_sweep_checks_each_overlap_once(monkeypatch):
    now = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    transition_count = 500
    base = [
        Event(
            timestamp=now,
            duration=timedelta(seconds=transition_count),
            data={"app": "vim"},
        )
    ]
    sub = [
        Event(
            timestamp=now + timedelta(seconds=i),
            duration=timedelta(seconds=1),
            data={"project": str(i)},
        )
        for i in range(transition_count)
    ]
    intersection_calls = 0
    original_intersection = Timeslot.intersection

    def counting_intersection(self, other):
        nonlocal intersection_calls
        intersection_calls += 1
        return original_intersection(self, other)

    monkeypatch.setattr(Timeslot, "intersection", counting_intersection)

    result = merge_subwatcher_fields(base, sub, ["project"])

    assert len(result) == transition_count
    assert intersection_calls <= transition_count + 1


def test_merge_subwatcher_fields_no_overlap():
    """Base events with no overlapping subwatcher event are returned unchanged."""
    now = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    td1h = timedelta(hours=1)

    base = [Event(timestamp=now, duration=td1h, data={"app": "vim"})]
    # Subwatcher event is entirely after the base event
    sub = [
        Event(
            timestamp=now + 2 * td1h,
            duration=td1h,
            data={"project": "other"},
        )
    ]
    result = merge_subwatcher_fields(base, sub, ["project"])

    assert len(result) == 1
    assert "project" not in result[0].data
    assert result[0].data["app"] == "vim"


def test_merge_subwatcher_fields_base_wins_conflict():
    """With conflict='base_wins' (default), existing base keys are not overwritten."""
    now = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    td1h = timedelta(hours=1)

    base = [Event(timestamp=now, duration=td1h, data={"app": "vim", "file": "base.py"})]
    sub = [Event(timestamp=now, duration=td1h, data={"file": "sub.py", "project": "p"})]

    result = merge_subwatcher_fields(
        base, sub, ["file", "project"], conflict="base_wins"
    )
    # base's "file" must not be overwritten
    assert result[0].data["file"] == "base.py"
    # "project" not in base → injected from sub
    assert result[0].data["project"] == "p"


def test_merge_subwatcher_fields_sub_wins_conflict():
    """With conflict='sub_wins', subwatcher fields overwrite base fields."""
    now = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    td1h = timedelta(hours=1)

    base = [Event(timestamp=now, duration=td1h, data={"app": "vim", "file": "base.py"})]
    sub = [Event(timestamp=now, duration=td1h, data={"file": "sub.py"})]

    result = merge_subwatcher_fields(base, sub, ["file"], conflict="sub_wins")
    assert result[0].data["file"] == "sub.py"


def test_merge_subwatcher_fields_multiple_subsegments_preserve_duration():
    """Repeated subwatcher values aggregate to their true covered duration."""
    now = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    td15m = timedelta(minutes=15)
    td1h = timedelta(hours=1)

    base = [Event(timestamp=now, duration=td1h, data={"app": "vim"})]
    sub = [
        Event(timestamp=now, duration=td15m, data={"project": "alpha"}),
        Event(timestamp=now + td15m, duration=td15m, data={"project": "beta"}),
        Event(
            timestamp=now + 2 * td15m,
            duration=td15m,
            data={"project": "alpha"},
        ),
    ]

    result = merge_subwatcher_fields(base, sub, ["project"])

    by_project = {
        event.data.get("project"): event.duration
        for event in merge_events_by_keys(result, ["project"])
    }
    by_app = {
        event.data.get("app"): event.duration
        for event in merge_events_by_keys(result, ["app"])
    }

    assert by_project["alpha"] == 2 * td15m
    assert by_project["beta"] == td15m
    assert by_project[None] == td15m
    assert by_app["vim"] == td1h
    assert sum_durations(result) == td1h


def test_merge_subwatcher_fields_empty_inputs():
    """Empty sub or keys returns a defensive copy of base (not the same list)."""
    now = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    td1h = timedelta(hours=1)
    base = [Event(timestamp=now, duration=td1h, data={"app": "vim"})]

    # Empty subwatcher list — returns a new list, data unchanged
    result = merge_subwatcher_fields(base, [], ["project"])
    assert result[0].data == {"app": "vim"}
    assert result is not base

    # Empty keys list
    sub = [Event(timestamp=now, duration=td1h, data={"project": "p"})]
    result = merge_subwatcher_fields(base, sub, [])
    assert "project" not in result[0].data
    assert result is not base

    # Both empty
    result = merge_subwatcher_fields(base, [], [])
    assert result[0].data == {"app": "vim"}
    assert result is not base


def test_merge_subwatcher_fields_invalid_conflict():
    """Invalid conflict value raises ValueError immediately."""
    now = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    td1h = timedelta(hours=1)
    base = [Event(timestamp=now, duration=td1h, data={"app": "vim"})]
    sub = [Event(timestamp=now, duration=td1h, data={"project": "p"})]

    with pytest.raises(ValueError, match="conflict must be"):
        merge_subwatcher_fields(base, sub, ["project"], conflict="invalid")


def test_merge_subwatcher_fields_namespaces_source_fields():
    now = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    base = [
        Event(
            timestamp=now,
            duration=timedelta(minutes=10),
            data={"app": "devenv.exe", "title": "ActivityWatch"},
        )
    ]
    sub = [
        Event(
            timestamp=now + timedelta(minutes=3),
            duration=timedelta(minutes=4),
            data={"vdesktop": "Personal"},
        )
    ]

    result = merge_subwatcher_fields(
        base, sub, ["vdesktop"], source_id="vdesktop"
    )

    assert [event.duration for event in result] == [
        timedelta(minutes=3),
        timedelta(minutes=4),
        timedelta(minutes=3),
    ]
    assert "$source.vdesktop.vdesktop" not in result[0].data
    assert result[1].data["$source.vdesktop.vdesktop"] == "Personal"
    assert "$source.vdesktop.vdesktop" not in result[2].data
    assert sum((event.duration for event in result), timedelta()) == timedelta(
        minutes=10
    )


def test_merge_subwatcher_fields_rejects_invalid_source_id():
    with pytest.raises(ValueError, match="source_id"):
        merge_subwatcher_fields([], [], [], source_id="invalid.source")
