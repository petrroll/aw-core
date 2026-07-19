from typing import Dict, List

from aw_core import Event


def map_event_fields(events: List[Event], mappings: Dict[str, str]) -> List[Event]:
    if not isinstance(mappings, dict) or not all(
        isinstance(target, str)
        and target
        and isinstance(source, str)
        and source
        for target, source in mappings.items()
    ):
        raise ValueError("field mappings must map non-empty target names to source names")

    for event in events:
        original_data = event.data.copy()
        for target, source in mappings.items():
            if source in original_data:
                event.data[target] = original_data[source]
    return events
