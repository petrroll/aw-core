import logging
import re
from copy import deepcopy
from typing import List

from aw_core.models import Event

logger = logging.getLogger(__name__)


def filter_keyvals(
    events: List[Event], key: str, vals: List[str], exclude=False
) -> List[Event]:
    def predicate(event):
        return key in event.data and event.data[key] in vals

    if exclude:
        return [deepcopy(event) for event in events if not predicate(event)]
    return [deepcopy(event) for event in events if predicate(event)]


def filter_keyvals_regex(events: List[Event], key: str, regex: str) -> List[Event]:
    r = re.compile(regex)

    def predicate(event):
        return key in event.data and bool(r.findall(event.data[key]))

    return [deepcopy(event) for event in events if predicate(event)]
