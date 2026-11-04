"""A nested member is handed over built, and the writer checks it.

The caller builds the inner struct and passes its bytes. Those bytes state
their own extent, so all four writers ask: too many shift every member after
it and too few leave the next member inside it, and neither is a message.
Where the inner struct's first byte is the outer's peeked discriminant, it
has also to be this arm's.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

import fourway						# noqa: E402
from every_schema import load_schema			# noqa: E402
from test_the_four_builders_agree import Given, four_say	# noqa: E402

from situc.codegen import plain_build			# noqa: E402
from situc.codegen.plain_build import Unit		# noqa: E402
from situc.codegen.c.build import refusals		# noqa: E402
from situc.layout import solve				# noqa: E402
from situc.resolve import resolve			# noqa: E402

pytestmark = pytest.mark.skipif(not fourway.COMPLETE,
                                reason="needs gcc, g++ and rustc")

SCHEMA = """\
struct header { u8 n; u8 data[n]; }
struct outer  { u8 a; header header; u8 z; }
struct ping   { u8 kind; u8 v; }
struct pong   { u8 kind; u8 v; }
struct other  { u8 kind; }
struct either {
	peek u8 kind;
	variant body switch (kind) {
		case 1:  ping ping;
		case 2:  pong pong;
		default: other other;
	}
}
"""


def _units(schema: Path) -> dict[str, Unit]:
	parsed   = load_schema(schema)
	resolved = resolve(parsed, solve(parsed))
	found    = plain_build.units(resolved, plain_build.shape(parsed, resolved))
	return {f"{one.struct.name}.{one.suffix}": one for one in found}


def _with(unit: Unit, *values: int | bytes) -> tuple[Unit, Given]:
	taken = [part for part in unit.parts if plain_build.takes_value(part)
	         or part.role in plain_build.MEASURED]
	assert len(taken) == len(values), [part.role for part in taken]
	return unit, list(zip(taken, values))


def _extra(said: str, count: int) -> list[str]:
	return [line.split(" ")[1] for line in said.splitlines()[-count:]]


def test_the_bytes_have_to_be_exactly_one_inner_message(
		tmp_path: Path) -> None:
	schema = tmp_path / "nested.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	units = _units(schema)
	outer = units["outer."]
	said  = four_say(schema, tmp_path, list(units.values()), [
		_with(outer, 7, b"\x02\xaa\xbb", 9),
		_with(outer, 7, b"\x02\xaa\xbb\xcc", 9),	# one too many
		_with(outer, 7, b"\x02\xaa", 9),		# one too few
		_with(outer, 7, b"", 9),			# none at all
	])
	assert _extra(said, 4) == ["0702aabb09", "refused", "refused", "refused"]


def test_the_bytes_have_to_be_this_arm(tmp_path: Path) -> None:
	schema = tmp_path / "nested.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	units = _units(schema)
	said  = four_say(schema, tmp_path, list(units.values()), [
		_with(units["either.ping"], b"\x01\x55"),
		_with(units["either.ping"], b"\x02\x55"),	# a pong in ping's arm
		_with(units["either.pong"], b"\x02\x55"),
		_with(units["either.other"], b"\x07"),
		_with(units["either.other"], b"\x01"),	# ping's tag, default's arm
	])
	assert _extra(said, 5) == ["0155", "refused", "0255", "07", "refused"]


def _declined(schema: Path) -> str:
	parsed   = load_schema(schema)
	resolved = resolve(parsed, solve(parsed))
	shape    = plain_build.shape(parsed, resolved)
	return " ".join(why for _, why in refusals(resolved, shape))


def test_two_variants_are_not_one_arm_written_twice(tmp_path: Path) -> None:
	"""mqtt's CONNECT went out with `will_present` where its name belonged."""
	schema = tmp_path / "two.situ"
	schema.write_text("""\
struct word { u8 n; u8 data[n]; }
struct two {
	u8 a;
	u8 b;
	variant first switch (a) {
		case 1:  word one;
		default: error;
	}
	variant second switch (b) {
		case 1:  word other;
		default: error;
	}
}
""", encoding="ascii")
	assert "more than one variant" in _declined(schema)


def test_a_packed_tag_cannot_disagree_with_its_arm(tmp_path: Path) -> None:
	schema = tmp_path / "packed.situ"
	schema.write_text("""\
bit_order msb_first;            // 2.2.1: the type is the *high* nibble
struct word { u8 n; u8 data[n]; }
struct nothing { }
struct flagged {
	u1 present;
	u7 rest;
	variant body switch (present) {
		case 1:  word held;
		case 0:  nothing absent;
		default: error;
	}
}
""", encoding="ascii")
	assert "packed discriminant" in _declined(schema)


def test_a_version_too_old_for_what_is_written_is_refused(
		tmp_path: Path) -> None:
	"""It validates: the rules on a `[since]` member are gated with it."""
	schema = tmp_path / "versioned.situ"
	schema.write_text("""\
struct versioned [version = rev] {
	u8 rev;
	u8 later [since = 2];
}
""", encoding="ascii")
	units = _units(schema)
	said  = four_say(schema, tmp_path, list(units.values()), [
		_with(units["versioned."], 0, 7),
		_with(units["versioned."], 1, 7),
		_with(units["versioned."], 2, 7),
	])
	assert _extra(said, 3) == ["refused", "refused", "0207"]
