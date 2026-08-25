"""`[min]`/`[max]` exported as per-field constants (26.125).

The bound is stated once in the schema and enforced in `validate`, and was
reachable from nowhere else -- so hand-written code validating the same value
before it crosses the wire (a CLI flag that fills a field, a config key)
restated the number and drifted. The argv evaluation (26.124) is where the
gap was noticed; the constants are the single-source rule applied to the
value domain.

The C spelling lives here; the other three backends' spellings are asserted
in their own codegen suites, and the executable agreement test -- the
module's own constants driving its own `validate` at all four boundary
values -- is in `test_codegen_python.py`, where a generated module can be
imported without a compiler.
"""

from __future__ import annotations

from situc.codegen.c import generate
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve

PREAMBLE = "target buffer;\nendian big;\nbit_order msb_first;\n"

#: A field named `size` on purpose: its constants must not collide with the
#: struct's own `SIZE_MIN`/`SIZE_MAX`, which is why the spelling is
#: `VALUE_MIN`/`VALUE_MAX` -- the value's domain, not the field's byte size.
FIXTURE = ("const CAP = 9216;\n"
           "struct s { u16 mtu [min = 576, max = CAP];"
           " i8 bias [min = -20]; u16 size [max = 100]; }")


def header(body: str) -> str:
	schema   = parse_text(PREAMBLE + body)
	resolved = resolve(schema, solve(schema))
	return generate(schema, resolved, "unit").header


def test_value_bounds_are_exported_as_macros() -> None:
	emitted = header(FIXTURE)
	assert "#define SITU_S_MTU_VALUE_MIN 576u" in emitted
	assert "#define SITU_S_MTU_VALUE_MAX 9216u" in emitted
	assert "#define SITU_S_BIAS_VALUE_MIN -20" in emitted
	assert "#define SITU_S_SIZE_VALUE_MAX 100u" in emitted


def test_the_size_field_does_not_collide_with_the_struct_size() -> None:
	"""The reason for the VALUE spelling, held as a test."""
	emitted = header(FIXTURE)
	assert "#define SITU_S_SIZE_MIN   5u" in emitted
	assert emitted.count("SITU_S_SIZE_VALUE_MAX") >= 1


def test_a_bound_that_does_not_fold_is_skipped() -> None:
	"""`validate` still enforces it; a constant that cannot be computed at
	compile time is not a constant. Skipped rather than refused, and the
	absence is discoverable -- code using the macro fails to compile --
	rather than silently wrong."""
	emitted = header("struct s { u8 n; u8 v [max = n]; }")
	assert "VALUE_MAX" not in emitted


def test_wrong_domains_are_excluded() -> None:
	"""Fixed point's getter is scaled and BCD's is decoded, so a raw bound
	would be a constant in the wrong domain -- worse than none."""
	emitted = header("struct s { q8_8 trim [max = 100]; bcd8 day [max = 49]; }")
	assert "VALUE_MAX" not in emitted
