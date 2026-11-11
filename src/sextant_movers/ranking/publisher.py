"""The ranking data type and the universe digest, the publisher's first two names.

`ranking_data_type(feed, view)` is the engine's `DataType` (nautechsystems/nautilus_trader
v2.0.0rc8 `d53849b216e20f2c122ca01b4dd0da377638b186`,
`python/nautilus_trader/model/__init__.pyi:1688`) under the registered type name,
`generation.TYPE_NAME`, with the metadata `feed`, `view` and `ranker`. The engine builds the topic
from those pairs sorted by key (`crates/model/src/data/data_type.rs:154-163,346-361`) and compares
and hashes data types by topic (`:584-608`), so each feed and view is its own topic. Feed and view
are not checked here. `universe_digest` is the SHA-256 of the compact JSON of the sorted symbols:
a repeat counts, and nothing is checked. Nothing here starts a stream, a socket, a thread or a
node, and nothing here places an order.

Source: the reference's `src/sextant/ranking/publisher.py:40-48` at
`06562bec0898a7203f4eaf69086ced83d2477e6b`, rebuilt value for value; the publisher itself is a
later part.
"""

import hashlib
import json

from nautilus_trader.model import DataType

from sextant_movers.ranking.generation import TYPE_NAME

RANKER = "sextant/fixture-ranking/1"  # the reference's publisher.py:40, word for word


def universe_digest(symbols: tuple[str, ...]) -> str:
    text = json.dumps(sorted(symbols), separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def ranking_data_type(feed: str, view: str = "full") -> DataType:
    return DataType(TYPE_NAME, metadata={"feed": feed, "view": view, "ranker": RANKER})
