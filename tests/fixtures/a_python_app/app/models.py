"""Domain objects."""

from dataclasses import dataclass


@dataclass
class Item:
    id: int
    name: str
    qty: int
