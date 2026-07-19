import logging
import re
from heapq import heappop, heappush
from copy import deepcopy
from typing import List, Optional, Tuple

from aw_core.models import Event
from timeslot import Timeslot

from .filter_period_intersect import _get_event_period

logger = logging.getLogger(__name__)


class _ActiveSubwatcher:
    def __init__(
        self, event: Event, period: Timeslot, input_order: int
    ) -> None:
        self.event = event
        self.period = period
        self.input_order = input_order

    def __lt__(self, other: "_ActiveSubwatcher") -> bool:
        if self.period.start != other.period.start:
            return self.period.start > other.period.start
        if self.period.end != other.period.end:
            return self.period.end > other.period.end
        return self.input_order < other.input_order


def merge_subwatcher_fields(
    base_events: List[Event],
    subwatcher_events: List[Event],
    keys: List[str],
    conflict: str = "base_wins",
    source_id: Optional[str] = None,
) -> List[Event]:
    """
    Split each event in *base_events* on overlapping subwatcher boundaries and
    copy the named *keys* from the matching subwatcher event into each segment's
    ``data`` dict.

    Unlike the ``concat`` workaround, this does not fabricate extra duration.
    App/title/duration aggregations stay correct because the output still covers
    exactly the same total time as *base_events*; only the segmentation changes
    where subwatcher fields actually change.

    This is the backend primitive that lets every client (webui, native UIs,
    exporters) categorize by subwatcher fields (browser ``url``/``$domain``;
    editor ``project``/``file``/``language``) without bespoke per-watcher
    client-side code.

    Args:
        base_events: The resolved activity stream to enrich.
        subwatcher_events: Events from a subwatcher bucket (e.g. aw-watcher-vim,
            aw-watcher-web).  Should already be clipped via
            ``filter_period_intersect`` before passing here.
        keys: Which keys to copy from the subwatcher event into the base event.
            Keys already present in the base event are left untouched when
            ``conflict="base_wins"`` (default).
        conflict: ``"base_wins"`` (default) — base event's existing keys are
            never overwritten; subwatcher fields are purely additive.
            ``"sub_wins"`` — subwatcher fields overwrite base fields.
        source_id: Optional stable source identifier. When set, copied fields are
            namespaced as ``$source.<source_id>.<key>`` to prevent collisions
            between window, browser, editor, and custom watcher fields.

    Returns:
        A new list of base event segments with subwatcher fields injected.
        Events in *base_events* that have no overlapping subwatcher event are
        returned with their original data unchanged.

    Example::

        window_events = query_bucket(bid_window)
        editor_events = flood(query_bucket(bid_editor))
        editor_events = filter_period_intersect(editor_events, window_events)
        window_events = merge_subwatcher_fields(
            window_events, editor_events, ["project", "file", "language"]
        )
        # Now categorize(window_events, ...) can match on "project"/"file"

    Note on overlap:
        Base events are split at the clipped subwatcher boundaries so each
        output segment only carries the subwatcher fields that were actually
        present during that slice of time. If multiple subwatcher events cover
        the same slice, the most recent one wins so a later transition does not
        get smeared backward by an older, longer pulse.
    """
    if conflict not in ("base_wins", "sub_wins"):
        raise ValueError(
            f"conflict must be 'base_wins' or 'sub_wins', got {conflict!r}"
        )
    if source_id is not None and not re.fullmatch(r"[A-Za-z0-9_-]+", source_id):
        raise ValueError(
            "source_id may only contain letters, numbers, '_' and '-'"
        )
    if not subwatcher_events or not keys:
        return [deepcopy(e) for e in base_events]

    sub_sorted = sorted(
        (
            (sub, _get_event_period(sub), input_order)
            for input_order, sub in enumerate(subwatcher_events)
        ),
        key=lambda item: item[1].start,
    )
    indexed_bases = sorted(
        enumerate(base_events), key=lambda item: item[1].timestamp
    )
    segments_by_index: List[List[Event]] = [[] for _ in base_events]
    active: List[Tuple[Event, Timeslot, int]] = []
    sub_cursor = 0

    for base_index, base in indexed_bases:
        base_period = _get_event_period(base)
        active = [
            (sub, sub_period, input_order)
            for sub, sub_period, input_order in active
            if sub_period.end > base_period.start
        ]
        while (
            sub_cursor < len(sub_sorted)
            and sub_sorted[sub_cursor][1].start < base_period.end
        ):
            sub, sub_period, input_order = sub_sorted[sub_cursor]
            if sub_period.end > base_period.start:
                active.append((sub, sub_period, input_order))
            sub_cursor += 1

        overlapping = [
            (sub, sub_period, input_order)
            for sub, sub_period, input_order in active
            if sub_period.start < base_period.end
        ]
        boundaries = {base_period.start, base_period.end}
        starts = {}
        for sub, sub_period, input_order in overlapping:
            ip = base_period.intersection(sub_period)
            if ip:
                boundaries.add(ip.start)
                boundaries.add(ip.end)
                starts.setdefault(ip.start, []).append(
                    _ActiveSubwatcher(sub, sub_period, input_order)
                )

        if not overlapping:
            segments_by_index[base_index].append(deepcopy(base))
            continue

        boundary_points = sorted(boundaries)
        base_segments: List[Event] = []
        active_heap: List[_ActiveSubwatcher] = []
        for start, end in zip(boundary_points, boundary_points[1:]):
            for item in starts.get(start, []):
                heappush(active_heap, item)
            while active_heap and active_heap[0].period.end <= start:
                heappop(active_heap)
            best_sub = active_heap[0].event if active_heap else None

            enriched = deepcopy(base)
            enriched.timestamp = start
            enriched.duration = end - start

            if best_sub is not None:
                for key in keys:
                    if key in best_sub.data:
                        target_key = (
                            f"$source.{source_id}.{key}" if source_id else key
                        )
                        if conflict == "base_wins" and target_key in enriched.data:
                            continue
                        enriched.data[target_key] = deepcopy(best_sub.data[key])

            if (
                base_segments
                and base_segments[-1].timestamp + base_segments[-1].duration
                == enriched.timestamp
                and base_segments[-1].data == enriched.data
            ):
                base_segments[-1].duration += enriched.duration
            else:
                base_segments.append(enriched)

        segments_by_index[base_index].extend(base_segments)

    return [
        segment
        for base_segments in segments_by_index
        for segment in base_segments
    ]
