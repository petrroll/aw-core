"""
Originally from aw-research
"""

from bisect import bisect_right
from copy import deepcopy
from datetime import datetime
from typing import List, Tuple

from aw_core import Event


def union_no_overlap(events1: List[Event], events2: List[Event]) -> List[Event]:
    """Combine streams while giving every first-stream interval precedence.

    Lower-priority records are split around the complete union of first-stream
    coverage. Inputs and their events are never modified.
    """
    higher_priority = deepcopy(events1)

    coverage: List[Tuple[datetime, datetime]] = []
    for event in sorted(higher_priority, key=lambda item: item.timestamp):
        start = event.timestamp
        end = event.timestamp + event.duration
        if end <= start:
            continue
        if coverage and start <= coverage[-1][1]:
            coverage[-1] = (coverage[-1][0], max(coverage[-1][1], end))
        else:
            coverage.append((start, end))
    coverage_ends = [end for _, end in coverage]

    remainder: List[Event] = []
    for event in events2:
        start = event.timestamp
        end = event.timestamp + event.duration
        if end <= start:
            remainder.append(deepcopy(event))
            continue

        cursor = start
        coverage_index = bisect_right(coverage_ends, cursor)
        for coverage_index in range(coverage_index, len(coverage)):
            covered_start, covered_end = coverage[coverage_index]
            if covered_start >= end:
                break
            if covered_end <= cursor:
                continue
            if cursor < covered_start:
                segment = deepcopy(event)
                segment.timestamp = cursor
                segment.duration = min(covered_start, end) - cursor
                remainder.append(segment)
            cursor = max(cursor, covered_end)
            if cursor >= end:
                break
        if cursor < end:
            segment = deepcopy(event)
            segment.timestamp = cursor
            segment.duration = end - cursor
            remainder.append(segment)

    return sorted(higher_priority + remainder, key=lambda event: event.timestamp)
