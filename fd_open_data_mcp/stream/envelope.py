"""Event envelope + ULID idempotency key (add-data-stream-engine task 3.1).

A published observation is an ``EventEnvelope``: a flat, versioned record
carrying its own idempotency key (``event_id``), the concept/entity point it
lands, and either an inline value or a ``payload_ref`` pointer to object
storage (for large payloads — the MinIO already configured in the deploy).

The idempotency key is a ULID (Crockford base32, 48-bit ms timestamp + 80-bit
randomness): sortable by emission time, globally unique, and — crucially —
monotonic within the same millisecond via an incrementing random tail, so a
burst of events from one worker never collides. It is the dedup key at the
consume side AND the replay cursor (replay filters on event_id's embedded
timestamp).

Serialization is a flat ``dict[str, str]`` (Redis Streams fields are
string→string); ``to_fields``/``from_fields`` are the single encode/decode
point so a backend never re-implements the wire format.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

SCHEMA_V = 1

# Crockford's base32 alphabet (ULID).
_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_TIME_LEN = 10   # 48-bit ms → ceil(48/5)=10 chars
_RAND_LEN = 16   # 80-bit random → ceil(80/5)=16 chars


class _Ulid:
    """Monotonic-within-ms ULID generator (process-local)."""

    def __init__(self) -> None:
        self._last_ms = -1
        self._last_rand = 0

    def next(self) -> str:
        ms = int(time.time() * 1000)
        if ms == self._last_ms:
            rand = self._last_rand + 1  # monotonic tail within the ms
            if rand >= (1 << 80):
                raise RuntimeError("ULID random overflow within one millisecond")
        else:
            self._last_ms = ms
            rand = int.from_bytes(os.urandom(10), "big")  # 80 bits
        self._last_rand = rand
        tchars = _B32
        out = []
        v = ms
        for _ in range(_TIME_LEN):
            out.append(tchars[v & 0x1F]); v >>= 5
        out.reverse()
        out.append("-")
        rchars = []
        v = rand
        for _ in range(_RAND_LEN):
            rchars.append(tchars[v & 0x1F]); v >>= 5
        rchars.reverse()
        out.extend(rchars)
        return "".join(out)


_ULID = _Ulid()


def new_event_id() -> str:
    """A fresh monotonic ULID (26 chars, '-' separated time|rand)."""
    return _ULID.next()


def event_id_ms(event_id: str) -> int:
    """The millisecond timestamp embedded in a ULID (replay time filter)."""
    head = event_id.split("-")[0]
    v = 0
    for ch in head:
        v = (v << 5) | _B32.index(ch)
    return v


@dataclass(frozen=True)
class EventEnvelope:
    """One normalized record in flight. ``kind`` is the payload shape tag
    (today only ``observation``; the field keeps the wire format open for
    future event kinds without a schema break)."""

    event_id: str
    source: str
    concept_id: int
    entity_type: str
    entity_id: int
    date: str
    granularity: str
    source_used: str
    value: str | None = None
    unit: str | None = None
    schema_v: int = SCHEMA_V
    emitted_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    payload_ref: str | None = None   # object-storage pointer; value/unit live there
    kind: str = "observation"

    def to_fields(self) -> dict[str, str]:
        """Flat string map for the stream wire. None → '' (Redis has no NULL)."""
        return {
            "event_id": self.event_id,
            "source": self.source,
            "kind": self.kind,
            "concept_id": str(self.concept_id),
            "entity_type": self.entity_type,
            "entity_id": str(self.entity_id),
            "date": self.date,
            "granularity": self.granularity,
            "source_used": self.source_used,
            "value": "" if self.value is None else str(self.value),
            "unit": "" if self.unit is None else str(self.unit),
            "schema_v": str(self.schema_v),
            "emitted_at": self.emitted_at,
            "payload_ref": "" if self.payload_ref is None else self.payload_ref,
        }

    @classmethod
    def from_fields(cls, fields: dict[str, str]) -> "EventEnvelope":
        return cls(
            event_id=fields["event_id"],
            source=fields["source"],
            kind=fields.get("kind", "observation"),
            concept_id=int(fields["concept_id"]),
            entity_type=fields["entity_type"],
            entity_id=int(fields["entity_id"]),
            date=fields["date"],
            granularity=fields.get("granularity", "day"),
            source_used=fields["source_used"],
            value=fields.get("value") or None,
            unit=fields.get("unit") or None,
            schema_v=int(fields.get("schema_v", SCHEMA_V)),
            emitted_at=fields.get("emitted_at", ""),
            payload_ref=fields.get("payload_ref") or None,
        )
