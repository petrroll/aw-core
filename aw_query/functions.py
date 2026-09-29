import math
import re
from datetime import timedelta
from functools import wraps
from inspect import signature
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
)

import iso8601
from aw_core.models import Event
from aw_datastore import Datastore
from aw_transform import (
    Rule,
    active_periods_v2,
    categorize,
    categorize_v2,
    categorize_v2_explain,
    chunk_events_by_key,
    concat,
    filter_keyvals,
    filter_keyvals_regex,
    filter_period_intersect,
    flood,
    flood_v2,
    limit_events,
    merge_events_by_keys,
    merge_subwatcher_fields,
    map_event_fields,
    period_union,
    simplify_string,
    sort_by_duration,
    sort_by_timestamp,
    split_url_events,
    sum_durations,
    tag,
    union_no_overlap,
)

from .exceptions import QueryFunctionException


def _verify_bucket_exists(datastore, bucketname):
    if bucketname in datastore.buckets():
        return
    else:
        raise QueryFunctionException(f"There's no bucket named '{bucketname}'")


def _verify_variable_is_type(variable, t):
    if not isinstance(variable, t):
        raise QueryFunctionException(
            f"Variable '{variable}' passed to function call is of invalid type. Expected {t} but was {type(variable)}"
        )


# TODO: proper type checking (typecheck-decorator in pypi?)


TNamespace = Dict[str, Any]
TQueryFunction = Callable[..., Any]


"""
    Declarations
"""
functions: Dict[str, TQueryFunction] = {}


def q2_function(transform_func=None):
    """
    Decorator used to register query functions.

    Automatically adds mock arguments for Datastore and TNamespace
    if not in function signature.
    """

    def h(f):
        sig = signature(f)
        # If function lacks docstring, use docstring from underlying function in aw_transform
        if transform_func and transform_func.__doc__ and not f.__doc__:
            f.__doc__ = f".. note:: Documentation automatically copied from underlying function `aw_transform.{transform_func.__name__}`\n\n{transform_func.__doc__}"

        @wraps(f)
        def g(datastore: Datastore, namespace: TNamespace, *args, **kwargs):
            # Remove datastore and namespace argument for functions that don't need it
            args = (datastore, namespace, *args)
            if TNamespace not in (sig.parameters[p].annotation for p in sig.parameters):
                args = (args[0], *args[2:])
            if Datastore not in (sig.parameters[p].annotation for p in sig.parameters):
                args = args[1:]
            return f(*args, **kwargs)

        fname = f.__name__
        if fname[:3] == "q2_":
            fname = fname[3:]
        functions[fname] = g
        return g

    return h


def q2_typecheck(f):
    """Decorator that typechecks using `_verify_variable_is_type`"""
    sig = signature(f)

    @wraps(f)
    def g(*args, **kwargs):
        # FIXME: If the first argument passed to a query2 function is a straight [] then the second argument disappears from the argument list for unknown reasons, which breaks things
        for i, p in enumerate(sig.parameters):
            param = sig.parameters[p]

            # print(f"Checking that param ({param}) was {param.annotation}, value: {args[i]}")
            # FIXME: Won't check keyword arguments
            if (
                param.annotation in [list, str, int, float]
                and param.default == param.empty
            ):
                _verify_variable_is_type(args[i], param.annotation)

        return f(*args, **kwargs)

    return g


"""
    Getting buckets
"""


@q2_function()
@q2_typecheck
def q2_find_bucket(
    datastore: Datastore, filter_str: str, hostname: Optional[str] = None
):
    """Find bucket by using a filter_str (to avoid hardcoding bucket names)"""
    for bucket in datastore.buckets():
        if filter_str in bucket:
            bucket_metadata = datastore[bucket].metadata()
            if hostname:
                if bucket_metadata["hostname"] == hostname:
                    return bucket
            else:
                return bucket
    raise QueryFunctionException(
        f"Unable to find bucket matching '{filter_str}' (hostname filter set to '{hostname}')"
    )


"""
    Data gathering functions
"""


@q2_function()
@q2_typecheck
def q2_query_bucket(
    datastore: Datastore, namespace: TNamespace, bucketname: str
) -> List[Event]:
    """Fetch events with the legacy Query2 clipping and ordering contract."""
    _verify_bucket_exists(datastore, bucketname)
    try:
        starttime = iso8601.parse_date(namespace["STARTTIME"])
        endtime = iso8601.parse_date(namespace["ENDTIME"])
    except iso8601.ParseError:
        raise QueryFunctionException(
            "Unable to parse starttime/endtime for query_bucket"
        ) from None
    return datastore[bucketname].get(starttime=starttime, endtime=endtime)


@q2_function()
@q2_typecheck
def q2_query_bucket_optional(
    datastore: Datastore,
    namespace: TNamespace,
    bucketname: str,
    expected_hostname: Optional[str] = None,
) -> List[Event]:
    """Optionally fetch events using the legacy clipped bucket read."""
    buckets = datastore.buckets()
    if bucketname not in buckets:
        return []
    if expected_hostname and buckets[bucketname].get("hostname") != expected_hostname:
        return []
    return q2_query_bucket(datastore, namespace, bucketname)


@q2_function()
@q2_typecheck
def q2_query_bucket_optional_raw(
    datastore: Datastore,
    namespace: TNamespace,
    bucketname: str,
    expected_hostname: Optional[str] = None,
    padding_seconds: float = 0,
) -> List[Event]:
    """Fetch overlapping source records without clipping their stored bounds.

    ``padding_seconds`` expands only the datastore lookup interval on both
    sides. It accepts finite values from 0 through 3600 seconds, at microsecond
    precision. The original query interval and returned event bounds are never
    changed. Passing ``null`` for ``expected_hostname`` allows callers to use
    padding without applying a hostname check.
    """
    if expected_hostname is not None and not isinstance(expected_hostname, str):
        raise QueryFunctionException(
            "query_bucket_optional_raw expected_hostname must be a string or null"
        )
    if (
        isinstance(padding_seconds, bool)
        or not isinstance(padding_seconds, (int, float))
        or padding_seconds < 0
        or padding_seconds > 3600
        or not math.isfinite(padding_seconds)
    ):
        raise QueryFunctionException(
            "query_bucket_optional_raw padding_seconds must be a finite number "
            "between 0 and 3600"
        )
    # Round half-up once to the common Python/Rust microsecond precision.
    padding_microseconds = math.floor(padding_seconds * 1_000_000 + 0.5)
    padding = timedelta(microseconds=padding_microseconds)

    try:
        starttime = iso8601.parse_date(namespace["STARTTIME"])
        endtime = iso8601.parse_date(namespace["ENDTIME"])
    except iso8601.ParseError:
        raise QueryFunctionException(
            "Unable to parse starttime/endtime for query_bucket_optional_raw"
        ) from None
    # Padding must not turn an empty/reversed caller interval into a data load.
    if endtime <= starttime:
        return []

    buckets = datastore.buckets()
    if bucketname not in buckets:
        return []
    if (
        expected_hostname is not None
        and buckets[bucketname].get("hostname") != expected_hostname
    ):
        return []

    try:
        lookup_start = starttime - padding
        lookup_end = endtime + padding
    except OverflowError:
        raise QueryFunctionException(
            "query_bucket_optional_raw padding exceeds the supported datetime range"
        ) from None
    events = datastore[bucketname].get_unclipped(
        starttime=lookup_start, endtime=lookup_end
    )
    overlapping = [
        event
        for event in events
        if event.timestamp < lookup_end
        and event.timestamp + event.duration > lookup_start
    ]
    insertion_order = {id(event): index for index, event in enumerate(overlapping)}
    return sorted(
        overlapping,
        key=lambda event: (
            event.timestamp,
            event.timestamp + event.duration,
            event.id if event.id is not None else insertion_order[id(event)],
        ),
    )


@q2_function()
def q2_query_period(namespace: TNamespace) -> List[Event]:
    """Return a blank event exactly bounding the non-empty query interval."""
    try:
        starttime = iso8601.parse_date(namespace["STARTTIME"])
        endtime = iso8601.parse_date(namespace["ENDTIME"])
    except iso8601.ParseError:
        raise QueryFunctionException(
            "Unable to parse starttime/endtime for query_period"
        ) from None
    if endtime <= starttime:
        return []
    return [Event(timestamp=starttime, duration=endtime - starttime, data={})]


@q2_function()
@q2_typecheck
def q2_query_bucket_eventcount(
    datastore: Datastore, namespace: TNamespace, bucketname: str
) -> int:
    _verify_bucket_exists(datastore, bucketname)
    starttime = iso8601.parse_date(namespace["STARTTIME"])
    endtime = iso8601.parse_date(namespace["ENDTIME"])
    return datastore[bucketname].get_eventcount(starttime=starttime, endtime=endtime)


"""
    Filtering functions
"""


@q2_function(filter_keyvals)
@q2_typecheck
def q2_filter_keyvals(events: list, key: str, vals: list) -> List[Event]:
    return filter_keyvals(events, key, vals, False)


@q2_function(filter_keyvals)
@q2_typecheck
def q2_exclude_keyvals(events: list, key: str, vals: list) -> List[Event]:
    return filter_keyvals(events, key, vals, True)


@q2_function(filter_keyvals_regex)
@q2_typecheck
def q2_filter_keyvals_regex(events: list, key: str, regex: str) -> List[Event]:
    return filter_keyvals_regex(events, key, regex)


@q2_function(filter_period_intersect)
@q2_typecheck
def q2_filter_period_intersect(events: list, filterevents: list) -> List[Event]:
    return filter_period_intersect(events, filterevents)


@q2_function(period_union)
@q2_typecheck
def q2_period_union(events1: list, events2: list) -> List[Event]:
    return period_union(events1, events2)


@q2_function(limit_events)
@q2_typecheck
def q2_limit_events(events: list, count: int) -> List[Event]:
    return limit_events(events, count)


"""
    Merge functions
"""

_MISSING_SOURCE_ID = object()


@q2_function(merge_events_by_keys)
@q2_typecheck
def q2_merge_events_by_keys(events: list, keys: list) -> List[Event]:
    return merge_events_by_keys(events, keys)


@q2_function(merge_subwatcher_fields)
@q2_typecheck
def q2_merge_subwatcher_fields(
    base_events: list,
    subwatcher_events: list,
    keys: list,
    conflict: str = "base_wins",
    source_id: Any = _MISSING_SOURCE_ID,
) -> List[Event]:
    try:
        if isinstance(conflict, dict):
            options = conflict
            conflict = options.get("conflict", "base_wins")
            if source_id is not _MISSING_SOURCE_ID and "source_id" in options:
                raise QueryFunctionException(
                    "source_id must be provided either in options or as the fifth argument, not both"
                )
            if source_id is _MISSING_SOURCE_ID:
                source_id = options.get("source_id")
        elif source_id is _MISSING_SOURCE_ID:
            source_id = None
        if source_id is not None and not isinstance(source_id, str):
            raise QueryFunctionException("source_id must be a string")
        return merge_subwatcher_fields(
            base_events, subwatcher_events, keys, conflict, source_id
        )
    except ValueError as exc:
        raise QueryFunctionException(str(exc)) from None


@q2_function(map_event_fields)
@q2_typecheck
def q2_map_event_fields(events: list, mappings: dict) -> List[Event]:
    try:
        return map_event_fields(events, mappings)
    except ValueError as exc:
        raise QueryFunctionException(str(exc)) from None


@q2_function(chunk_events_by_key)
@q2_typecheck
def q2_chunk_events_by_key(events: list, key: str) -> List[Event]:
    return chunk_events_by_key(events, key)


"""
    Sort functions
"""


@q2_function(sort_by_timestamp)
@q2_typecheck
def q2_sort_by_timestamp(events: list) -> List[Event]:
    return sort_by_timestamp(events)


@q2_function(sort_by_duration)
@q2_typecheck
def q2_sort_by_duration(events: list) -> List[Event]:
    return sort_by_duration(events)


"""
    Summarizing functions
"""


@q2_function(sum_durations)
@q2_typecheck
def q2_sum_durations(events: list) -> timedelta:
    return sum_durations(events)


@q2_function(concat)
@q2_typecheck
def q2_concat(events1: list, events2: list) -> List[Event]:
    return concat(events1, events2)


@q2_function(union_no_overlap)
@q2_typecheck
def q2_union_no_overlap(events1: list, events2: list) -> List[Event]:
    return union_no_overlap(events1, events2)


"""
    Flood functions
"""


@q2_function(flood)
@q2_typecheck
def q2_flood(events: list) -> List[Event]:
    """Apply the legacy Query2 heartbeat transform."""
    return flood(events)


@q2_function(flood_v2)
@q2_typecheck
def q2_flood_v2(events: list, threshold_seconds: float = 5) -> List[Event]:
    try:
        return flood_v2(events, threshold_seconds)
    except ValueError as exc:
        raise QueryFunctionException(str(exc)) from None


"""
    Watcher specific functions
"""


@q2_function(split_url_events)
@q2_typecheck
def q2_split_url_events(events: list) -> List[Event]:
    return split_url_events(events)


@q2_function(simplify_string)
@q2_typecheck
def q2_simplify_window_titles(events: list, key: str) -> List[Event]:
    return simplify_string(events, key=key)


"""
    Test functions
"""


@q2_function()
@q2_typecheck
def q2_nop():
    """No operation function for unittesting"""
    return 1


"""
    Classify
"""


def _compile_legacy_rules(classes: list) -> List[Tuple[Any, Rule]]:
    try:
        return [(_cls, Rule(rule_dict)) for _cls, rule_dict in classes]
    except (re.error, TypeError, ValueError) as exc:
        raise QueryFunctionException(str(exc)) from None


@q2_function(categorize)
@q2_typecheck
def q2_categorize(events: list, classes: list):
    return categorize(events, _compile_legacy_rules(classes))


@q2_function(categorize_v2)
@q2_typecheck
def q2_categorize_v2(
    events: list, category_specs: list, host: Optional[str] = None
):
    try:
        return categorize_v2(events, category_specs, host)
    except (TypeError, ValueError) as exc:
        raise QueryFunctionException(str(exc)) from None


@q2_function(categorize_v2_explain)
@q2_typecheck
def q2_categorize_v2_explain(
    events: list, category_specs: list, host: Optional[str] = None
):
    try:
        return categorize_v2_explain(events, category_specs, host)
    except (TypeError, ValueError) as exc:
        raise QueryFunctionException(str(exc)) from None


@q2_function(active_periods_v2)
@q2_typecheck
def q2_active_periods_v2(
    named_sources: list,
    expression_spec: dict,
    host: Optional[str] = None,
):
    try:
        return active_periods_v2(named_sources, expression_spec, host)
    except (TypeError, ValueError) as exc:
        raise QueryFunctionException(str(exc)) from None


@q2_function(tag)
@q2_typecheck
def q2_tag(events: list, classes: list):
    """Run the legacy Query2 tag API with client-safe input errors."""
    return tag(events, _compile_legacy_rules(classes))
