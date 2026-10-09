"""A `peek` does not consume, and its bytes still have to be there.

hull reported (suggestion/hull.md, 14) that a trailing `peek` in a nested
struct cannot see the parent's next byte. Reproducing it found a second
and sharper thing, which needs no nesting at all: the same shape at the
top level ACCEPTS a buffer that does not hold the peeked byte.

    struct fixed_peek { u8 a; peek u8 next; variant more switch (next) ... }
    one byte, peeking byte 1   -> conformed, reading past the input

    struct dyn_peek { u8 n; u8 body[n]; peek u8 next; variant more ... }
    two bytes, peeking byte 2  -> BoundsError: outside the frame

The two disagreed, and the static one disagreed in the direction that
reads past the input: a peek advances no cursor, so it added nothing to
the extent -- right -- and nothing to the MINIMUM either, which is wrong,
because the variant after it switches on those bytes and a struct cannot
be read without them.

The minimum is the one place all five consumers ask -- four backends and
the walker -- so that is where it is fixed.

And it has to count only the struct's OWN peeks. The first version
counted every placement, which includes a variant arm's interior and a
run element's, so a peek forty bytes inside one arm raised the minimum
for a message that takes the other arm, and a peek in a run's element
made an empty run impossible. hull reported that as finding 16 against
the commit two later, having adopted it; the cases are below.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

from situc.codegen.c import generate as generate_c	# noqa: E402
from situc.diagnostics import Source			# noqa: E402
from situc.layout import solve				# noqa: E402
from situc.parser import parse				# noqa: E402
from situc.resolve import resolve			# noqa: E402

SCHEMA = """target buffer;
endian big;

struct nothing {
}

struct fixed_peek {
\tu8       a;
\tpeek u8  next;
\tvariant  more switch (next) {
\t\tcase 'B': u8       extra;
\t\tdefault:  nothing  none;
\t}
}

struct dyn_peek {
\tu8       n;
\tu8       body[n];
\tpeek u8  next;
\tvariant  more switch (next) {
\t\tcase 'B': u8       extra;
\t\tdefault:  nothing  none;
\t}
}
"""

#: Each is one byte short of holding the byte its peek reads, and each
#: must be refused. The pair is the point: before the fix the first
#: conformed and the second did not, so either alone would have looked
#: like a rule rather than a disagreement.
SHORT = "fixed_peek one 41\ndyn_peek d 01 41\n"

#: And the same two with that byte present, so the fix is not just a
#: refusal of everything.
WHOLE = "fixed_peek two 41 42\ndyn_peek e 01 41 42\n"


def _parts():  # type: ignore[no-untyped-def]  # noqa: ANN202
	parsed   = parse(Source("inline", SCHEMA))
	resolved = resolve(parsed, solve(parsed))
	return parsed, resolved


def test_a_static_peek_raises_the_minimum_to_its_own_end() -> None:
	"""The layout, which is what every consumer reads.

	`fixed_peek` is `u8` then a peeked `u8`, so a complete one is two
	bytes however little of it the extent counts.
	"""
	_, resolved = _parts()		# type: ignore[no-untyped-call]
	assert resolved.structs["fixed_peek"].layout.size_bytes == 2


def test_the_c_header_states_the_raised_minimum() -> None:
	"""Asserted through the emitted constant rather than by compiling.

	`SITU_FIXED_PEEK_SIZE_MIN` is what the C view acquisition checks
	against, so the fix reaches C by the same number the layout raised --
	which is the argument for fixing it in the layout rather than in each
	backend.
	"""
	parsed, resolved = _parts()	# type: ignore[no-untyped-call]
	header = generate_c(parsed, resolved, "peeky").files()["peeky.h"]
	assert "#define SITU_FIXED_PEEK_SIZE_MIN   2u" in header, [
		line for line in header.split("\n") if "FIXED_PEEK_SIZE" in line]


def test_both_shapes_refuse_a_buffer_short_of_the_peeked_byte(
		tmp_path: Path) -> None:
	"""End to end through `situc verify`, which is how hull met it.

	Two vectors that must be refused and two that must conform, in one
	run each, because a fix that refuses everything passes the first
	assertion alone.
	"""
	(tmp_path / "p.situ").write_text(SCHEMA, encoding="utf-8")
	(tmp_path / "short").write_text(SHORT, encoding="ascii")
	(tmp_path / "whole").write_text(WHOLE, encoding="ascii")

	short = subprocess.run(
		[sys.executable, str(ROOT / "bin" / "situc"), "verify",
		 str(tmp_path / "p.situ"), str(tmp_path / "short")],
		capture_output=True, text=True, timeout=300)
	assert short.returncode != 0, short.stdout
	assert "2 of 2 vectors do not conform" in short.stderr + short.stdout, \
		short.stderr

	whole = subprocess.run(
		[sys.executable, str(ROOT / "bin" / "situc"), "verify",
		 str(tmp_path / "p.situ"), str(tmp_path / "whole")],
		capture_output=True, text=True, timeout=300)
	assert whole.returncode == 0, whole.stderr


#: hull's finding 16, reduced. The peek that broke it is inside ONE arm,
#: forty bytes in, and the message takes the other: nothing in `t` itself
#: reads byte 40, so a one-byte message is complete.
ARMS = """struct nothing {
}

struct big {
\tu8       x[40];
\tpeek u8  k2;
\tvariant  w switch (k2) {
\t\tcase 'm': u8       extra;
\t\tdefault:  nothing  none;
\t}
}

struct small {
\tu8  y;
}

struct t {
\tpeek u8  k;
\tvariant  v switch (k) {
\t\tcase 1:   big    b;
\t\tdefault:  small  s;
\t}
}
"""

#: And the run form: a peek in the ELEMENT must not make an empty run
#: impossible, because a run of no elements reads no element's bytes.
ELEMENTS = """struct nothing {
}

struct e {
\tu8        a;
\tpeek u32  k;
\tvariant   w switch (k) {
\t\tcase 1:   u8       extra;
\t\tdefault:  nothing  none;
\t}
}

struct r {
\tu8  o;
\te   items[] until ")";
}
"""


def _resolved(body: str):			# type: ignore[no-untyped-def]
	parsed = parse(Source("inline", "target buffer;\nendian big;\n\n"
	                      + body))
	return resolve(parsed, solve(parsed))


def test_a_peek_inside_an_arm_does_not_bind_the_whole_struct() -> None:
	"""`t` is one byte or forty-one, and its minimum is one.

	A peek inside an arm is read only when the data takes that arm, so
	counting it in the enclosing struct's minimum refuses a message of the
	OTHER arm. Before the fix `t` was 41 bytes flat and the map said
	`size=41` where it had said `1..41`.
	"""
	resolved = _resolved(ARMS)
	assert resolved.structs["t"].layout.size_bytes == 1
	# `big`'s own peek is still counted where it belongs: its last byte is
	# the one the peek reads, so a complete `big` is 41 rather than 40.
	assert resolved.structs["big"].layout.size_bytes == 41


def test_a_peek_in_a_run_element_does_not_bind_the_run_s_owner() -> None:
	"""`r` is one byte and a delimiter, whatever its elements hold.

	A run of no elements reads no element's bytes, so a `peek u32` inside
	the element made `()` impossible -- five bytes needed for a two-byte
	message.

	Two rather than one, and the second byte is the delimiter: a run that
	ends at `)` cannot be complete without it. Asserting one here was my
	own wrong expectation and this caught it.
	"""
	resolved = _resolved(ELEMENTS)
	assert resolved.structs["r"].layout.size_bytes == 2
	assert resolved.structs["e"].layout.size_bytes == 5
