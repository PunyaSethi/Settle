"""Canonical JSON encoding. SPEC §5.6.

This function determines every hash in the audit chain, so it must produce
identical bytes for equal objects across runs, machines and Python
invocations. Everything here is in service of that one property.

The rules, and why each one is needed:

- **Sorted keys.** Python dict order is insertion order, which depends on
  construction path. Sorting removes the dependence.
- **No whitespace.** `(",", ":")` separators, so no formatting choice can leak
  into a hash.
- **ASCII-escaped output.** Hinglish and Devanagari payloads (SPEC §11) would
  otherwise encode as raw UTF-8, whose bytes are stable but whose handling
  across tooling is not. `\\uXXXX` escapes are pure ASCII and unambiguous.
- **Datetimes as ISO-8601 UTC with an explicit offset.** Two instants that are
  equal must hash equal regardless of the timezone they were constructed in.
  Naive datetimes are rejected outright: there is no correct guess.
- **No floats where money belongs.** Money is `int` paise everywhere in
  `settle/schema/`. This encoder cannot tell a money field from any other
  number, so it enforces what it can — `Decimal` is rejected, and non-finite
  floats are rejected because `NaN` and `Infinity` are not JSON.
- **`frozenset` sorted, `set` refused.** Iteration order is not a property
  of a set's value, so a frozenset is sorted on the way out and a mutable
  set is refused outright.

`canonical_json` returns `bytes`, not `str`, because the thing that follows it
is `hashlib`, and bytes is the type that has no encoding ambiguity left in it.
"""

import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from math import isfinite
from typing import Any
from uuid import UUID

from pydantic import BaseModel

__all__ = ["canonical_json", "to_canonical_obj"]


def to_canonical_obj(obj: Any) -> Any:
    """Reduce `obj` to JSON primitives under the rules in the module docstring.

    Raises `TypeError` for anything whose encoding would not be byte-stable.
    """
    if obj is None or isinstance(obj, (str, bool)):
        return obj

    if isinstance(obj, BaseModel):
        return to_canonical_obj(obj.model_dump(mode="python"))

    if isinstance(obj, Enum):
        return to_canonical_obj(obj.value)

    if isinstance(obj, datetime):
        if obj.tzinfo is None or obj.tzinfo.utcoffset(obj) is None:
            raise TypeError(
                "canonical_json refuses naive datetimes: the correct offset "
                "is not knowable, and guessing one silently changes a hash"
            )
        return obj.astimezone(timezone.utc).isoformat()

    if isinstance(obj, date):
        return obj.isoformat()

    if isinstance(obj, int):
        return obj

    if isinstance(obj, float):
        if not isfinite(obj):
            raise TypeError(f"canonical_json refuses non-finite float: {obj!r}")
        return obj

    if isinstance(obj, Decimal):
        raise TypeError(
            "canonical_json refuses Decimal: money is int paise everywhere in "
            "settle/schema/ (SPEC §5.1)"
        )

    if isinstance(obj, UUID):
        return str(obj)

    if isinstance(obj, Mapping):
        out: dict[str, Any] = {}
        for key, value in obj.items():
            if not isinstance(key, str):
                raise TypeError(f"canonical_json requires str keys, got {type(key).__name__}")
            out[key] = to_canonical_obj(value)
        return out

    if isinstance(obj, frozenset):
        # Sorted, so the encoding is a property of the value rather than of the
        # process that built it. Set iteration order varies with
        # PYTHONHASHSEED, which would make the same CaseState hash differently
        # in two runs — exactly what GEN-1 and the audit chain rule out.
        return sorted(to_canonical_obj(item) for item in obj)

    if isinstance(obj, set):
        raise TypeError(
            "canonical_json refuses mutable sets: use frozenset, which this "
            "encoder sorts. A mutable container inside a frozen contract is "
            "only half frozen"
        )

    if isinstance(obj, Sequence):
        return [to_canonical_obj(item) for item in obj]

    raise TypeError(f"canonical_json cannot encode {type(obj).__name__}")


def canonical_json(obj: Any) -> bytes:
    """Encode `obj` as canonical JSON bytes, ready to hash."""
    return json.dumps(
        to_canonical_obj(obj),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
