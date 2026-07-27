"""`situc gen-dissector` -- a Wireshark dissector, in Lua (section 20.3).

There is no Lua interpreter in this environment and no Wireshark, so nothing
here executes the output. That is a real limit and worth stating plainly: these
tests check that the Lua is structurally sound and that the numbers in it are
the numbers the layout says, not that Wireshark accepts it.

The numbers are the part worth testing anyway. A dissector that loads and shows
the wrong bytes is worse than one that fails to load.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from situc.cli import analyse
from situc.dissector import generate
from situc.layout import solve
from situc.parser import parse, parse_text
from situc.resolve import resolve

ROOT     = Path(__file__).resolve().parent.parent.parent
EXAMPLES = sorted(ROOT.glob("examples/*/*.situ")) + sorted(ROOT.glob("tests/schemas/*.situ"))

PREAMBLE = "target buffer;\nendian big;\nbit_order msb_first;\n"

#: Lua's own words, which may not be used as an identifier. A schema is free to
#: name a field `end` or `function`; Lua is not.
LUA_KEYWORDS = frozenset({
	"and", "break", "do", "else", "elseif", "end", "false", "for", "function",
	"goto", "if", "in", "local", "nil", "not", "or", "repeat", "return",
	"then", "true", "until", "while",
})


def emit(body: str) -> str:
	schema   = parse_text(PREAMBLE + body)
	resolved = resolve(schema, solve(schema))
	return generate(schema, resolved, "unit")


def code(text: str) -> str:
	"""The Lua with comments stripped, which is what has to parse."""
	return "\n".join(line.split("--")[0] for line in text.splitlines())


def test_blocks_balance() -> None:
	"""An unbalanced `end` is the one error that makes the file useless, and it
	is easy to introduce: every emitted shape opens a block."""
	for path in EXAMPLES:
		source, resolved, _ = analyse(path)
		body = code(generate(parse(source), resolved, path.stem))

		# `do` is not counted: `for ... do` is one block with two keywords, and
		# no bare `do ... end` is emitted.
		opens = len(re.findall(r"\b(function|if|for|while)\b", body))
		ends  = len(re.findall(r"\bend\b", body))

		assert opens == ends, f"{path.name}: {opens} blocks opened, {ends} closed"


def test_locals_are_valid_lua_identifiers() -> None:
	for path in EXAMPLES:
		source, resolved, _ = analyse(path)
		body = code(generate(parse(source), resolved, path.stem))

		for name in re.findall(r"\blocal (\w+)", body):
			assert name.isidentifier(), f"{path.name}: `{name}` is not an identifier"
			assert name not in LUA_KEYWORDS, f"{path.name}: `{name}` is a Lua keyword"


def test_a_bit_packed_field_gets_its_byte_span_and_mask() -> None:
	"""The bug the first version had: a four-bit field is zero bytes wide if you
	divide, so every bit-packed field was silently dropped from the tree. They
	are read through the bytes they live in and masked."""
	text = emit("struct s { u4 version; u4 ihl; u8 tos; }\n")

	assert 'ProtoField.uint8("s.version", "version", base.DEC, nil, 0xf0)' in text
	assert 'ProtoField.uint8("s.ihl", "ihl", base.DEC, nil, 0xf)' in text
	assert "subtree:add(s_f.version, tvb(0, 1))" in text
	assert "subtree:add(s_f.ihl, tvb(0, 1))" in text


def test_a_straddling_field_is_read_through_both_bytes() -> None:
	"""IPv4's fragment offset: 13 bits starting three bits into a byte, so it
	is a 16-bit read with a 13-bit mask at the bottom."""
	text = emit("""struct s [allow_straddle] {
		bit a; bit b; bit c; u13 fragment_offset;
	}
	""")

	assert 'ProtoField.uint16("s.fragment_offset"' in text
	assert "0x1fff)" in text
	assert "subtree:add(s_f.fragment_offset, tvb(0, 2))" in text


def test_little_endian_fields_use_add_le() -> None:
	"""Wireshark reads big endian by default; the other way needs saying."""
	assert "subtree:add(s_f.a, tvb(0, 2))" in emit("struct s { u16 a; }\n")

	schema   = parse_text("target buffer;\nendian little;\nbit_order lsb_first;\n"
	                      "struct s { u16 a; }\n")
	resolved = resolve(schema, solve(schema))

	assert "subtree:add_le(s_f.a, tvb(0, 2))" in generate(schema, resolved, "unit")


def test_an_enum_becomes_a_value_string() -> None:
	"""So Wireshark shows `tcp`, not `6`, which is the whole reason to bother."""
	text = emit("""enum protocol : u8 { icmp = 1, tcp = 6, udp = 17 }
	struct s { protocol proto; }
	""")

	assert 'local protocol_values = { [1] = "icmp", [6] = "tcp", [17] = "udp" }' in text
	assert "protocol_values)" in text


def test_a_struct_array_is_dissected_element_by_element() -> None:
	"""A repeated record shown as a run of bytes tells a reader nothing the hex
	pane would not."""
	text = emit("""struct r { u32 id; u16 kind; }
	struct s { u8 n; r recs[4]; }
	""")

	assert "for i = 0, recs_n - 1 do" in text
	assert 'Dissector.get("r"):call(tvb(at, 6):tvb(), pinfo, subtree)' in text
	assert "at = at + 6" in text


def test_a_dynamic_count_is_read_from_the_field_that_drives_it() -> None:
	"""The same arithmetic the generated C offset functions do."""
	text = emit("""struct h { u8 v; u16 n; }
	struct r { u32 id; }
	struct s { h hdr; r recs[hdr.n]; }
	""")

	assert "local recs_n = tvb(1, 2):uint()" in text
	assert "for i = 0, recs_n - 1 do" in text


def test_remaining_runs_to_the_end_of_the_frame() -> None:
	text = emit("struct s { u8 head; u8 rest[remaining]; }\n")

	assert "subtree:add(s_f.rest, tvb(at))" in text
	assert "at = tvb:len()" in text


def test_a_reserved_field_loses_its_synthetic_brackets() -> None:
	"""`<reserved0>` is the compiler's own name for it, and not one Wireshark
	will take in an abbrev."""
	text = emit("struct s { u4 a; reserved u4 [must_be_zero]; }\n")

	# `<` on its own is Lua's less-than, which the bounds check uses.
	assert "<reserved" not in text
	assert 'ProtoField.uint8("s.reserved0", "reserved0"' in text


def test_registers_are_not_dissected() -> None:
	"""A register is a bus transaction. It does not appear on a wire, so a
	dissector for one would be describing something that never arrives."""
	source, resolved, _ = analyse(ROOT / "examples" / "registers" / "registers.situ")
	text = generate(parse(source), resolved, "registers")

	assert "Not dissected: ctrl_reg, status_reg" in text
	assert "Proto(" not in text


def test_the_registration_hint_names_the_outermost_struct() -> None:
	"""`record` is not the protocol; `message` is. Pointing a reader at an inner
	struct sends them to bind a dissector for something that only ever appears
	inside another."""
	source, resolved, _ = analyse(ROOT / "examples" / "message" / "message.situ")
	text = generate(parse(source), resolved, "message")

	assert text.rstrip().endswith(":add(9999, message)")


def test_offsets_match_the_layout() -> None:
	"""The claim worth checking: every `tvb(first, count)` in the file is a span
	some member actually occupies. A dissector that loads and shows the wrong
	bytes is worse than one that fails to load."""
	for path in EXAMPLES:
		source, resolved, _ = analyse(path)
		text = generate(parse(source), resolved, path.stem)

		spans = set()
		for struct in resolved.structs.values():
			for entry in struct.entries:
				placement = entry.placement
				if placement.offset_bits is None or not placement.size_bits:
					continue
				first = placement.offset_bits // 8
				last  = (placement.offset_bits + placement.size_bits - 1) // 8
				spans.add((first, last - first + 1))

		for first, count in re.findall(r"tvb\((\d+), (\d+)\)", text):
			assert (int(first), int(count)) in spans, \
				f"{path.name}: tvb({first}, {count}) is not any member's span"


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_every_example_generates(path: Path) -> None:
	source, resolved, _ = analyse(path)
	text = generate(parse(source), resolved, path.stem)

	assert text.endswith("\n")
	assert text.startswith("-- Generated by situc")
