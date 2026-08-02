"""`situc gen-dissector` -- a Wireshark dissector, in Lua (section 20.3).

Two kinds of test. The structural ones read the emitted text and check that the
numbers in it are the numbers the layout says. The rest *run* it: a Lua
interpreter parses every dissector this repository can generate, and four of
them are executed over real packet bytes through the Wireshark stub in
`tests/lua/dissect.lua`.

What is still not checked is that Wireshark accepts the plugin -- there is no
Wireshark here, and the stub is a stand-in for its API rather than a copy of
it. What is checked now is the dissection itself, which is the half that
produces wrong answers rather than an error message: a dissector that loads and
shows the wrong bytes is worse than one that fails to load.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from situc.cli import analyse
from situc.dissector import generate
from situc.layout import solve
from situc.parser import parse, parse_text
from situc.resolve import resolve

from every_schema import ROOT, SCHEMAS
EXAMPLES = SCHEMAS

PREAMBLE = "target buffer;\nendian big;\nbit_order msb_first;\n"

LUA     = shutil.which("lua5.4") or shutil.which("lua")
LUAC    = shutil.which("luac5.4") or shutil.which("luac")
HARNESS = ROOT / "tests" / "lua" / "dissect.lua"

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

		# `do` is not counted: `for ... do` is one block with two keywords, and
		# no bare `do ... end` is emitted.
		opens = len(re.findall(r"\b(function|if|for|while)\b", body))
		ends  = len(re.findall(r"\bend\b", body))

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


# -- run it (26.14, and what section 22 said it did not do) -----------------


@pytest.mark.skipif(LUA is None, reason="no Lua interpreter")
def test_a_dissector_shows_the_fields_a_real_packet_holds(tmp_path: Path) -> None:
	"""The first generated Lua in this repository ever executed.

	A UDP header off the wire: source 1234, destination 80, length 24. Every
	row is a field the dissector *showed*, at the offset it showed it at, so
	this is the layout arriving in the one artifact nothing had run."""
	consumed, rows = dissect(
		tmp_path, ROOT / "examples" / "udp" / "udp.situ", "udp_header",
		bytes.fromhex("04d2005000180000"))

	assert consumed == 8
	assert rows == [
		("udp_header.source_port",      0, 2, "1234"),
		("udp_header.destination_port", 2, 2, "80"),
		("udp_header.length",           4, 2, "24"),
		("udp_header.checksum",         6, 2, "0"),
	]


@pytest.mark.skipif(LUA is None, reason="no Lua interpreter")
def test_bit_packed_fields_come_out_masked_and_shifted(tmp_path: Path) -> None:
	"""`45 00 ...` is version 4 and IHL 5 in one byte, which is the read a
	dissector gets wrong by showing 0x45 twice.

	The mask is in the `ProtoField`, so this is Wireshark's arithmetic rather
	than the generator's -- and checking it means doing what Wireshark does,
	which the stub does and the text tests could not."""
	consumed, rows = dissect(
		tmp_path, ROOT / "examples" / "ipv4" / "ipv4.situ", "ipv4_header",
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
		tmp_path, ROOT / "examples" / "dnsname" / "dnsname.situ", "question",
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
		tmp_path, ROOT / "examples" / "http" / "http.situ", "request_line",
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
	schema."""
	lua = emit("struct s [version = v] { u8 v; u16 a; u32 b [since = 2]; }")

	assert "if tvb(0, 1):uint() >= 2 then" in lua
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

	assert "tvb(0, 1)" in arm
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

