import re
from copy import deepcopy
from typing import Any, Dict, List, Optional, Pattern, Tuple

from aw_core import Event

from .rule_engine import (
    CategoryRule,
    MatchResult,
    NoneExpression,
    RuleExpression,
    categorize_compiled,
)


Tag = str
Category = List[str]


class Rule:
    """Predicate used by the legacy Query2 ``categorize`` and ``tag`` APIs."""
    regex: Optional[Pattern]
    select_keys: Optional[List[str]]
    ignore_case: bool

    def __init__(self, rules: Dict[str, Any]) -> None:
        if not isinstance(rules, dict):
            raise ValueError("legacy rule must be an object")
        self.select_keys = rules.get("select_keys", None)
        self.ignore_case = rules.get("ignore_case", False)

        # NOTE: Also checks that the regex isn't an empty string (which would erroneously match everything)
        regex_str = rules.get("regex", None)
        self.regex = (
            re.compile(
                regex_str, (re.IGNORECASE if self.ignore_case else 0) | re.UNICODE
            )
            if regex_str
            else None
        )

    def match(self, e: Event) -> bool:
        """Match one event according to the legacy Query2 predicate contract."""
        if self.select_keys:
            values = [e.data.get(key, None) for key in self.select_keys]
        else:
            values = list(e.data.values())
        if self.regex:
            for val in values:
                if isinstance(val, str) and self.regex.search(val):
                    return True
        return False


class _LegacyExpression(RuleExpression):
    def __init__(self, rule: Rule) -> None:
        self.rule = rule

    def match(self, event: Event) -> MatchResult:
        return MatchResult(self.rule.match(event))

    def explain(self, event: Event) -> Dict[str, Any]:
        matched = self.rule.match(event)
        return {"type": "regex", "matched": matched, "score": 0}


def categorize(
    events: List[Event], classes: List[Tuple[Category, Rule]]
) -> List[Event]:
    """Adapt the legacy categorization contract to the shared v2 evaluator.

    Legacy Query2 accepts rule counts, pattern lengths, and category names which
    are outside the persisted v2 schema. Its already compiled predicates enter
    evaluation directly instead of being revalidated as a v2 document.
    """
    compiled = []
    for index, (category, rule) in enumerate(classes):
        # A zero-depth legacy category cannot beat the default one-element name.
        if not category:
            continue
        compiled.append(
            CategoryRule(
                id=f"legacy-{index}",
                name=category,
                expression=_LegacyExpression(rule)
                if rule.regex is not None
                else NoneExpression(),
                priority=0,
                set_priority=0,
                requires=(),
                index=index,
            )
        )

    preserved_metadata = [
        {
            key: event.data[key]
            for key in ("$category_score", "$category_rule")
            if key in event.data
        }
        for event in events
    ]
    categorized = categorize_compiled(events, compiled)
    for event, preserved in zip(categorized, preserved_metadata):
        for key in ("$category_score", "$category_rule"):
            if key in preserved:
                event.data[key] = preserved[key]
            else:
                event.data.pop(key, None)
    return categorized


def tag(events: List[Event], classes: List[Tuple[Tag, Rule]]) -> List[Event]:
    """Apply the legacy Query2 tag transform."""
    return [_tag_one(event, classes) for event in deepcopy(events)]


def _tag_one(e: Event, classes: List[Tuple[Tag, Rule]]) -> Event:
    e.data["$tags"] = [_cls for _cls, rule in classes if rule.match(e)]
    return e
