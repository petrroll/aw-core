from copy import deepcopy
from typing import Any, Dict, List, Optional

from aw_core import Event

from .filter_period_intersect import filter_period_intersect, period_union
from .merge_subwatcher_fields import resolve_source_facts
from .rule_engine import (
    MAX_EXPRESSION_DEPTH,
    MAX_EXPRESSION_NODES,
    MAX_RULE_SOURCES,
    RegexExpression,
)


class ActivePeriodExpression:
    def evaluate(
        self, sources: Dict[str, List[Event]], host: Optional[str]
    ) -> List[Event]:
        raise NotImplementedError


class NonePeriodExpression(ActivePeriodExpression):
    def evaluate(
        self, sources: Dict[str, List[Event]], host: Optional[str]
    ) -> List[Event]:
        return []


class RegexPeriodExpression(ActivePeriodExpression):
    def __init__(self, spec: Dict[str, Any]) -> None:
        source = spec.get("source")
        if not isinstance(source, str) or not source:
            raise ValueError("active regex rule requires a non-empty string 'source'")
        self.source = source
        predicate_spec = dict(spec)
        predicate_spec.pop("source", None)
        self.host = predicate_spec.pop("host", None)
        if self.host is not None and (
            not isinstance(self.host, str) or not self.host
        ):
            raise ValueError("active regex rule 'host' must be a non-empty string")
        self.predicate = RegexExpression(predicate_spec)

    def evaluate(
        self, sources: Dict[str, List[Event]], host: Optional[str]
    ) -> List[Event]:
        if self.host is not None and self.host != host:
            return []
        if self.source not in sources:
            raise ValueError(f"active regex rule references unknown source {self.source!r}")
        return [
            deepcopy(event)
            for event in sources[self.source]
            if self.predicate.match(event).matched
        ]


class GroupPeriodExpression(ActivePeriodExpression):
    def __init__(self, expressions: List[ActivePeriodExpression]) -> None:
        if not expressions:
            raise ValueError("active rule group must contain at least one child rule")
        self.expressions = expressions


class AllPeriodExpression(GroupPeriodExpression):
    def evaluate(
        self, sources: Dict[str, List[Event]], host: Optional[str]
    ) -> List[Event]:
        result = self.expressions[0].evaluate(sources, host)
        for expression in self.expressions[1:]:
            result = filter_period_intersect(
                result, expression.evaluate(sources, host)
            )
        return result


class AnyPeriodExpression(GroupPeriodExpression):
    def evaluate(
        self, sources: Dict[str, List[Event]], host: Optional[str]
    ) -> List[Event]:
        result: List[Event] = []
        for expression in self.expressions:
            result = period_union(result, expression.evaluate(sources, host))
        return result


def compile_active_period_expression(
    spec: Dict[str, Any],
    *,
    _depth: int = 1,
    _state: Optional[Dict[str, Any]] = None,
) -> ActivePeriodExpression:
    if _depth > MAX_EXPRESSION_DEPTH:
        raise ValueError(
            f"active rule expression exceeds maximum depth of {MAX_EXPRESSION_DEPTH}"
        )
    state = _state if _state is not None else {"nodes": 0, "sources": set()}
    state["nodes"] += 1
    if state["nodes"] > MAX_EXPRESSION_NODES:
        raise ValueError(
            f"active rule expression exceeds maximum node count of {MAX_EXPRESSION_NODES}"
        )
    if not isinstance(spec, dict):
        raise ValueError("active rule expression must be an object")

    rule_type = spec.get("type")
    if rule_type is None and "regex" in spec:
        rule_type = "regex"
    if rule_type == "none":
        return NonePeriodExpression()
    if rule_type == "regex":
        expression = RegexPeriodExpression(spec)
        state["sources"].add(expression.source)
        if len(state["sources"]) > MAX_RULE_SOURCES:
            raise ValueError(
                f"active rule expressions exceed maximum source count of {MAX_RULE_SOURCES}"
            )
        return expression
    if rule_type in ("all", "any"):
        children = spec.get("rules", spec.get("children"))
        if not isinstance(children, list):
            raise ValueError(f"active {rule_type} rule requires a list of child 'rules'")
        expressions = [
            compile_active_period_expression(
                child, _depth=_depth + 1, _state=state
            )
            for child in children
        ]
        return (
            AllPeriodExpression(expressions)
            if rule_type == "all"
            else AnyPeriodExpression(expressions)
        )
    raise ValueError(f"unknown active rule expression type: {rule_type!r}")


def active_periods_v2(
    named_sources: List[List[Any]],
    expression_spec: Dict[str, Any],
    host: Optional[str] = None,
) -> List[Event]:
    if not isinstance(named_sources, list):
        raise ValueError("active period sources must be a list")
    if len(named_sources) > MAX_RULE_SOURCES:
        raise ValueError(
            f"active period sources exceed maximum count of {MAX_RULE_SOURCES}"
        )
    if host is not None and (not isinstance(host, str) or not host):
        raise ValueError("active period host must be a non-empty string")

    sources: Dict[str, List[Event]] = {}
    for index, entry in enumerate(named_sources):
        if not isinstance(entry, list) or len(entry) != 2:
            raise ValueError(
                f"active period source at index {index} must be [source_id, events]"
            )
        source_id, events = entry
        if not isinstance(source_id, str) or not source_id:
            raise ValueError(
                f"active period source at index {index} has an invalid source_id"
            )
        if source_id in sources:
            raise ValueError(f"duplicate active period source id: {source_id!r}")
        if not isinstance(events, list) or not all(
            isinstance(event, Event) for event in events
        ):
            raise ValueError(
                f"active period source {source_id!r} events must be an event list"
            )
        sources[source_id] = resolve_source_facts(events)

    periods = compile_active_period_expression(expression_spec).evaluate(sources, host)
    return period_union([], periods)
