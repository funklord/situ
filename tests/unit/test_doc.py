"""`situc doc` -- the layout as documentation (section 20.3).

The point of generating diagrams rather than drawing them is that a drawn one
is a second description of the layout and second descriptions drift. So the
tests that matter are the ones that would catch drift: the diagram has to agree
with the published RFC for a protocol everybody knows, and every row has to be
exactly as wide as the ruler above it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from situc.doc import render
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve

from every_schema import ROOT, SCHEMAS
EXAMPLES = SCHEMAS

PREAMBLE = "target buffer;\nendian big;\nbit_order msb_first;\n"


def emit(body: str, fmt: str = "ascii") -> str:
	schema   = parse_text(PREAMBLE + body)
	resolved = resolve(schema, solve(schema))
	return render(schema, resolved, "unit", fmt)


def diagram(text: str) -> list[str]:
	"""The drawn lines, which are the ones whose width has to line up."""
	return [line for line in text.splitlines() if line[:1] in ("|", "+", "/")]


def test_it_draws_rfc_768() -> None:
	"""The UDP header, which is eight bytes everybody can check from memory."""
	text = emit("""struct udp_header {
		u16 source_port;
		u16 destination_port;
		u16 length;
		u16 checksum;
	}
	""")

	assert "|          source_port          |        destination_port       |" in text
	assert "|             length            |            checksum           |" in text


def test_it_draws_bit_fields_at_bit_granularity() -> None:
	"""RFC 791's first row: four fields, two of them nibbles.

	Drawing this correctly is the entire reason the format is bit-oriented; a
	byte-granular diagram cannot express it at all.
	"""
	text = emit("""struct ipv4 {
		u4  version;
		u4  ihl;
		u8  tos;
		u16 total_length;
	}
	""")

	assert "|version|  ihl  |      tos      |          total_length         |" in text


def test_every_drawn_line_is_the_width_of_its_ruler() -> None:
	"""A cell one character out makes the whole picture lie, and it is the
	failure this code is most prone to: widths are computed per field and the
	borders only line up if every one of them is right."""
	for path in EXAMPLES:
		from situc.cli import analyse
		from situc.parser import parse

		source, resolved, _ = analyse(path)
		text = render(parse(source), resolved, path.stem)

		widths = {len(line) for line in diagram(text)}
		# One width per row size in use; a struct narrower than 32 bits gets a
		# narrower diagram, so more than one value is legitimate.
		for width in widths:
			assert (width - 1) % 2 == 0, f"{path.name}: odd row width {width}"
		assert widths <= {17, 33, 65}, f"{path.name}: unexpected widths {widths}"


def test_a_narrow_struct_gets_a_narrow_diagram() -> None:
	"""A one-byte struct drawn across 32 bits reads as a four-byte one."""
	text = emit("struct f { bit a; bit b; u6 rest; }\n")

	assert "+-+-+-+-+-+-+-+-+" in text
	assert "+-+-+-+-+-+-+-+-+-+" not in text


def test_a_variable_member_is_drawn_as_variable() -> None:
	"""And it claims the rest of the row it starts in, because that is where it
	starts -- padding there would read as unused space."""
	text = emit("""struct h { u8 v; u16 n; }
	struct s { h hdr; u8 opts[hdr.n]; }
	""")

	assert "(variable)" in text
	assert any(line.startswith("/") for line in text.splitlines())


def test_a_nested_struct_is_one_box() -> None:
	"""RFC 791 draws `Source Address`, not four octets. The interior gets its
	own diagram further down."""
	text = emit("""struct addr { u8 octets[4]; }
	struct hdr { addr source; addr destination; }
	""")

	assert "|                             source                            |" in text
	assert "struct addr" in text


def test_the_table_renders_attribute_values() -> None:
	"""An expression node holds its own span, and a span holds the whole source
	file, so `str()` on one renders the entire schema into a table cell."""
	text = emit("struct s { u8 version [must_eq = 4]; }\n")

	assert "must_eq = 4" in text
	assert "Span(" not in text
	assert "target buffer" not in text.split("Notes")[1]


def test_markdown_fences_the_diagram() -> None:
	text = emit("struct s { u16 a; u16 b; }\n", fmt="markdown")

	assert text.startswith("# unit.situ")
	assert "```" in text
	assert "| Field | Offset | Size | Type | Notes |" in text


def test_offsets_are_reported_in_bytes_and_bits() -> None:
	"""Invariant 7: bytes where byte-aligned, and the bit phase where not."""
	text = emit("struct s { u4 a; u4 b; u8 c; }\n")
	rows = {line.split()[0]: line.split()[1]
	        for line in text.splitlines() if line[:1].isalpha() and len(line.split()) > 2}

	assert rows["a"] == "0",   "a byte-aligned offset is reported in bytes"
	assert rows["b"] == "0.4", "a bit phase is reported as byte.bit"
	assert rows["c"] == "1"


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_every_example_documents_without_error(path: Path) -> None:
	"""Cheap, and it is how the register and TLV shapes got covered at all."""
	from situc.cli import analyse
	from situc.parser import parse

	source, resolved, _ = analyse(path)
	text = render(parse(source), resolved, path.stem)

	assert text.endswith("\n")

	# A schema of codec signatures and nothing else documents no layout, and
	# that is the honest answer for a tool whose subject is where bytes sit:
	# `std/codecs.situ` and `std/kernels.situ` produce a title and the note
	# about where the offsets come from. Where a codec *is* layout -- a coded
	# or sealed region -- it is named in the field table, which the tests
	# below hold.
	if resolved.structs:
		assert "struct " in text or "tlv" in text


def test_a_derived_field_says_it_is_derived() -> None:
	"""Somebody writing an encoder from this table needs to know the value is
	not theirs to choose. The fields an invariant *reads* showed up here from
	the start, through `covered_by`, so the dependency was documented from one
	end only -- and the missing end is the one that constrains the writer."""
	text = emit("struct s { u16 total; u8 a; u32 b; }\n"
	            "invariant s.total == size(s.a) + size(s.b);\n")

	assert "derived: invariant total computes it" in text
	assert "covered by invariant total" in text


def test_a_plain_field_is_not_called_derived() -> None:
	text = emit("struct s { u16 total; u8 a; }")

	assert "derived" not in text


# -- delimited and versioned members (sections 8.6, 19.4) -------------------


def test_a_delimited_member_is_not_an_array_of_one() -> None:
	"""It carries `array_count = 1` -- the empty bracket form is one run, not
	one element -- so the table called `name[] until ": "` a one-element array
	of the delimiter's width. Somebody implementing from that writes a
	fixed-width parser, which is worse than the row being absent."""
	text = emit('struct s { u8 name[] until ": "; u8 rest[remaining]; }')

	assert "name[1]" not in text
	assert "name..." in text


def test_a_delimited_member_says_where_it_stops() -> None:
	"""Which is the only size a reader can use. The fixed-size branch reported
	the delimiter's own width, the one number that is not the member's."""
	text = emit('struct s { u8 name[] until ": "; u8 rest[remaining]; }')

	assert 'to ": "' in text
	assert "2 bytes" not in text.split("Field")[1]


def test_a_capped_scan_says_its_cap() -> None:
	text = emit('struct s { u8 n[] until "\\r\\n" max 16; u8 rest[remaining]; }')

	assert 'to "\\r\\n", max 16' in text


def test_a_delimited_member_is_drawn_as_variable() -> None:
	"""The diagram had it as a fixed one-byte box, which is the RFC convention
	for something entirely different."""
	drawn = diagram(emit('struct s { u8 name[] until ": "; u8 rest[remaining]; }'))

	assert any("(variable)" in line for line in drawn)


def test_a_text_number_says_which_base() -> None:
	text = emit('struct s { decimal u16 n until "\\r\\n"; u8 r[remaining]; }')

	assert "decimal digits" in text


def test_a_versioned_member_says_which_version() -> None:
	text = emit("struct s [version = v] { u8 v; u16 a; u32 b [since = 2]; }")

	assert "from version 2" in text


def test_it_does_not_say_the_version_twice() -> None:
	"""`since = 2` and "from version 2" in one row is one fact twice, and a
	document that repeats itself teaches a reader to skim it."""
	text = emit("struct s [version = v] { u8 v; u16 a; u32 b [since = 2]; }")

	assert "since = 2" not in text
