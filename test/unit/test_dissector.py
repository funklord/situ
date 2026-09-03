"""`situc gen-dissector` -- a Wireshark dissector, in Lua (section 20.3).

Two kinds of test. The structural ones read the emitted text and check that the
numbers in it are the numbers the layout says. The rest *run* it: a Lua
interpreter parses every dissector this repository can generate, and four of
them are executed over real packet bytes through the Wireshark stub in
`test/lua/dissect.lua`.

And every one of them is now run over *random* bytes with its answers compared
against the walker's, which is a third kind: `walker/report.listing` computes
what the schema says about any buffer, so the chosen packets are no longer the
only ones with a right answer.

What is still not checked is that Wireshark accepts the plugin -- there is no
Wireshark here, and the stub is a stand-in for its API rather than a copy of
it. What is checked now is the dissection itself, which is the half that
produces wrong answers rather than an error message: a dissector that loads and
shows the wrong bytes is worse than one that fails to load.
"""

from __future__ import annotations

import random
import re
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

import pytest

from every_schema import SCHEMAS, ids
from situc import pack as packer
from situc.cli import analyse
from situc.dissector import generate
from situc.layout import solve
from situc.parser import parse, parse_text
from situc.resolve import resolve
from walker import report
from walker.image import Image, load

from every_schema import ROOT, SCHEMAS
EXAMPLES = SCHEMAS

PREAMBLE = "target buffer;\nendian big;\nbit_order msb_first;\n"

LUA     = shutil.which("lua5.4") or shutil.which("lua")
LUAC    = shutil.which("luac5.4") or shutil.which("luac")
HARNESS = ROOT / "test" / "lua" / "dissect.lua"

#: Lua's own words, which may not be used as an identifier. A schema is free to
#: name a field `end` or `function`; Lua is not. Imported from the generator
#: rather than copied: the list that decides what to rename and the list that
#: checks nothing was missed are one list, or they drift (invariant 34).
from situc.dissector import LUA_KEYWORDS


def emit(body: str) -> str:
	schema   = parse_text(PREAMBLE + body)
	resolved = resolve(schema, solve(schema))
	return generate(schema, resolved, "unit")


def code(text: str) -> str:
	"""The Lua with comments stripped, which is what has to parse."""
	return "\n".join(line.split("--")[0] for line in text.splitlines())


#: Operators Lua 5.3 introduced. None of them parses in 5.2 at all -- they are
#: syntax errors rather than different meanings -- so any occurrence outside a
#: comment or a string is a dissector older Wireshark cannot load.
#:
#: `~=` is excluded because it is 5.2's "not equal"; only a lone `~` is the
#: 5.3 bitwise operator. A bare `/` is excluded deliberately: it is float
#: division in every Lua and the emitter uses it inside `math.floor`, which is
#: exactly the spelling decision 0021 asks for.
_LUA_53_ONLY = re.compile(r"//|<<|>>|&|\||~(?!=)")


def _without_strings_or_comments(text: str) -> str:
	"""Lua source with string literals and comments blanked out.

	Blanked rather than deleted, so reported line and column numbers still
	line up with the file a reader will open.

	Strings go first and comments second, because a `--` inside a string
	starts no comment -- and stripping comments first would truncate the line
	and hide anything after it, which is a false negative in a check whose
	whole job is to notice something. Neither case occurs in the emitted Lua
	today; the order costs nothing and stops that being load-bearing.
	"""
	out, i, n = [], 0, len(text)
	while i < n:
		ch = text[i]
		if ch in "\"'":
			quote = ch
			out.append(" ")
			i += 1
			while i < n and text[i] != quote:
				if text[i] == "\\":
					out.append("  ")
					i += 2
					continue
				out.append("\n" if text[i] == "\n" else " ")
				i += 1
			out.append(" ")
			i += 1
			continue
		if text.startswith("--", i):
			while i < n and text[i] != "\n":
				out.append(" ")
				i += 1
			continue
		out.append(ch)
		i += 1
	return "".join(out)


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_no_dissector_uses_an_operator_lua_52_lacks(path: Path) -> None:
	"""Decision 0021's constraint, which nothing was checking.

	0021 targets the Lua 5.2 that older Wireshark bundles, and it is why
	`align_up` is spelled with `math.floor` rather than `//`: a dissector
	that fails to load is worse than one that divides in floating point.

	`_UNSPELLABLE` in the generator guards *schema expressions* before they
	are translated, and guards them well. It says nothing about the Lua the
	emitter writes around them, so a hand-edit spelling `//` or `<<` in the
	boilerplate would pass every test here: `test_every_dissector_parses`
	runs `luac`, and the `luac` on this machine is 5.4, where all of these
	are perfectly legal. A check that cannot fail for the thing it exists to
	catch is worse than none, because it gets quoted afterwards as though it
	had.

	The measurement that scoped this: across 33 generated dissectors and
	7610 lines, every occurrence of these operators is inside a comment
	quoting a schema expression the emitter declined -- so the check is
	comment-aware because it has to be, not defensively.
	"""
	source, resolved, _ = analyse(path)
	lua  = generate(parse(source), resolved, path.stem)
	bare = _without_strings_or_comments(lua)

	found = sorted({match.group(0) for match in _LUA_53_ONLY.finditer(bare)})
	assert found == [], (
		f"{path.stem}.lua uses {found}, which Lua 5.2 cannot parse "
		f"(decision 0021)")


@pytest.mark.skipif(LUAC is None, reason="no Lua compiler")
@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_every_dissector_parses(path: Path, tmp_path: Path) -> None:
	"""A Lua parser, over every dissector this repository can generate.

	`test_blocks_balance` counted `function`/`if`/`for`/`while` against `end`
	and said in its own docstring that it was not a Lua parser -- the cheapest
	guard available when nothing here could parse Lua. Something can now, so
	the proxy is retired: `luac -p` accepts or rejects the file the way the
	interpreter loading it will.
	"""
	source, resolved, _ = analyse(path)
	lua = tmp_path / f"{path.stem}.lua"
	lua.write_text(generate(parse(source), resolved, path.stem), encoding="ascii")

	assert LUAC is not None
	result = subprocess.run([LUAC, "-p", str(lua)],
	                        capture_output=True, text=True)
	assert result.returncode == 0, result.stderr


def dissect(tmp_path: Path, path: Path, proto: str,
		packet: bytes) -> tuple[int, list[tuple[str, int, int, str]]]:
	"""Run a generated dissector over real bytes, and read back what it showed.

	Rows are `(field, offset, length, value)`, offsets absolute in the packet
	even where a nested dissector was handed a sub-range -- which is what makes
	them comparable with the layout the accessors come from.
	"""
	source, resolved, _ = analyse(path)
	lua = tmp_path / f"{path.stem}.lua"
	lua.write_text(generate(parse(source), resolved, path.stem), encoding="ascii")

	return read_back(lua, proto, packet)


def read_back(lua: Path, proto: str,
		packet: bytes) -> tuple[int, list[tuple[str, int, int, str]]]:
	"""The same, for a dissector already written out.

	Split off so that a sweep over many packets writes the Lua once. `analyse`
	and `generate` are the expensive half and neither depends on the bytes.
	"""
	assert LUA is not None
	result = subprocess.run(
		[LUA, str(HARNESS), str(lua), proto, packet.hex()],
		capture_output=True, text=True)
	assert result.returncode == 0, result.stderr

	lines    = result.stdout.splitlines()
	consumed = int(lines[0].split("\t")[1])
	rows     = []
	for line in lines[1:]:
		field, offset, length, value = line.split("\t")
		rows.append((field, int(offset), int(length), value))
	return consumed, rows


def test_the_blocks_balance() -> None:
	"""Kept as the cheap one, and it runs where no Lua is installed."""
	for path in EXAMPLES:
		source, resolved, _ = analyse(path)
		body = code(generate(parse(source), resolved, path.stem))

		# String literals go too. This counts keywords, and a schema is free
		# to name a field `function` -- Modbus does, section 4.1 -- which
		# reaches the output as a `ProtoField` abbrev and display name, both
		# of them strings. The generator renames the *identifier* and must
		# not touch what Wireshark shows, so the two `function`s on that line
		# are correct and are not blocks.
		plain = re.sub(r'"[^"]*"', '""', body)

		# `do` is not counted: `for ... do` is one block with two keywords, and
		# no bare `do ... end` is emitted.
		opens = len(re.findall(r"\b(function|if|for|while)\b", plain))
		ends  = len(re.findall(r"\bend\b", plain))

		assert opens == ends, f"{path.name}: {opens} blocks opened, {ends} closed"


def test_locals_are_valid_lua_identifiers() -> None:
	for path in EXAMPLES:
		source, resolved, _ = analyse(path)
		body = code(generate(parse(source), resolved, path.stem))

		# `local function f()` is a declaration too, and its name is the
		# second word. Reading only the first found `function`, which is a
		# keyword and is not the thing being named.
		for name in re.findall(r"\blocal (?:function )?(\w+)", body):
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
	source, resolved, _ = analyse(ROOT / "example" / "register" / "register.situ")
	text = generate(parse(source), resolved, "registers")

	assert "Not dissected: ctrl_reg, irq_reg, status_reg" in text
	assert "Proto(" not in text


def test_the_registration_hint_names_the_outermost_struct() -> None:
	"""`record` is not the protocol; `message` is. Pointing a reader at an inner
	struct sends them to bind a dissector for something that only ever appears
	inside another."""
	source, resolved, _ = analyse(ROOT / "example" / "message" / "message.situ")
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


# -- run it (26.14, and what section 22 said it did not do) -----------------


@pytest.mark.skipif(LUA is None, reason="no Lua interpreter")
def test_a_dissector_shows_the_fields_a_real_packet_holds(tmp_path: Path) -> None:
	"""The first generated Lua in this repository ever executed.

	A UDP header off the wire: source 1234, destination 80, length 24. Every
	row is a field the dissector *showed*, at the offset it showed it at, so
	this is the layout arriving in the one artifact nothing had run."""
	consumed, rows = dissect(
		tmp_path, ROOT / "example" / "udp" / "udp.situ", "udp_header",
		bytes.fromhex("04d2005000180000"))

	assert consumed == 8
	assert rows == [
		("udp_header.source_port",      0, 2, "1234"),
		("udp_header.destination_port", 2, 2, "80"),
		("udp_header.length",           4, 2, "24"),
		# Two bytes rather than the number `0`: the field is a `checksum`
		# now and not a plain `u16` (14.2a), and a checksum carries a length
		# by construction -- situ says which bytes it covers and never what
		# they mean as an integer, so the dissector shows the bytes.
		("udp_header.checksum",         6, 2, "0000"),
	]


@pytest.mark.skipif(LUA is None, reason="no Lua interpreter")
def test_a_member_sized_by_arithmetic_is_walked(tmp_path: Path) -> None:
	"""`payload[length - 8]`. `sized_by` holds a path and holds nothing for
	arithmetic over one, and both places this file asked the question asked it
	that way -- so UDP's payload was declared as a field, never shown, and the
	member was reported as "sized by `None`, which this dissector cannot
	locate". Its driver is `length`, four bytes back.

	The same packet as above with the payload actually present: length 24 is
	eight of header and sixteen of payload."""
	packet = bytes.fromhex("04d2005000180000") + bytes(range(16))
	consumed, rows = dissect(
		tmp_path, ROOT / "example" / "udp" / "udp.situ", "udp_header", packet)

	assert consumed == 24
	assert rows[-1][:3] == ("udp_header.payload", 8, 16)


@pytest.mark.skipif(LUA is None, reason="no Lua interpreter")
def test_a_dissector_consumes_no_more_than_the_frame(tmp_path: Path) -> None:
	"""A length is the message's claim, not the frame's. The `if` above the
	advance already declined to show bytes that are not there and the advance
	counted them anyway, so an eight-byte header declaring 24 reported having
	consumed 24 -- past the end of what it was handed."""
	consumed, _ = dissect(
		tmp_path, ROOT / "example" / "udp" / "udp.situ", "udp_header",
		bytes.fromhex("04d2005000180000"))

	assert consumed == 8


def test_an_operator_lua_spells_differently_is_declined() -> None:
	"""Lua's `/` is floating point and its `^` is exponentiation, and `<<`,
	`>>`, `&` and `|` arrived in 5.3 -- which decision 0021 says this backend
	cannot assume. Nothing reached any of them while every member sized by
	arithmetic was declined for a different reason; the first packet through
	the fixed version died on `tvb(at, 2.5)`.

	`^` is the one that would not have died: `(units ^ 1) + 1` is a number Lua
	computes happily and nobody meant."""
	text = emit("struct s { u8 n [min = 1, max = 4];"
	            " u8 half[n / 2 + 1]; u8 flip[(n ^ 1) + 1];"
	            " u8 sum[n + 1]; u16 tail; }\n")

	assert "sized by `n / 2 + 1`" in text
	assert "sized by `(n ^ 1) + 1`" in text
	assert "local sum_n = situ_uint(tvb, 0, 1, false) + 1" in text	# and the rest still walks


#: The first entry of the archive `example/cpio/cpio.vectors` records, which
#: GNU cpio wrote. 110 bytes of header, a 13-byte name, one byte of padding,
#: six bytes of file and two more of padding.
CPIO_ENTRY = bytes.fromhex(
	"30373037303130303142313132393030303038314234303030303033453830303030303345383030303030303031364137304241424230303030303030363030303030303030303030303030324230303030303030303030303030303030303030303030304430303030303030306772656574696e672e747874000068656c6c6f0a0000")


@pytest.mark.skipif(LUA is None, reason="no Lua interpreter")
def test_a_dissector_walks_a_format_written_in_digits(tmp_path: Path) -> None:
	"""Every number in a cpio header is ASCII, and three things went wrong.

	The bracket of `hex u32 ino[8]` is a width and was read as a count, so
	every header field was declared four times too wide and overlapped the
	ones after it. The name length was read with `uint()` over eight
	characters -- a number nobody wrote, and one Wireshark refuses outright
	above four bytes. And the padding between the name and the data was
	reported as "no bytes of its own", so the walk finished three bytes short
	of the entry it had just read (26.42).

	The offsets below are GNU cpio's, and the last of them is the one that
	only comes out right if all three are fixed.
	"""
	consumed, rows = dissect(
		tmp_path, ROOT / "example" / "cpio" / "cpio.situ", "cpio_entry",
		CPIO_ENTRY)

	assert consumed == len(CPIO_ENTRY)

	shown = {name: (offset, length) for name, offset, length, _ in rows}
	assert shown["cpio_entry.name"]      == (110, 13)
	assert shown["cpio_entry.reserved0"] == (123, 1)
	assert shown["cpio_entry.data"]      == (124, 6)
	assert shown["cpio_entry.reserved1"] == (130, 2)

	# ...and the header's own fields are eight bytes each, not thirty-two.
	assert shown["cpio_header.ino"]      == (6, 8)
	assert shown["cpio_header.namesize"] == (94, 8)


@pytest.mark.skipif(LUA is None, reason="no Lua interpreter")
def test_bit_packed_fields_come_out_masked_and_shifted(tmp_path: Path) -> None:
	"""`45 00 ...` is version 4 and IHL 5 in one byte, which is the read a
	dissector gets wrong by showing 0x45 twice.

	The mask is in the `ProtoField`, so this is Wireshark's arithmetic rather
	than the generator's -- and checking it means doing what Wireshark does,
	which the stub does and the text tests could not."""
	consumed, rows = dissect(
		tmp_path, ROOT / "example" / "ipv4" / "ipv4.situ", "ipv4_header",
		bytes.fromhex("450000280001000040110000c0a80001c0a80002"))

	assert consumed == 20
	assert ("ipv4_header.version", 0, 1, "4") in rows
	assert ("ipv4_header.ihl",     0, 1, "5") in rows
	assert ("ipv4_header.time_to_live", 8, 1, "64") in rows
	assert ("ipv4_header.protocol",     9, 1, "17") in rows

	# The nested struct is dissected twice, at both addresses.
	assert ("ipv4_address.octets", 12, 4, "c0a80001") in rows
	assert ("ipv4_address.octets", 16, 4, "c0a80002") in rows


@pytest.mark.skipif(LUA is None, reason="no Lua interpreter")
def test_a_condition_keeps_its_logical_operators() -> None:
	"""`&&` and `||` are not operators Lua spells differently -- they are
	rendered as `and` and `or`. Declining them declined every `while` run in
	the repository, which is what the first version of the guard above did."""
	text = emit(DNS_LABEL)

	# Anchored on the operators rather than on the bracketing: the emitted
	# condition is parenthesised at every operator, because the same text
	# reaches four host compilers whose precedence tables are not all
	# situ's, and counting brackets here would test the renderer instead.
	assert "(situ_uint(tvb, last, 1, false) % 64)" in code(text)
	assert " and " in code(text) and "&&" not in code(text)


@pytest.mark.skipif(LUA is None, reason="no Lua interpreter")
def test_a_dns_name_is_walked_label_by_label(tmp_path: Path) -> None:
	"""`www.example.com`, then qtype and qclass.

	Three constructs at once, and each of them was only ever read as text
	before: a run walked by a computed extent, a variant whose arm the
	discriminant selects, and a nested dissector called over a sub-range.

	The root label is the point. It is `form = 0, rest = 0`, so its extent
	runs through `a and b or c` with `b` zero -- the idiom section 22 named as
	the one semantic dependency riding on Lua's truthiness. Executed, it
	advances by one byte and the question's fields land at 17 and 19, which is
	where the layout puts them."""
	consumed, rows = dissect(
		tmp_path, ROOT / "example" / "dnsname" / "dnsname.situ", "question",
		bytes.fromhex("03777777076578616d706c6503636f6d0000010001"))

	assert consumed == 21
	assert rows == [
		("label.form",      0, 1, "0"),
		("label.rest",      0, 1, "3"),
		("label.body.text", 1, 3, "777777"),
		("label.form",      4, 1, "0"),
		("label.rest",      4, 1, "7"),
		("label.body.text", 5, 7, "6578616d706c65"),
		("label.form",     12, 1, "0"),
		("label.rest",     12, 1, "3"),
		("label.body.text", 13, 3, "636f6d"),
		("label.form",     16, 1, "0"),
		("label.rest",     16, 1, "0"),
		("question.qtype",  17, 2, "1"),
		("question.qclass", 19, 2, "1"),
	]


@pytest.mark.skipif(LUA is None, reason="no Lua interpreter")
def test_a_delimited_member_is_scanned_at_run_time(tmp_path: Path) -> None:
	"""The scan helper, executed: an HTTP request line is three members whose
	offsets no constant can give, and the dissector finds them by looking."""
	consumed, rows = dissect(
		tmp_path, ROOT / "example" / "http" / "http.situ", "request_line",
		b"GET /index.html HTTP/1.1\r\n")

	assert consumed == 26
	assert rows == [
		("request_line.method",  0,  3, "474554"),
		("request_line.target",  4, 11, "2f696e6465782e68746d6c"),
		("request_line.version", 16, 8, "485454502f312e31"),
	]


# -- delimited and versioned members (sections 8.6, 19.4) -------------------


def test_a_delimited_member_is_scanned_not_counted() -> None:
	"""It carries `array_count = 1`, so the count branch dissected it as one
	byte and misaligned every field after it. A dissector shows those bytes to
	somebody debugging a live capture, with the confidence of a decode."""
	lua = emit('struct s { u8 name[] until ": "; u8 rest[remaining]; }')

	assert "name_n = 1" not in lua
	assert "situ_scan(tvb, at, {58, 32}, tvb:len())" in lua


def test_the_scan_helper_appears_only_where_something_scans() -> None:
	"""A helper nobody calls is dead Lua in a file a user is expected to read
	before trusting it."""
	assert "situ_scan" not in emit("struct s { u8 a; u16 b; }")
	assert "situ_scan" in emit('struct s { u8 a[] until ","; u8 r[remaining]; }')


def test_a_capped_scan_stops_at_its_cap() -> None:
	lua = emit('struct s { u8 a[] until "," max 8; u8 r[remaining]; }')

	assert "math.min(at + 8, tvb:len())" in lua


def test_a_run_of_records_says_it_is_not_unrolled() -> None:
	"""The terminator ends the run rather than each element, so where it stops
	is a walk this does not do. Saying so beats a wrong decode and beats
	silence, which reads as the member not existing."""
	lua = emit('struct kv { u8 k[] until ":"; u8 v[] until "\\r\\n"; }\n'
	           'struct s { kv items[] until "\\r\\n"; u8 rest[remaining]; }')

	assert "Not unrolled" in lua


def test_a_versioned_member_is_guarded() -> None:
	"""Without the guard it read `tvb(3, 4)` on a three-byte v1 message and
	Wireshark showed the packet as malformed -- blaming the capture for the
	schema.

	Two questions, and this asked one of them for a long time: a *v3* message
	that stops after three bytes passes the version test and reads past the
	end just the same. Executing every dissector over pseudo-random packets is
	what found the second, on a draw that happened to put 0x20 in the version
	byte (26.37)."""
	lua = emit("struct s [version = v] { u8 v; u16 a; u32 b [since = 2]; }")

	assert "if tvb:len() >= 7 and tvb(0, 1):uint() >= 2 then" in lua
	assert "-- present from version 2" in lua


def test_an_unversioned_member_is_not_guarded() -> None:
	lua = emit("struct s { u8 v; u16 a; u32 b; }")

	assert "present from version" not in lua


def test_a_delimited_member_is_a_bytes_field() -> None:
	"""Not a uint. Its width is not the delimiter's, and declaring it one made
	Wireshark render a variable-length header name as a single decimal
	number."""
	lua = emit('struct s { u8 name[] until ":"; u8 rest[remaining]; }')

	assert 'ProtoField.bytes("s.name", "name")' in lua
	assert "ProtoField.uint8(\"s.name\"" not in lua


# -- a variant, and a run of them -------------------------------------------

DNS_LABEL = """
struct label {
	u2 form;
	u6 rest;
	variant body switch (form) {
		case 0:  u8 text[rest];
		case 3:  u8 pointer_low;
		default: error;
	}
}
struct name { label labels[] while (form == 0 && rest != 0) max 128; }
struct question { name qname; u16 qtype; u16 qclass; }
"""


def test_a_variant_shows_the_arm_the_discriminant_selects() -> None:
	"""It showed `no bytes of its own`, so a reader saw the discriminant and
	not the bytes it discriminates -- the half that matters. Every arm gets a
	`ProtoField`, because which arms exist is a compile-time question even
	though which one is present is not."""
	text = emit(DNS_LABEL)

	assert 'label_f.body_text = ProtoField.bytes("label.body.text"' in text
	assert "label_f.body_pointer_low = ProtoField.uint8(" in text
	assert "if arm == 0 then" in text
	assert "elseif arm == 3 then" in text


def test_the_discriminant_is_read_from_the_struct_not_from_the_cursor() -> None:
	"""`at` has already walked past the fields a length is read from, so the
	reads are based at 0 -- the tvb the dissector was handed starts at the
	struct. Based at `at` it read the discriminant from the byte after
	itself."""
	body = emit(DNS_LABEL)
	arm  = next(line for line in body.splitlines() if "local arm =" in line)

	assert "situ_uint(tvb, 0, 1, false)" in arm
	# A word boundary: `at` is a substring of `math.floor`, and the crude
	# check passed for the wrong reason before the fix as well as after.
	assert not re.search(r"\bat\b", arm.split("=", 1)[1])


def test_a_run_of_variants_is_walked_by_extent() -> None:
	"""`elements of no fixed size` -- which is true, and is a different thing
	from having no size. Each element is measured and handed to its own
	dissector."""
	text = emit(DNS_LABEL)

	assert "local size = label_extent(tvb, at)" in text
	assert 'Dissector.get("label"):call(tvb(at, size):tvb()' in text
	assert "elements of no fixed size" not in text


def test_the_extent_is_lua_rather_than_c() -> None:
	"""The schema's operators are C's. Lua spells four of them differently,
	and `!=` has to be replaced before `!` or the result is `not =`."""
	text = emit(DNS_LABEL)
	walk = next(line for line in text.splitlines() if "if not (" in line)

	# In the code. The comment above the walk quotes the schema, operators and
	# all, which is the point of quoting it.
	code = [line for line in text.splitlines() if not line.strip().startswith("--")]

	assert not any("&&" in line or "||" in line for line in code)
	assert " and " in walk and " ~= " in walk


def test_a_member_after_a_variable_one_is_placed_and_typed() -> None:
	"""`byte_span` is None for a dynamic offset, and *where* it sits is the
	only thing unknown -- how wide it is was never in doubt. Both were
	reported as `no bytes of its own`, which is a strange thing to say about
	a u16, and declared `ProtoField.bytes`."""
	text = emit(DNS_LABEL)

	assert 'question_f.qtype = ProtoField.uint16(' in text
	assert "subtree:add(question_f.qtype, tvb(at, 2))" in text
	assert "no bytes of its own" not in text


def test_a_variable_nested_struct_is_dissected_over_its_extent() -> None:
	"""It was handed `size_bytes`, which is the *minimum*: a whole DNS name
	was dissected as its first byte, and every member after it placed on top
	of the rest."""
	text = emit(DNS_LABEL)

	assert "local size = name_extent(tvb, at)" in text
	assert 'Dissector.get("name"):call(tvb(at, size):tvb()' in text


def test_helpers_are_defined_before_they_are_called() -> None:
	"""`local function` binds where it is written, so a caller that comes
	first names a nil. Containment is acyclic, so the order exists."""
	text  = emit(DNS_LABEL)
	where = {name: text.index(f"local function {name}")
	         for name in ("label_extent", "name_labels_span", "name_extent",
	                      "question_extent")}

	assert where["label_extent"] < where["name_labels_span"]
	assert where["name_labels_span"] < where["name_extent"]
	assert where["name_extent"] < where["question_extent"]



# -- every dissector, executed (26.35) --------------------------------------

#: Alphabets, for the same reason the differential drivers have them: a
#: dissector for a text protocol reaches its scans only over text-shaped
#: bytes, and one for a binary format reaches its arithmetic over anything.
DISSECT_ALPHABETS = (
	None,
	bytes(range(0x20, 0x7f)) + b"\r\n",
	b"0123456789 \r\n:.-",
)


def dissect_bytes(rng: random.Random) -> bytes:
	alphabet = DISSECT_ALPHABETS[rng.randrange(len(DISSECT_ALPHABETS))]
	length   = rng.randrange(0, 96)

	if alphabet is None:
		return bytes(rng.randrange(256) for _ in range(length))
	return bytes(alphabet[rng.randrange(len(alphabet))] for _ in range(length))


@pytest.mark.skipif(LUA is None, reason="no Lua interpreter")
def test_a_pad_lands_the_member_after_it_on_the_multiple(tmp_path: Path) -> None:
	"""`pad_to(n)` shows nothing and moves the cursor, and it did neither.

	`byte_run` is `u8 n; u8 data[n]; pad_to(4); u16 trailer;`. With `n = 14`
	the run ends at 15 and the trailer sits at 16; the generated C says so in
	as many words -- `situ_align_up_u32(offset, 4u, view.limit)` -- and the
	walker reads 16. The dissector stepped over the run and straight past the
	padding, and showed a trailer straddling the pad.

	Chosen bytes rather than the sweep, because the sweep cannot see this:
	an unhandled pad now reaches the "extent this dissector cannot compute"
	branch, which declines every member after it, so the differential sees a
	*missing* row rather than a wrong one -- correct behaviour, and one
	answer of coverage, which no floor can be tight enough to catch.
	"""
	packet = bytes([14]) + bytes(range(0x20, 0x2e)) + b"\x00" + b"\xab\xcd"
	assert len(packet) == 18

	consumed, rows = dissect(tmp_path, ROOT / "test" / "schema" / "padded.situ",
	                         "byte_run", packet)
	trailer = [row for row in rows if row[0].endswith(".trailer")]

	assert len(trailer) == 1, "the trailer is shown exactly once"
	assert trailer[0][1] == 16, "the trailer sits on the multiple of four"
	assert trailer[0][3] == "43981"		# 0xabcd, and not 0x00ab at 15
	assert consumed == 18


def test_a_pad_is_aligned_rather_than_stepped_over() -> None:
	"""The arithmetic itself, so the structural half says it too.

	Relative to the current tvb, which is what the generated C aligns
	relative to -- its own view base, not the whole capture.
	"""
	source, resolved, _ = analyse(ROOT / "test" / "schema" / "padded.situ")
	lua = generate(parse(source), resolved, "padded")

	assert "at = at + ((4 - at % 4) % 4)" in lua


@pytest.mark.skipif(LUA is None, reason="no Lua interpreter")
def test_nothing_is_placed_after_a_member_with_no_computable_extent(
		tmp_path: Path) -> None:
	"""A wrong line is worse than a missing one, which this file already says
	about a located member.

	Four branches declined a member by returning a comment and leaving `at`
	where it was, and the walk carried on placing every member after it at a
	cursor that was now wrong. `beats` is `beat pulse[] while (kind == 0x33)
	max 6; u16 after;` -- the run is declined, and `after` was then shown at
	offset 0 on every packet, where C walks the run first and the walker
	reads it at 2.
	"""
	source, resolved, _ = analyse(ROOT / "test" / "schema" / "edges.situ")
	lua = generate(parse(source), resolved, "edges")

	assert "beats.after: not shown" in lua
	assert "coded_run.trailer: not shown" in lua

	body = lua[lua.index("function beats.dissector"):]
	assert "subtree:add(beats_f.after" not in body[:body.index("\nend")]


@pytest.mark.skipif(LUA is None, reason="no Lua")
@pytest.mark.parametrize("schema", SCHEMAS, ids=ids(SCHEMAS))
def test_every_dissector_runs(schema: Path, tmp_path: Path) -> None:
	"""`luac -p` has parsed every dissector for months, which proves a file is
	syntax. Four were executed over chosen packets (26.14); the other
	twenty-one had never run a line.

	Running them found three schemas whose dissector died on the first packet
	-- `subtree:add(nil, ...)`, because the loop that declares `ProtoField`s
	and the loop that adds them disagreed about varints and coded regions --
	and two more that the *stub* could not run, because it implemented only
	the two-argument `tvb(offset, length)` and a `[remaining]` member writes
	`tvb(at)`.

	This asks only that the dissector survives the packet, and that is still
	worth asking on its own: nothing a dissector shows matters until it
	finishes showing it.

	What it used to say next was that a random buffer has no right answer to
	compare against, so what a dissector *shows* was the business of the
	chosen-byte tests above. That was false, and the machinery to disprove it
	was already in the tree: `walker/report.listing` computes what the schema
	says about *any* buffer, out of the packed image, so a random buffer has a
	right answer and always had one.
	`test_the_dissector_agrees_with_the_walker` below asks both descriptions
	the same question over these same bytes.
	"""
	source, resolved, _ = analyse(schema)
	lua = tmp_path / f"{schema.stem}.lua"
	lua.write_text(generate(parse(source), resolved, schema.stem),
	               encoding="ascii")

	rng = random.Random(20260803)
	for name, struct in sorted(resolved.structs.items()):
		# A register map is a bus transaction rather than bytes on a wire, so
		# no `Proto` is emitted for one and there is nothing to run.
		if struct.layout.register is not None:
			continue

		for _ in range(8):
			packet = dissect_bytes(rng)
			assert LUA is not None
			result = subprocess.run(
				[LUA, str(HARNESS), str(lua), name, packet.hex()],
				capture_output=True, text=True)

			assert result.returncode == 0, (
				f"{schema.name}/{name} died on {len(packet)} bytes:\n"
				f"  {packet.hex()}\n{result.stderr}")


# -- and both descriptions of the same bytes, compared (26.185) -------------

#: Lines the walker's listing carries that are not a member at all: a verdict
#: about the whole struct, and the note for a struct no view can be taken of.
#: The same two `test_walker.py` holds out, for the same reason.
NOT_A_MEMBER = frozenset({"validate", "no-view"})

#: Where the dissector says what it thinks a member *is*. Read out of the
#: emitted text rather than off a row, because `dissect.lua` prints the value
#: and not the kind it printed it as -- and the kind is what decides whether
#: the row is an integer at all.
PROTOFIELD = re.compile(r'ProtoField\.(\w+)\("([^"]+)"')


class Comparison(NamedTuple):
	"""One schema's two descriptions of the same bytes, counted.

	`shown` and `walked` are one side each and `compared` is the overlap,
	which is the number that matters: 26.185's finding was that a one-sided
	coverage number can look healthy while the overlap is small, and that
	nothing counted the intersection.
	"""

	shown:    int
	walked:   int
	compared: int
	differ:   tuple[str, ...]


def _shown(rows: list[tuple[str, int, int, str]], proto: str,
		kinds: dict[str, str]) -> tuple[int, dict[str, int]]:
	"""What the dissector showed about `proto` itself: how much, and which of
	it is an integer a walker's reading can be held against.

	Rows a *nested* dissector added are not `proto`'s and are dropped before
	either count. They carry the nested struct's own abbreviation and sit
	wherever the sub-range began, while the walker's section for that struct
	is read at offset 0 of the whole buffer -- two answers about different
	bytes, which is a difference in what was asked.

	Of what is left, two `ProtoField` kinds are counted and not compared,
	each because the two sides are making different claims rather than
	disagreeing:

	- `bytes` -- a run or a delimited member, shown as its raw bytes. The
	  walker answers `len=` and a first byte about the same member. Neither
	  is wrong and neither is the other.
	- `string` -- the same, for a member shown as text.

	`int` was a third, on the ground that `dissect.lua` read every range with
	`uint()` and Wireshark is what applies the sign. That was true of the
	stub and not of the dissector, and the stub is ours: it applies the sign
	now, from the mask's width where there is a mask, so the fifteen signed
	members in the corpus are compared like any other (26.210).

	The old reason noted that the exclusion could not hide a signed member
	*declared* unsigned, because that one gets a `uint` kind and disagrees.
	The other direction it could hide, and no longer does: an unsigned member
	declared signed is compared too, and reads negative where the walker does
	not.
	"""
	total  = 0
	values: dict[str, int] = {}
	seen:   set[str] = set()
	for abbrev, _, _, value in rows:
		if not abbrev.startswith(proto + "."):
			continue
		total += 1
		if not kinds.get(abbrev, "").startswith(("uint", "int")):
			continue
		local = abbrev.split(".")[-1]
		# The same member shown twice in one packet. The walker has one
		# answer and nothing says which row it is about, so neither row is
		# compared -- an invented pairing would be a disagreement the
		# comparison made up.
		if local in seen:
			values.pop(local, None)
			continue
		seen.add(local)
		values[local] = int(value)
	return total, values


def _walked(image: Image, proto: str,
		packet: bytes) -> tuple[int, dict[str, int]]:
	"""What the walker says about `proto` over the same bytes.

	The count is every member line in that struct's section of the listing;
	the values are the subset that is a plain integer, which is what
	`report._members` prints for a scalar it read: `name value`.

	Every other line answers a different question and is left out rather than
	translated into one. `present=` is a tag's presence and not its bytes --
	the difference the probe for this comparison ran into first. `little=` is
	what a marker says about byte order. `ok=` and `extent=` are whether a
	nested struct fits, and `count=`, `len=`, `term=` and `[0]=` are a run's
	shape. A gate answers `refused=1 opened=1`, which is a claim about
	permission and about no bytes at all.
	"""
	total  = 0
	values: dict[str, int] = {}
	where  = ""
	for line in report.listing(image, packet).split("\n"):
		if line.startswith("-- "):
			where = line[3:]
			continue
		if where != proto or not line.strip():
			continue
		parts = line.split()
		if parts[0] in NOT_A_MEMBER:
			continue
		total += 1
		if len(parts) == 2 and "=" not in parts[1]:
			values[parts[0]] = int(parts[1])
	return total, values


@lru_cache(maxsize=None)
def compare(schema: Path) -> Comparison:
	"""Run one schema's dissector and its walker over the same buffers.

	The bytes are `test_every_dissector_runs`' bytes -- the same seed, the
	same order, the same eight packets per struct -- so the claim that a
	dissector survives a packet and the claim that it agrees about one are
	made about the one buffer rather than about two draws that happen to
	rhyme.

	Cached because the corpus-wide floor below asks the same question of the
	same 37 schemas, and the answer is a pure function of the tree.
	"""
	source, resolved, _ = analyse(schema)
	parsed  = parse(source)
	text    = generate(parsed, resolved, schema.stem)
	blob, _ = packer.pack(parsed, resolved, metadata=True)
	image   = load(blob)
	kinds   = {abbrev: kind for kind, abbrev in PROTOFIELD.findall(text)}

	shown = walked = compared = 0
	differ: list[str] = []

	with tempfile.TemporaryDirectory() as held:
		lua = Path(held) / f"{schema.stem}.lua"
		lua.write_text(text, encoding="ascii")

		rng = random.Random(20260803)
		for name, struct in sorted(resolved.structs.items()):
			# A register map is a bus transaction rather than bytes on a
			# wire, so no `Proto` is emitted for one and there is nothing to
			# run -- the exclusion above, for the reason it gives.
			if struct.layout.register is not None:
				continue

			for _ in range(8):
				packet = dissect_bytes(rng)
				_, rows = read_back(lua, name, packet)

				rows_total, said = _shown(rows, name, kinds)
				line_total, read = _walked(image, name, packet)
				shown  += rows_total
				walked += line_total

				for member in said.keys() & read.keys():
					compared += 1
					if said[member] != read[member]:
						differ.append(
							f"{name}.{member}: the dissector shows "
							f"{said[member]} and the walker reads "
							f"{read[member]}\n    buffer: {packet.hex()}")

	return Comparison(shown, walked, compared, tuple(differ))


@pytest.mark.skipif(LUA is None, reason="no Lua")
@pytest.mark.parametrize("schema", SCHEMAS, ids=ids(SCHEMAS))
def test_the_dissector_agrees_with_the_walker(schema: Path) -> None:
	"""Two descriptions of one layout, asked the same question.

	The dissector walks a buffer in Lua from the placements the generator
	read; `walker/report.listing` walks the same buffer from the packed
	image. Neither is derived from the other at run time, so where both speak
	about a member they are a differential -- the move 26.185 records for the
	walker against C, one column over.

	Only where both speak. A member the dissector shows and the walker never
	renders is a difference in what was asked and not in the answer, and
	forcing either side to answer something it does not is how a comparison
	invents disagreements. `_shown` and `_walked` say which shapes are held
	out and why.
	"""
	result = compare(schema)

	assert result.differ == (), (
		f"{schema.parent.name}/{schema.name}: the dissector and the walker "
		f"disagree about bytes they were both handed:\n  "
		+ "\n  ".join(result.differ))


@pytest.mark.skipif(LUA is None, reason="no Lua")
def test_the_comparison_covers_enough_to_be_a_differential() -> None:
	"""The intersection, counted, because nothing else counts it.

	A gate over an empty file list reports success exactly as loudly as a
	real pass, and the test above is a gate over `said.keys() & read.keys()`.
	It would go green over a corpus where the two sides never once speak
	about the same member -- and each side has its own reasons to shrink,
	none of which is visible from the other.

	Two floors, for 26.185's reason: either alone can be met while the other
	rots. The count, so the overlap cannot quietly shrink; and its share of
	what the dissector shows, so adding schemas the walker says nothing about
	cannot dilute it.

	Measured over the 37 committed schemas: 3278 rows shown, 3940 member
	lines walked, 2361 member-answers compared -- 72% of what the dissector
	shows and 59% of what the walker renders.

	Seventy-nine of those answers arrived when the stub learned to apply the
	sign: the same rows, counted all along, moved from the held-out column
	into the compared one. They cover all four widths and both readings --
	`edges.trim` is `i5` behind a mask, `image`'s four `i64` members come
	through `add_le` -- and negatives are drawn at every one of them.

	The dissector shows 37 fewer rows than when this was first measured, and
	that is the differential's first two findings being fixed rather than
	coverage lost: a member after one whose extent the dissector cannot
	compute used to be placed at the un-advanced cursor and shown with full
	confidence. It is declined by name now.
	"""
	shown = walked = compared = 0
	for schema in SCHEMAS:
		result    = compare(schema)
		shown    += result.shown
		walked   += result.walked
		compared += result.compared

	assert compared >= 2340, (
		f"the two descriptions are compared over {compared} member-answers, "
		f"down from 2361; the dissector shows {shown} rows and the walker "
		f"renders {walked} member lines")
	assert compared * 100 >= shown * 71, (
		f"{compared} of the dissector's {shown} rows are compared against "
		f"the walker, down from 72%")
	assert walked >= 3940, (
		f"the walker renders {walked} member lines, down from 3940; the "
		"share above can be met by the dissector showing less")


@pytest.mark.skipif(LUA is None, reason="no Lua")
def test_a_signed_member_is_shown_with_its_sign(tmp_path: Path) -> None:
	"""Chosen bytes for the sign, because the differential can be met without
	it.

	The corpus comparison catches both ways of getting this wrong -- dropping
	the sign, and extending it from the container rather than from the field
	-- and it catches them as *disagreements with the walker*, which is the
	right evidence and is also evidence about the walker. These are the two
	numbers on their own, so the stub can be held to them where nothing else
	is running.

	Three widths and both readings. `trim` is `i5` behind the mask `0xf8`, so
	the byte `0xf8` is 31 in five bits and -1 as one of them; `scale` is the
	three bits below it and stays unsigned. `epoch` is `i64` and comes through
	`add_le`, where the extension is the one Lua's integer has already done --
	`0x8000000000000000` little-endian is the most negative value there is,
	and no arithmetic in `display` may disturb it.
	"""
	lua = tmp_path / "unit.lua"
	lua.write_text(emit("struct s { i5 trim; u3 scale; i16 drift; }\n"),
	               encoding="ascii")

	_, rows = read_back(lua, "s", bytes([0xf8, 0xff, 0xff]))
	assert rows == [("s.trim", 0, 1, "-1"), ("s.scale", 0, 1, "0"),
	                ("s.drift", 1, 2, "-1")]

	schema   = parse_text("target buffer;\nendian little;\n"
	                      "bit_order msb_first;\nstruct t { i64 epoch; }\n")
	resolved = resolve(schema, solve(schema))
	little   = tmp_path / "little.lua"
	little.write_text(generate(schema, resolved, "little"), encoding="ascii")

	assert read_back(little, "t", bytes([0xff] * 8))[1] == [
		("t.epoch", 0, 8, "-1")]
	assert read_back(little, "t", bytes(7) + bytes([0x80]))[1] == [
		("t.epoch", 0, 8, "-9223372036854775808")]


@pytest.mark.skipif(LUA is None, reason="no Lua interpreter")
def test_a_number_in_digits_is_never_negative(tmp_path: Path) -> None:
	"""`tonumber` reads a leading minus and situ has no sign to read (8.6.2).

	Four bytes of "-26" drove a length of -26, an offset of -48, and
	`tvb(-48, 2)` -- which is an error out of the harness rather than a short
	field. The compiled backends parse digits and have no such case.

	Found by the random-packet sweep over `test/schema/edges.situ`, which
	drew a different packet once the schema grew, and the first fix for it
	hit invariant 53: `math.max` in an expression the builtin expansion then
	rewrote into `math.math.max`.
	"""
	source = emit("struct s { decimal u32 n[4]; u16 d[n]; u16 tail; }\n")

	assert "situ_digits(" in source
	assert "math.math" not in code(source)

	# And it holds: "-26" in four bytes is zero here, not a negative length.
	lua = tmp_path / "unit.lua"
	lua.write_text(source, encoding="ascii")
	assert LUA is not None
	result = subprocess.run(
		[LUA, str(HARNESS), str(lua), "s", b"-26 \x00\x00\x00\x00".hex()],
		capture_output=True, text=True)
	assert result.returncode == 0, result.stderr


def test_a_located_member_is_read_where_the_data_says() -> None:
	"""`at expr` places a member where a field says, and the dissector read it
	at the running cursor instead.

	Section 9.8 is explicit: a located member "joins no offset chain: it
	contributes nothing to the enclosing struct's extent, and the member
	declared after it sits where it would if the located member were not
	there". `offset_bits` is `None` for one, so it fell through to `at` and
	was dissected wherever the walk had reached -- while the file printed
	"the dissector cannot drift from the parser" at the top.

	For a typical BMP the two agree: the headers end at 54 and
	`file.pixel_offset` is also 54. That is why nothing noticed, and it is
	the case the construct does not exist for. A file with a colour table or
	a gap between the headers and the pixels is the one it does.
	"""
	schema = parse_text((ROOT / "example/bmp/bmp.situ").read_text(encoding="ascii"))
	lua    = generate(schema, resolve(schema, solve(schema)), "bmp")

	# `file.pixel_offset` is at 0x0A, four bytes, little endian.
	assert "local pixels_at = situ_uint(tvb, 10, 4, true)" in lua
	assert "subtree:add(bitmap_file_f.pixels, tvb(pixels_at, pixels_n))" in lua

	# And the cursor is not advanced past it: nothing in its block touches
	# `at`, because the member after it sits where it would without it.
	block = lua[lua.index("-- bitmap_file.pixels"):]
	block = block[:block.index("\n\n")]
	assert "at = at +" not in block, block
