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
from situc.codegen.c.build import RUN, refusals		# noqa: E402
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
	         or part.role == RUN or part.role in plain_build.MEASURED]
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
struct kinded {
	u2 kind;
	u6 rest;
	variant body switch (kind) {
		case 1:  word held;
		default: nothing other;
	}
}
""", encoding="ascii")
	units = _units(schema)
	said  = four_say(schema, tmp_path, list(units.values()), [
		_with(units["flagged.held"], 5, b"\x01\xaa"),
		_with(units["flagged.absent"], 5, b""),
		# The default arm's tag is the caller's, short of one a named arm
		# claims.
		_with(units["kinded.other"], 2, 5, b""),
		_with(units["kinded.other"], 1, 5, b""),
	])
	assert _extra(said, 4) == ["85" "01aa", "05", "85", "refused"]


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


def test_a_member_may_be_called_what_a_builder_calls_its_own(
		tmp_path: Path) -> None:
	"""`u8 tail;` was the walk's own flag, and `len(tail)` raised."""
	schema = tmp_path / "names.situ"
	schema.write_text("""\
struct word { u8 n; u8 data[n]; }
struct names {
	u8 out;
	u8 view;
	u8 need;
	u8 scan;
	u8 count;
	word element[3];
	u8 tail[2];
}
""", encoding="ascii")
	units = _units(schema)
	said  = four_say(schema, tmp_path, list(units.values()), [
		_with(units["names."], 1, 2, 3, 4, 5,
		      b"\x00\x00\x00", b"\x08\x09"),
	])
	assert _extra(said, 1) == ["0102030405000000" "0809"]


# -- a run of them (26.865) -------------------------------------------------

RUNS = """target buffer;
struct word { u8 n [max = 3]; u8 data[n]; }
struct counted { u8 n; word items[n]; u8 after; }
struct pair { word items[2]; }
struct ended { word items[] until ")"; u8 after; }
struct rest { u8 head; word items[remaining]; }
struct stopped { word items[] while (n != 0); }
"""

ONE = b"\x01\xaa"
TWO = b"\x02\xbb\xcc"
STOP = b"\x00"


def test_a_run_has_to_be_whole_elements_in_all_four(tmp_path: Path) -> None:
	schema = tmp_path / "runs.situ"
	schema.write_text(RUNS, encoding="ascii")
	units = _units(schema)
	cases = [
		# The count is what the walk finds, and never the caller's.
		(_with(units["counted."], ONE + TWO, 9), "02" "01aa" "02bbcc" "09"),
		(_with(units["counted."], b"", 9), "00" "09"),
		# An element cut short, and one its own rule refuses.
		(_with(units["counted."], ONE + b"\x02\xbb", 9), "refused"),
		(_with(units["counted."], b"\x09" + bytes(9), 9), "refused"),
		# A fixed count is exactly that many.
		(_with(units["pair."], ONE + TWO), "01aa02bbcc"),
		(_with(units["pair."], ONE), "refused"),
		(_with(units["pair."], ONE + TWO + ONE), "refused"),
		# The delimiter is written, and an element may not begin with it.
		(_with(units["ended."], ONE, 9), "01aa" "29" "09"),
		(_with(units["ended."], b")" + ONE, 9), "refused"),
		(_with(units["rest."], 7, ONE + TWO), "07" "01aa" "02bbcc"),
		# The reader stops at the first element the condition fails for,
		# and bytes past it are not the run the caller was told it wrote.
		(_with(units["stopped."], ONE + STOP), "01aa00"),
		(_with(units["stopped."], STOP + ONE), "refused"),
	]
	said = four_say(schema, tmp_path, list(units.values()),
	                [case for case, _ in cases])
	assert _extra(said, len(cases)) == [made for _, made in cases]


def test_a_run_of_nothing_cannot_spin(tmp_path: Path) -> None:
	"""A zero-byte element would never advance the walk."""
	schema = tmp_path / "empty.situ"
	schema.write_text("""\
struct none { }
struct holds { u8 n; none items[n]; }
""", encoding="ascii")
	units = _units(schema)
	said  = four_say(schema, tmp_path, list(units.values()), [
		_with(units["holds."], b"\x00"),
	])
	assert _extra(said, 1) == ["refused"]


# -- a run of bytes a delimiter ends (26.866) -------------------------------

ENDED_RUNS = r"""target buffer;
struct plain   { u8 name[] until ")"; u8 z; }
struct wide    { u8 name[] until "--"; u8 z; }
struct escaped { u8 name[] until ")" [escape = "\\"]; u8 z; }
"""


def test_a_delimiter_ends_a_run_in_all_four(tmp_path: Path) -> None:
	schema = tmp_path / "ended.situ"
	schema.write_text(ENDED_RUNS, encoding="ascii")
	units = _units(schema)
	cases = [
		(_with(units["plain."], b"ab", 9), "6162" "29" "09"),
		(_with(units["plain."], b"", 9), "29" "09"),
		# It would read back short, and no escape is stated.
		(_with(units["plain."], b"a)b", 9), "refused"),
		(_with(units["wide."], b"a-b", 9), "612d62" "2d2d" "09"),
		(_with(units["wide."], b"a--b", 9), "refused"),
		# The delimiter and the escape carry it, and nothing else does.
		(_with(units["escaped."], b"a)b", 9), "61" "5c29" "62" "29" "09"),
		(_with(units["escaped."], b"\\", 9), "5c5c" "29" "09"),
	]
	said = four_say(schema, tmp_path, list(units.values()),
	                [case for case, _ in cases])
	assert _extra(said, len(cases)) == [made for _, made in cases]


# -- a tag the schema computes (26.867) -------------------------------------

def test_a_tag_is_computed_once_the_message_is_whole(tmp_path: Path) -> None:
	"""A published value rather than four languages agreeing: every PNG
	ends with this chunk, and these are its bytes."""
	schema = ROOT / "example" / "png" / "png.situ"
	units  = _units(schema)
	said   = four_say(schema, tmp_path, list(units.values()), [
		_with(units["chunk."], b"IEND", b""),
	])
	assert _extra(said, 1) == ["00000000" "49454e44" "ae426082"]
