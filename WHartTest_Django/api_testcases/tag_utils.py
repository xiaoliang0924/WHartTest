"""Utilities for ordering API test case tags in UI-friendly display order."""

from __future__ import annotations

from typing import Iterable, Sequence, TypeVar

# Module-level parent tags should appear before feature/action tags.
PARENT_TAG_ORDER: tuple[str, ...] = (
    "我的工单",
    "工单列表",
)

_PARENT_TAG_RANK = {name: index for index, name in enumerate(PARENT_TAG_ORDER)}

T = TypeVar("T")


def sort_testcase_tags(tags: Iterable[T]) -> list[T]:
    """Sort tags with parent/module tags first, then others alphabetically."""

    def sort_key(tag: T) -> tuple[int, int, str]:
        name = getattr(tag, "name", None)
        if name is None and isinstance(tag, dict):
            name = tag.get("name", "")
        name = str(name or "")
        if name in _PARENT_TAG_RANK:
            return (0, _PARENT_TAG_RANK[name], name)
        return (1, 0, name)

    return sorted(tags, key=sort_key)


def serialize_sorted_tags(tags: Iterable, serializer_class) -> list[dict]:
    return serializer_class(sort_testcase_tags(tags), many=True).data
