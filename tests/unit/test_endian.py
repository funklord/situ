"""Byte order: where it is decided, and by whom (project.md section 8.3).

Two questions this file exists to pin down.

**When does a directive take effect?** From where it is written. It used to be
file-wide with the last one winning, so `endian native;` on line 3 silently
rewrote the struct on line 2 -- and both readings produce a valid layout, so
nothing could have caught it.

**Whose byte order is `native`?** The machine the generated code is compiled
for, decided by the C compiler. Not the machine that ran situc: those differ on
every cross build, and a generator that baked in its own answer would emit code
that reads the wrong bytes on the target without a word of complaint.
"""

from __future__ import annotations

import pytest

from situc import ast
from situc.capability import Axis, Value
from situc.codegen.c import generate
from situc.layout import solve
from situc.parser import parse_text
from situc.propagate import Resolved
from situc.resolve import resolve


def entries(body: str) -> dict[str, Resolved]:
	schema   = parse_text(body)
	resolved = resolve(schema, solve(schema))
	return {entry.placement.path: entry
	        for struct in resolved.structs.values()
	        for entry in struct.entries}


def header(body: str) -> str:
	schema   = parse_text(body)
	resolved = resolve(schema, solve(schema))
	return generate(schema, resolved, "unit").header


def endian_of(body: str, path: str) -> ast.Endian | None:
	return entries(body)[path].placement.endian


# -- when a directive takes effect ------------------------------------------


POSITIONAL = """endian big;
struct first { u16 x; }
endian little;
struct second { u16 y; }
"""


def test_a_directive_applies_from_where_it_is_written() -> None:
	assert endian_of(POSITIONAL, "first.x") is ast.Endian.BIG
	assert endian_of(POSITIONAL, "second.y") is ast.Endian.LITTLE


def test_a_later_directive_does_not_reach_backwards() -> None:
	"""The bug this test exists for.

	`endian native` used to apply file-wide, so the struct above it became
	host-order and failed the `[allow_host_dependent]` check it had no reason
	to need.
	"""
	held = entries("""endian big;
	struct first { u16 x; }
	endian native;
	struct second [allow_host_dependent] { u16 y; }
	""")

	assert held["first.x"].placement.endian is ast.Endian.BIG
	assert held["first.x"].vector.get(Axis.CANONICAL) == Value("Canonical")
	assert held["second.y"].vector.get(Axis.CANONICAL) == Value("NonCanonical")


def test_one_file_may_describe_layers_that_disagree() -> None:
	"""Which is the point of positional scoping, beyond fixing the bug.

	A protocol whose outer framing is network order and whose payload is a
	little-endian device record is one description, not two files.
	"""
	generated = header(POSITIONAL)

	assert "situ_get_be16(view.base + 0u)" in generated
	assert "situ_get_le16(view.base + 0u)" in generated


def test_a_struct_attribute_still_overrides_the_directive_in_force() -> None:
	held = entries("""endian big;
	struct wire { u16 x; }
	struct record [endian = little] { u16 y; }
	""")
	assert held["record.y"].placement.endian is ast.Endian.LITTLE
	assert held["wire.x"].placement.endian is ast.Endian.BIG


# -- whose byte order `native` means ----------------------------------------


NATIVE = """endian native;
struct shared [allow_host_dependent] {
	u16 half;
	u32 word;
	u64 wide;
	u24 odd;
}
"""


@pytest.mark.parametrize("call", [
	"situ_get_ne16", "situ_get_ne32", "situ_get_ne64",
	"situ_put_ne16", "situ_put_ne32", "situ_put_ne64",
	"situ_bits_get_ne", "situ_bits_set_ne",
])
def test_native_defers_to_the_compiler_rather_than_resolving_here(call: str) -> None:
	assert call in header(NATIVE)


def test_no_fixed_order_is_baked_into_a_native_accessor() -> None:
	"""The cross-compilation bug, stated as a property.

	situc runs on the machine building the code and not on the machine running
	it. Resolving `native` here would produce output that is correct only when
	those two agree, and silently wrong the rest of the time.
	"""
	generated = header(NATIVE)

	for fixed in ("situ_get_be16", "situ_get_le16", "situ_get_be32",
	              "situ_get_le32", "situ_bits_get_msb", "situ_bits_get_lsb"):
		assert fixed not in generated, f"`native` resolved to {fixed} at generation time"


def test_an_endian_marker_reads_the_same_host_constant() -> None:
	"""One decision point, not two.

	The marker's host constant and the `ne` accessors have to agree, or a
	writer would stamp one order into the marker and encode its fields in the
	other.
	"""
	generated = header("""endian big;
	endian_marker byte_order : u16 { little = 0x4949, big = 0x4D4D, }
	struct tiff [endian = from(byte_order)] {
		endian_marker byte_order;
		u16 magic;
	}
	""")

	assert "#if SITU_HOST_BIG" in generated
	assert "__BYTE_ORDER__" not in generated
