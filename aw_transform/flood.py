import logging
import math
from datetime import timedelta
from copy import deepcopy
from typing import List

from aw_core.models import Event

logger = logging.getLogger(__name__)


def flood_v2(events: List[Event], threshold_seconds: float = 5) -> List[Event]:
    """Fill short positive heartbeat gaps deterministically and locally.

    Gaps are the uncovered intervals between connected coverage components of
    the immutable original records. The boundary facts are selected from those
    originals: furthest end (latest start, then later input on a tie) on the
    left, and earliest start (longest end, then later input on a tie) on the
    right. The longer original boundary fact
    supplies a separate synthetic event for the whole gap; equal durations favor
    the left. Original timestamps and durations are never changed, overlapping
    originals are retained, and one fill never influences a later decision or
    overlap precedence.
    """
    if (
        isinstance(threshold_seconds, bool)
        or not isinstance(threshold_seconds, (int, float))
        or threshold_seconds < 0
        or threshold_seconds > (2**63 - 1) / 1_000_000
        or not math.isfinite(threshold_seconds)
    ):
        raise ValueError(
            "flood_v2 threshold_seconds must be a finite non-negative number"
        )

    originals = sorted(deepcopy(events), key=lambda event: event.timestamp)
    if len(originals) < 2:
        return originals

    # Query2's common precision is microseconds; truncation matches Rust.
    threshold = timedelta(microseconds=int(threshold_seconds * 1_000_000))

    # Each tuple is (coverage start, coverage end, left anchor, right anchor).
    # Anchor indexes always refer to immutable ``originals``.
    first_end = originals[0].timestamp + originals[0].duration
    components = []
    component_start = originals[0].timestamp
    component_end = first_end
    left_anchor = 0
    right_anchor = 0
    for index, event in enumerate(originals[1:], start=1):
        event_end = event.timestamp + event.duration
        if event.timestamp <= component_end:
            if event.timestamp == component_start:
                right_end = (
                    originals[right_anchor].timestamp
                    + originals[right_anchor].duration
                )
                if event_end >= right_end:
                    right_anchor = index
            if event_end > component_end:
                component_end = event_end
                left_anchor = index
            elif (
                event_end == component_end
                and event.timestamp >= originals[left_anchor].timestamp
            ):
                left_anchor = index
            continue

        components.append(
            (component_start, component_end, left_anchor, right_anchor)
        )
        component_start = event.timestamp
        component_end = event_end
        left_anchor = index
        right_anchor = index
    components.append((component_start, component_end, left_anchor, right_anchor))

    flooded = deepcopy(originals)
    for left_component, right_component in zip(components[:-1], components[1:]):
        gap_start = left_component[1]
        gap_end = right_component[0]
        gap = gap_end - gap_start
        if gap <= timedelta(0) or gap > threshold:
            continue
        left_index = left_component[2]
        right_index = right_component[3]
        anchor_index = (
            left_index
            if originals[left_index].duration >= originals[right_index].duration
            else right_index
        )
        synthetic = deepcopy(originals[anchor_index])
        synthetic.id = None
        synthetic.timestamp = gap_start
        synthetic.duration = gap
        flooded.append(synthetic)

    return sorted(flooded, key=lambda event: event.timestamp)


def flood(events: List[Event], pulsetime: float = 5) -> List[Event]:
    """
    Takes a list of events and "floods" any empty space between events by extending one of the surrounding events to cover the empty space.

    For more details on flooding, see this issue:
     - https://github.com/ActivityWatch/activitywatch/issues/124
    """
    # Originally written in aw-research: https://github.com/ActivityWatch/aw-analysis/blob/7da1f2cd8552f866f643501de633d74cdecab168/aw_analysis/flood.py
    # NOTE: This algorithm has a lot of smaller details that need to be
    #       carefully considered by anyone wishing to edit it, see:
    #        - https://github.com/ActivityWatch/aw-core/pull/73

    events = deepcopy(events)
    events = sorted(events, key=lambda e: e.timestamp)

    # If negative gaps are smaller than this, prune them to become zero
    negative_gap_trim_thres = timedelta(seconds=0.1)

    warned_about_negative_gap_safe = False
    warned_about_negative_gap_unsafe = False

    for e1, e2 in zip(events[:-1], events[1:]):
        gap = e2.timestamp - (e1.timestamp + e1.duration)

        if not gap:
            continue

        # Sanity check in case events overlap
        if gap < timedelta(0) and e1.data == e2.data:
            # Events with negative gap but same data can safely be merged
            start = min(e1.timestamp, e2.timestamp)
            end = max(e1.timestamp + e1.duration, e2.timestamp + e2.duration)
            e1.timestamp, e1.duration = start, (end - start)
            e2.timestamp, e2.duration = end, timedelta(0)
            if not warned_about_negative_gap_safe:
                logger.warning(
                    f"Gap was of negative duration but could be safely merged ({gap.total_seconds()}s). This message will only show once per batch."
                )
                warned_about_negative_gap_safe = True
        elif gap < -negative_gap_trim_thres and not warned_about_negative_gap_unsafe:
            # Events with negative gap but differing data cannot be merged safely
            logger.warning(
                f"Gap was of negative duration and could NOT be safely merged ({gap.total_seconds()}s). This warning will only show once per batch."
            )
            warned_about_negative_gap_unsafe = True
            # logger.warning("Event 1 (id {}): {} {}".format(e1.id, e1.timestamp, e1.duration))
            # logger.warning("Event 2 (id {}): {} {}".format(e2.id, e2.timestamp, e2.duration))
        elif -negative_gap_trim_thres < gap <= timedelta(seconds=pulsetime):
            e2_end = e2.timestamp + e2.duration

            # Prioritize flooding from the longer event
            if e1.duration >= e2.duration:
                if e1.data == e2.data:
                    # Extend e1 to the end of e2
                    # Set duration of e2 to zero (mark to delete)
                    e1.duration = e2_end - e1.timestamp
                    e2.timestamp = e2_end
                    e2.duration = timedelta(0)
                else:
                    # Extend e1 to the start of e2
                    e1.duration = e2.timestamp - e1.timestamp
            else:
                if e1.data == e2.data:
                    # Extend e2 to the start of e1, discard e1
                    e2.timestamp = e1.timestamp
                    e2.duration = e2_end - e2.timestamp
                    e1.duration = timedelta(0)
                else:
                    # Extend e2 backwards to end of e1
                    e2.timestamp = e1.timestamp + e1.duration
                    e2.duration = e2_end - e2.timestamp

    # Filter out remaining zero-duration events
    events = [e for e in events if e.duration > timedelta(0)]

    return events
