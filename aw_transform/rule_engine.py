import math
import re
from collections import deque
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Pattern, Sequence, Set, Tuple

from aw_core import Event


Category = List[str]
MAX_EXPRESSION_DEPTH = 32
MAX_EXPRESSION_NODES = 4096
MAX_REGEX_LENGTH = 4096
MAX_CATEGORY_RULES = 1000
MAX_RULE_SOURCES = 128
MIN_RANKING_INTEGER = -1_000_000
MAX_RANKING_INTEGER = 1_000_000


def _parse_ranking_integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not MIN_RANKING_INTEGER <= value <= MAX_RANKING_INTEGER:
        raise ValueError(
            f"{name} must be between {MIN_RANKING_INTEGER} and {MAX_RANKING_INTEGER}"
        )
    return value


def scalar_to_string(value: Any) -> Optional[str]:
    """Return the ECMAScript string representation of a JSON scalar."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if not isinstance(value, float) or not math.isfinite(value):
        return None
    if value == 0:
        return "0"

    rendered = repr(value).lower()
    if "e" not in rendered:
        return rendered[:-2] if rendered.endswith(".0") else rendered

    mantissa, raw_exponent = rendered.split("e")
    exponent = int(raw_exponent)
    if -6 <= exponent < 21:
        negative = mantissa.startswith("-")
        digits = mantissa.lstrip("-").replace(".", "")
        decimal_position = 1 + exponent
        if decimal_position <= 0:
            fixed = "0." + "0" * -decimal_position + digits
        elif decimal_position >= len(digits):
            fixed = digits + "0" * (decimal_position - len(digits))
        else:
            fixed = digits[:decimal_position] + "." + digits[decimal_position:]
        return "-" + fixed if negative else fixed

    mantissa = mantissa[:-2] if mantissa.endswith(".0") else mantissa
    sign = "+" if exponent >= 0 else "-"
    return f"{mantissa}e{sign}{abs(exponent)}"


@dataclass(frozen=True)
class MatchResult:
    matched: bool
    score: int = 0


class RuleExpression:
    def match(self, event: Event) -> MatchResult:
        raise NotImplementedError

    def explain(self, event: Event) -> Dict[str, Any]:
        raise NotImplementedError


class NoneExpression(RuleExpression):
    def match(self, event: Event) -> MatchResult:
        return MatchResult(False)

    def explain(self, event: Event) -> Dict[str, Any]:
        return {"type": "none", "matched": False, "score": 0}


class RegexExpression(RuleExpression):
    def __init__(self, spec: Dict[str, Any]) -> None:
        regex = spec.get("regex")
        if not isinstance(regex, str) or not regex:
            raise ValueError("regex rule requires a non-empty string 'regex'")
        if len(regex) > MAX_REGEX_LENGTH:
            raise ValueError(
                f"regex rule exceeds maximum length of {MAX_REGEX_LENGTH}"
            )

        ignore_case = spec.get("ignore_case", False)
        if not isinstance(ignore_case, bool):
            raise ValueError("regex rule 'ignore_case' must be a boolean")

        try:
            self.regex: Pattern[str] = re.compile(
                regex, (re.IGNORECASE if ignore_case else 0) | re.UNICODE
            )
        except re.error as exc:
            raise ValueError(f"invalid regex {regex!r}: {exc}") from None
        self.negate = spec.get("negate", False)
        if not isinstance(self.negate, bool):
            raise ValueError("regex rule 'negate' must be a boolean")

        self.weight = _parse_ranking_integer(
            spec.get("weight", 0), "regex rule 'weight'"
        )

        self.value_mode = spec.get("value_mode", "string")
        if self.value_mode not in ("string", "scalar"):
            raise ValueError("regex rule 'value_mode' must be 'string' or 'scalar'")

        self.source = spec.get("source")
        if "source" in spec and not isinstance(self.source, str):
            raise ValueError("regex rule 'source' must be a string")
        if self.source and not re.fullmatch(r"[A-Za-z0-9_-]+", self.source):
            raise ValueError(
                "regex rule 'source' may only contain letters, numbers, '_' and '-'"
            )
        self.host = spec.get("host")
        if "host" in spec and (
            not isinstance(self.host, str) or not self.host
        ):
            raise ValueError("regex rule 'host' must be a non-empty string")

        fields = spec.get("fields", spec.get("field", spec.get("select_keys")))
        if isinstance(fields, str):
            fields = [fields]
        if fields is not None and (
            not isinstance(fields, list)
            or not fields
            or not all(isinstance(field, str) and field for field in fields)
        ):
            raise ValueError(
                "regex rule field selector must be a non-empty string or list of strings"
            )
        self.fields: Optional[List[str]] = fields

    def _selected_values(self, event: Event) -> List[Any]:
        metadata_keys = {
            "$category",
            "$category_score",
            "$category_rule",
            "$category_explain",
            "$host",
        }
        source_prefix = f"$source.{self.source}." if self.source else None
        if self.fields is not None:
            keys = [
                source_prefix + field if source_prefix is not None else field
                for field in self.fields
            ]
            return [event.data[key] for key in keys if key in event.data]
        if source_prefix is not None:
            return [
                value
                for key, value in event.data.items()
                if key.startswith(source_prefix)
            ]
        return [
            value
            for key, value in event.data.items()
            if key not in metadata_keys
            and not key.startswith("$source.")
        ]

    def _normalize_values(self, values: Sequence[Any]) -> List[str]:
        if self.value_mode == "string":
            return [value for value in values if isinstance(value, str)]
        return [
            normalized
            for value in values
            if (normalized := scalar_to_string(value)) is not None
        ]

    def _match_values(self, event: Event) -> Tuple[bool, int, int]:
        if self.host is not None and event.data.get("$host") != self.host:
            return False, 0, 0
        values = self._normalize_values(self._selected_values(event))
        if not values:
            return False, 0, 0
        any_match = any(self.regex.search(value) for value in values)
        matched = not any_match if self.negate else any_match
        return matched, self.weight if matched else 0, len(values)

    def match(self, event: Event) -> MatchResult:
        matched, score, _ = self._match_values(event)
        return MatchResult(matched, score)

    def explain(self, event: Event) -> Dict[str, Any]:
        matched, score, value_count = self._match_values(event)
        return {
            "type": "regex",
            "matched": matched,
            "score": score,
            "value_count": value_count,
            "host": self.host,
            "host_matched": self.host is None
            or event.data.get("$host") == self.host,
        }


class GroupExpression(RuleExpression):
    def __init__(self, expressions: List[RuleExpression]) -> None:
        if not expressions:
            raise ValueError("rule group must contain at least one child rule")
        self.expressions = expressions


class AllExpression(GroupExpression):
    def match(self, event: Event) -> MatchResult:
        score = 0
        for expression in self.expressions:
            result = expression.match(event)
            if not result.matched:
                return MatchResult(False)
            score += result.score
        return MatchResult(True, score)

    def explain(self, event: Event) -> Dict[str, Any]:
        children = [expression.explain(event) for expression in self.expressions]
        matched = all(child["matched"] for child in children)
        return {
            "type": "all",
            "matched": matched,
            "score": sum(child["score"] for child in children) if matched else 0,
            "children": children,
        }


class AnyExpression(GroupExpression):
    def match(self, event: Event) -> MatchResult:
        matches = [
            result
            for expression in self.expressions
            if (result := expression.match(event)).matched
        ]
        if not matches:
            return MatchResult(False)
        return MatchResult(True, max(result.score for result in matches))

    def explain(self, event: Event) -> Dict[str, Any]:
        children = [expression.explain(event) for expression in self.expressions]
        matched = [
            (index, child)
            for index, child in enumerate(children)
            if child["matched"]
        ]
        selected = max(matched, key=lambda item: (item[1]["score"], -item[0])) if matched else None
        return {
            "type": "any",
            "matched": selected is not None,
            "score": selected[1]["score"] if selected is not None else 0,
            "selected": selected[0] if selected is not None else None,
            "children": children,
        }


def compile_expression(
    spec: Dict[str, Any],
    *,
    _depth: int = 1,
    _state: Optional[Dict[str, Any]] = None,
) -> RuleExpression:
    if _depth > MAX_EXPRESSION_DEPTH:
        raise ValueError(
            f"rule expression exceeds maximum depth of {MAX_EXPRESSION_DEPTH}"
        )
    state = _state if _state is not None else {"nodes": 0, "sources": set()}
    state["nodes"] += 1
    if state["nodes"] > MAX_EXPRESSION_NODES:
        raise ValueError(
            f"rule expression exceeds maximum node count of {MAX_EXPRESSION_NODES}"
        )
    if not isinstance(spec, dict):
        raise ValueError("rule expression must be an object")

    rule_type = spec.get("type")
    if rule_type is None and "regex" in spec:
        rule_type = "regex"

    if rule_type == "none":
        return NoneExpression()
    if rule_type == "regex":
        expression = RegexExpression(spec)
        if expression.source:
            state["sources"].add(expression.source)
            if len(state["sources"]) > MAX_RULE_SOURCES:
                raise ValueError(
                    f"rule expressions exceed maximum source count of {MAX_RULE_SOURCES}"
                )
        return expression
    if rule_type in ("all", "any"):
        children = spec.get("rules", spec.get("children"))
        if not isinstance(children, list):
            raise ValueError(f"{rule_type} rule requires a list of child 'rules'")
        expressions = [
            compile_expression(child, _depth=_depth + 1, _state=state)
            for child in children
        ]
        return AllExpression(expressions) if rule_type == "all" else AnyExpression(
            expressions
        )
    raise ValueError(f"unknown rule expression type: {rule_type!r}")


@dataclass(frozen=True)
class CategoryRule:
    id: str
    name: Category
    expression: RuleExpression
    priority: int
    set_priority: int
    requires: Tuple[str, ...]
    index: int


def compile_category_rules(specs: List[Dict[str, Any]]) -> List[CategoryRule]:
    if not isinstance(specs, list):
        raise ValueError("category rules must be a list")
    if len(specs) > MAX_CATEGORY_RULES:
        raise ValueError(
            f"category rules exceed maximum count of {MAX_CATEGORY_RULES}"
        )

    rules: List[CategoryRule] = []
    ids: Set[str] = set()
    expression_state: Dict[str, Any] = {"nodes": 0, "sources": set()}
    for index, spec in enumerate(specs):
        if not isinstance(spec, dict):
            raise ValueError(f"category rule at index {index} must be an object")

        name = spec.get("name")
        if (
            not isinstance(name, list)
            or not name
            or not all(isinstance(segment, str) and segment for segment in name)
        ):
            raise ValueError(
                f"category rule at index {index} requires a non-empty string list 'name'"
            )

        rule_id = spec.get("id", "\x1f".join(name))
        if not isinstance(rule_id, str) or not rule_id:
            raise ValueError(f"category rule at index {index} has an invalid 'id'")
        if rule_id in ids:
            raise ValueError(f"duplicate category rule id: {rule_id!r}")
        ids.add(rule_id)

        priority = _parse_ranking_integer(
            spec.get("priority", 0),
            f"category rule {rule_id!r} 'priority'",
        )

        set_priority = _parse_ranking_integer(
            spec.get("set_priority", 0),
            f"category rule {rule_id!r} 'set_priority'",
        )

        requires = spec.get("requires", [])
        if not isinstance(requires, list) or not all(
            isinstance(required, str) and required for required in requires
        ):
            raise ValueError(
                f"category rule {rule_id!r} 'requires' must be a list of rule ids"
            )

        rules.append(
            CategoryRule(
                id=rule_id,
                name=name,
                expression=compile_expression(
                    spec.get("rule", {"type": "none"}), _state=expression_state
                ),
                priority=priority,
                set_priority=set_priority,
                requires=tuple(requires),
                index=index,
            )
        )

    rules_by_id = {rule.id: rule for rule in rules}
    for rule in rules:
        missing = [required for required in rule.requires if required not in rules_by_id]
        if missing:
            raise ValueError(
                f"category rule {rule.id!r} requires unknown rule ids: {missing!r}"
            )

    _category_dependency_order(rules)
    return rules


def _category_dependency_order(rules: Sequence[CategoryRule]) -> List[str]:
    dependency_count = {rule.id: len(rule.requires) for rule in rules}
    dependents: Dict[str, List[str]] = {rule.id: [] for rule in rules}
    for rule in rules:
        for required in rule.requires:
            dependents[required].append(rule.id)

    ready = deque(rule.id for rule in rules if dependency_count[rule.id] == 0)
    order: List[str] = []
    while ready:
        rule_id = ready.popleft()
        order.append(rule_id)
        for dependent in dependents[rule_id]:
            dependency_count[dependent] -= 1
            if dependency_count[dependent] == 0:
                ready.append(dependent)

    if len(order) != len(rules):
        cycle_rule = next(
            rule.id for rule in rules if dependency_count[rule.id] > 0
        )
        raise ValueError(f"category rule dependency cycle includes {cycle_rule!r}")
    return order


def _eligible_rule_ids(
    rules: List[CategoryRule],
    raw_matches: Dict[str, MatchResult],
    dependency_order: Sequence[str],
) -> Set[str]:
    rules_by_id = {rule.id: rule for rule in rules}
    eligibility: Dict[str, bool] = {}
    for rule_id in dependency_order:
        rule = rules_by_id[rule_id]
        eligibility[rule_id] = raw_matches[rule_id].matched and all(
            eligibility[required] for required in rule.requires
        )
    return {rule_id for rule_id, eligible in eligibility.items() if eligible}


def categorize_compiled(
    events: List[Event],
    rules: List[CategoryRule],
    host: Optional[str] = None,
) -> List[Event]:
    """Categorize with already compiled rules.

    Compatibility adapters use this entrypoint when their input contract differs
    from the v2 document schema. Evaluation and winner selection stay shared.
    """
    if host is not None and (not isinstance(host, str) or not host):
        raise ValueError("categorization host must be a non-empty string")
    events = deepcopy(events)
    dependency_order = _category_dependency_order(rules)

    for event in events:
        previous_host = event.data.get("$host")
        had_host = "$host" in event.data
        if host is not None:
            event.data["$host"] = host
        raw_matches = {rule.id: rule.expression.match(event) for rule in rules}
        eligible_ids = _eligible_rule_ids(rules, raw_matches, dependency_order)
        matching_rules = [rule for rule in rules if rule.id in eligible_ids]
        if matching_rules:
            winner = max(
                matching_rules,
                key=lambda rule: (
                    raw_matches[rule.id].score,
                    rule.priority,
                    len(rule.name),
                    rule.set_priority,
                    rule.index,
                ),
            )
            event.data["$category"] = winner.name
            event.data["$category_score"] = raw_matches[winner.id].score
            event.data["$category_rule"] = winner.id
        else:
            event.data["$category"] = ["Uncategorized"]
            event.data["$category_score"] = 0
            event.data["$category_rule"] = None
        if host is not None:
            if had_host:
                event.data["$host"] = previous_host
            else:
                event.data.pop("$host", None)
    return events


def categorize_v2(
    events: List[Event],
    category_specs: List[Dict[str, Any]],
    host: Optional[str] = None,
) -> List[Event]:
    return categorize_compiled(events, compile_category_rules(category_specs), host)


def categorize_v2_explain(
    events: List[Event],
    category_specs: List[Dict[str, Any]],
    host: Optional[str] = None,
) -> List[Event]:
    if host is not None and (not isinstance(host, str) or not host):
        raise ValueError("categorization host must be a non-empty string")
    events = deepcopy(events)
    rules = compile_category_rules(category_specs)
    dependency_order = _category_dependency_order(rules)
    rules_by_id = {rule.id: rule for rule in rules}

    for event in events:
        previous_host = event.data.get("$host")
        had_host = "$host" in event.data
        if host is not None:
            event.data["$host"] = host
        raw_matches = {rule.id: rule.expression.match(event) for rule in rules}
        raw_explanations = {
            rule.id: rule.expression.explain(event) for rule in rules
        }
        eligible_ids = _eligible_rule_ids(rules, raw_matches, dependency_order)
        matching_rules = [rule for rule in rules if rule.id in eligible_ids]
        winner = (
            max(
                matching_rules,
                key=lambda rule: (
                    raw_matches[rule.id].score,
                    rule.priority,
                    len(rule.name),
                    rule.set_priority,
                    rule.index,
                ),
            )
            if matching_rules
            else None
        )
        if winner is not None:
            event.data["$category"] = winner.name
            event.data["$category_score"] = raw_matches[winner.id].score
            event.data["$category_rule"] = winner.id
        else:
            event.data["$category"] = ["Uncategorized"]
            event.data["$category_score"] = 0
            event.data["$category_rule"] = None
        event.data["$category_explain"] = {
            "winner": winner.id if winner is not None else None,
            "candidates": [
                {
                    "id": rule.id,
                    "name": rule.name,
                    "matched": raw_matches[rule.id].matched,
                    "eligible": rule.id in eligible_ids,
                    "score": raw_matches[rule.id].score,
                    "priority": rule.priority,
                    "depth": len(rule.name),
                    "set_priority": rule.set_priority,
                    "requires": list(rule.requires),
                    "requirements": {
                        required: (
                            raw_matches[required].matched
                            and required in eligible_ids
                        )
                        for required in rule.requires
                        if required in rules_by_id
                    },
                    "expression": raw_explanations[rule.id],
                }
                for rule in rules
            ],
        }
        if host is not None:
            if had_host:
                event.data["$host"] = previous_host
            else:
                event.data.pop("$host", None)
    return events
