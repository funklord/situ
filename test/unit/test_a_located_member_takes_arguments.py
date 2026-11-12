"""A member placed by `at` takes arguments (0068, 26.968).

A TIFF says its byte order in the header and puts its first directory
wherever `ifd_offset` says, and every entry of that directory is in the
header's order:

    struct ifd [endian = from(order)] {
        parameter byte_order order [stream];
        u16 count; ifd_entry(order = order) entries[count]; u32 next;
    }
    struct tiff [endian = from(byte_order)] {
        endian_marker byte_order; u16 magic [must_eq = 42]; u32 ifd_offset;
        ifd(order = byte_order) first at ifd_offset;
    }

A located member handed arguments was refused as unbuilt. It is the
located path (26.967) with the handed values threaded through: C's
extent and validate, C++'s `required` and constructor, Rust's `required`
and `new`, Python's `required` and `at`. And Rust built the elements of a
counted or fixed run of structs that take an argument without it, so a
directory's entries did not compile; the walked form had always handed
them on.

Directories here are laid out by hand from TIFF 6.0 section 2; the
values and verdicts are worked out from them.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

import fourway
from situc.cli import analyse
from situc.dissector import generate
from situc.parser import parse
from test_dissector import LUA, read_back
from test_walker_c import (COMPILER, c_verdict, packed_text, python_verdict,
                           shape_named_text)

SCHEMA = """target buffer;

endian_marker byte_order : u16 {
	little = 0x4949,
	big    = 0x4D4D,
}

struct ifd_entry [endian = from(order)] {
	parameter byte_order order [stream];
	u16 tag;
	u16 type [max = 12];
	u32 count;
	u32 value;
}

struct ifd [endian = from(order)] {
	parameter byte_order order [stream];
	u16 count;
	ifd_entry(order = order) entries[count];
	u32 next;
}

struct tiff [endian = from(byte_order)] {
	endian_marker  byte_order;
	u16            magic       [must_eq = 42];
	u32            ifd_offset;
	ifd(order = byte_order) first at ifd_offset;
}
"""

#: (tag, type, count, value): ImageWidth 3 and ImageLength 2, as LONGs.
ENTRIES = ((256, 4, 1, 3), (257, 4, 1, 2))


def tiff(order: str, entries: tuple[tuple[int, int, int, int], ...] = ENTRIES,
		count: int | None = None) -> bytes:
	"""A header, four bytes of padding, and the directory at offset 12."""
	marker = b"II" if order == "<" else b"MM"
	head   = marker + struct.pack(order + "HI", 42, 12) + bytes(4)
	body   = struct.pack(order + "H", len(entries) if count is None else count)
	for one in entries:
		body += struct.pack(order + "HHII", *one)
	return head + body + struct.pack(order + "I", 0)


#: The file, and its verdict: 0 OK, 1 BOUNDS, 2 CONSTRAINT.
CASES = {
	"little":                    (tiff("<"), "0"),
	"big":                       (tiff(">"), "0"),
	"an entry's type over 12":   (tiff("<", ((256, 13, 1, 3),)), "2"),
	"a type over 12, big":       (tiff(">", ((256, 13, 1, 3),)), "2"),
	"more entries than are here": (tiff("<", count=3), "1"),
	"the directory past the end": (tiff("<")[:12], "1"),
}


def test_all_four_read_the_directory(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for label, (packet, verdict) in CASES.items():
		given = {name: fourway.answers(argv, packet, tmp_path)
		         for name, argv in command.items()}
		first = next(iter(given.values()))
		assert all(answer == first for answer in given.values()), \
			(label, given)
		said = [one for one in first.splitlines()
		        if one.startswith("validate ")]
		assert said[-1] == f"validate {verdict}", (label, first)


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_both_walkers_read_the_directory(tmp_path: Path) -> None:
	blob  = packed_text(SCHEMA)
	shape = shape_named_text(SCHEMA, "tiff")
	for label, (packet, verdict) in CASES.items():
		assert python_verdict(blob, packet, shape) == verdict, label
		assert c_verdict(tmp_path, blob, packet, shape) == verdict, label


#: A fixed array of structs that take an argument: Rust built each element
#: without it.
FIXED = """target buffer;
endian big;
struct e { parameter u8 k [stream]; u8 a [max = k]; }
struct p { u8 k; e(k = k) es[2]; u8 z; }
"""


def test_a_fixed_array_hands_its_elements_the_argument(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(FIXED, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for packet, verdict in (("05010209", "0"),   # 1 and 2 under k = 5
	                        ("02010309", "2")):  # 3 over k = 2
		given = {name: fourway.answers(argv, bytes.fromhex(packet), tmp_path)
		         for name, argv in command.items()}
		first = next(iter(given.values()))
		assert all(answer == first for answer in given.values()), given
		said = [one for one in first.splitlines()
		        if one.startswith("validate ")]
		assert said[-1] == f"validate {verdict}", (packet, first)


@pytest.mark.skipif(LUA is None, reason="no Lua interpreter")
def test_the_dissector_shows_the_directory_where_it_is(tmp_path: Path) -> None:
	"""It measured the directory at the cursor -- byte 8, the end of the
	header -- rather than at `ifd_offset`, and declared no field for it, so
	the first file that reached it was a Lua error."""
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	source, resolved, _ = analyse(schema)
	script = tmp_path / "unit.lua"
	script.write_text(generate(parse(source), resolved, "unit"),
	                  encoding="ascii")
	for order in ("<", ">"):
		_, rows = read_back(script, "tiff", tiff(order))
		# Two entries: a count, 2 * 12 bytes of entries, a next offset.
		assert ("tiff.first", 12, 2 + 2 * 12 + 4) in \
			[(name, at, length) for name, at, length, _ in rows], rows


#: The entry's value as TIFF means it: a SHORT left-justified in the four
#: bytes, in the file's order. C's accessors for an array in an arm read
#: the handed order and did not take it, so this did not compile (26.970).
SHORTS = SCHEMA.replace("\tu32 value;\n", """\tvariant value switch (type) {
\t\tcase 3:  u16 shorts[2];
\t\tdefault: u32 long;
\t}
""")


def test_a_short_is_read_in_the_handed_order(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SHORTS, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for order in ("<", ">"):
		# Width 3 and length 2 as SHORTs: the value's first two bytes.
		entries = tuple((tag, 3, 1, (value << 16 if order == ">" else value))
		                for tag, value in ((256, 3), (257, 2)))
		packet  = tiff(order, entries)
		given = {name: fourway.answers(argv, packet, tmp_path)
		         for name, argv in command.items()}
		first = next(iter(given.values()))
		assert all(answer == first for answer in given.values()), given
		said = [one for one in first.splitlines()
		        if one.startswith("validate ")]
		assert said[-1] == "validate 0", (order, first)

		import sys
		sys.path.insert(0, str(tmp_path))
		try:
			import situ_runtime  # type: ignore[import-not-found]
			import unit  # type: ignore[import-not-found]
			msg  = situ_runtime.Message(bytearray(packet))
			head = unit.tiff.at(msg, 0)
			held = unit.ifd.at(msg, head.ifd_offset, len(head.first),
			                   arg_order=head.byte_order)
			assert [held.entries(i).value_shorts(0) for i in range(2)] \
				== [3, 2], order
		finally:
			sys.path.remove(str(tmp_path))
			sys.modules.pop("unit", None)
