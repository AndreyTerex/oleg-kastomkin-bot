"""Общие заглушки: тестам не нужен настоящий Discord."""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402


@dataclass(eq=False)
class FakeRole:
    id: int
    name: str = "role"
    position: int = 1
    mentionable: bool = True

    def __lt__(self, other: "FakeRole") -> bool:
        return self.position < other.position

    def __ge__(self, other: "FakeRole") -> bool:
        return self.position >= other.position


@dataclass(eq=False)
class FakeMember:
    id: int
    display_name: str = "player"
    roles: list = field(default_factory=list)
    bot: bool = False

    @property
    def mention(self) -> str:
        return f"<@{self.id}>"


def lane_role(key: str) -> FakeRole:
    return FakeRole(config.LANE_BY_KEY[key].role_id, key)


@pytest.fixture
def make_member():
    def factory(user_id: int, *lanes: str, name: str | None = None) -> FakeMember:
        return FakeMember(user_id, name or f"p{user_id}", [lane_role(key) for key in lanes])

    return factory
