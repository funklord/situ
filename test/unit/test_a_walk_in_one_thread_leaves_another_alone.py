"""A walk's validate state is its own thread's (26.1136).

`report._validate` kept how deep it was and what it had found clean in
module globals, the second keyed by neither image nor buffer. A walk in
one thread therefore started another's count where it had got to, and
could hand it a struct some other schema's walk had found clean:
replaying six schemas' fuzz corpora through the walker in six threads,
one protobuf message the walker refuses on its own read as valid.

Held here without a race: one thread is parked six structs down a chain
of four CBOR tags while a second validates a chain of two tags -- four structs,
inside the eight this walker follows (`WALK_DEPTH_MAX`). With shared
state the second starts at six, passes eight, and cannot say; with its
own it answers OK as it does alone.

Before 26.1136 this passed with the state shared, for a second wrong
reason: `_arm_validates` returned `_validate(...) or OK`, so the arm past
the ceiling came back "cannot say" and was read as a pass.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

import pytest						# noqa: E402

from every_schema import load_schema			# noqa: E402
from situc import pack as packer			# noqa: E402
from situc.layout import solve				# noqa: E402
from situc.resolve import resolve			# noqa: E402
from test_walker_c import shape_named			# noqa: E402
from walker import report				# noqa: E402
from walker.image import load				# noqa: E402
from walker.walk import acquire				# noqa: E402

SCHEMA = ROOT / "example" / "cbor" / "cbor.situ"
PARK   = 6


def _chain(depth: int) -> bytes:
	return b"\xc1" * depth + b"\x00"


def test_a_parked_walk_does_not_deepen_another(
		monkeypatch: pytest.MonkeyPatch) -> None:
	parsed = load_schema(SCHEMA)
	image = load(packer.pack(parsed, resolve(parsed, solve(parsed)),
	                         metadata=True)[0])
	shape = shape_named(SCHEMA, "tagged")

	def verdict(data: bytes) -> int | None:
		return report._validate(image, acquire(image, data, shape, []),
		                        shape)

	alone = verdict(_chain(2))
	assert alone == report.OK

	here = report._validate_here
	parked, release = threading.Event(), threading.Event()
	depth = threading.local()

	def counting(*args: object, **kwargs: object) -> int | None:
		held = getattr(depth, "n", 0) + 1
		depth.n = held
		try:
			if threading.current_thread().name == "deep" and held == PARK:
				parked.set()
				release.wait(30)
			return here(*args, **kwargs)	# type: ignore[arg-type]
		finally:
			depth.n = held - 1

	monkeypatch.setattr(report, "_validate_here", counting)
	deep = threading.Thread(target=verdict, args=(_chain(4),),
	                        name="deep")
	deep.start()
	try:
		assert parked.wait(30), "the deep walk never got six structs down"
		beside = verdict(_chain(2))
	finally:
		release.set()
		deep.join(30)
	assert beside == alone
