"""`skip`: the whitespace a member owns in front of it.

Every text format has bytes that may stand between two tokens and mean
nothing, and no construct here could say so. `until` and `before` describe
where a member *ends*; this describes where one *begins*, which is a
different fact about the bytes and needed a different word.

The rule it does not break is the one that makes a layout a layout: every
byte belongs to exactly one member. A lead belongs to the member that
FOLLOWS it, because the member before it is finished -- so the struct still
partitions its bytes exactly, and what the whitespace costs is the offset
rather than the accounting. That is the same price a delimiter already
charges, which is why `skip` fits a layout language at all.

The set is the schema's. `whitespace ' ' | '\\t' | '\\r' | '\\n'` is JSON's
four bytes (RFC 8259 section 2); HTTP's optional whitespace is two of them.
A compiler that supplied a default would be wrong about one of those, so a
bare `skip` with no directive is refused rather than guessed.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import ModuleType

import pytest

from every_schema import ROOT
from situc import unparse
from situc.diagnostics import Source, SituError
from situc.layout import solve
from situc.pack import pack
from situc.parser import parse, parse_text
from situc.resolve import ResolvedSchema, resolve
from walker.image import load as load_image
from walker.walk import (TooDeep, acquire, chain_bits, content_bits,
                         lead_bytes, offset_bits, size_bits, struct_extent)

from test_codegen_python import load as load_module

PREAMBLE = ("target buffer;\nendian big;\n"
            "whitespace ' ' | '\\t' | '\\r' | '\\n';\n")

#: Two tokens with a lead on the second, which is the smallest schema in
#: which a lead can be right or wrong: `b`'s offset is the whitespace's
#: length and `a`'s span is one byte whatever follows it.
PAIR = PREAMBLE + "struct pair {\n\tu8  a;\n\tu8  b  skip;\n}\n"


def build(text: str) -> ResolvedSchema:
	schema = parse_text(text)
	return resolve(schema, solve(schema))


def placement(resolved: ResolvedSchema, struct: str, name: str):
	for entry in resolved.structs[struct].entries:
		if entry.placement.name == name:
			return entry.placement
	raise AssertionError(f"no member {struct}.{name}")


def walked(text: str, message: bytes, struct: str | int = 0):
	"""One packed image and one view over it, for the walker's answers.

	Packed WITH metadata so a struct can be named rather than numbered. The
	image's order is the packer's and is not the schema's, so an index
	computed here would be a guess -- and a wrong one picks a struct that
	exists, which reads exactly like a disagreement about the right one.
	"""
	schema   = parse_text(text)
	resolved = resolve(schema, solve(schema))
	image    = load_image(pack(schema, resolved, metadata=True)[0])
	shape    = (image.struct_names.index(struct) if isinstance(struct, str)
	            else struct)
	return image, acquire(image, message, shape)


# -- what a lead is --------------------------------------------------------


def test_the_lead_belongs_to_the_member_that_follows_it() -> None:
	"""The whole construct in one assertion, and it is a relationship
	rather than a pair of numbers: the member's own offset moves by exactly
	the whitespace, and its span grows by exactly the same amount. Either
	alone would pass with the lead counted twice or not at all."""
	for spaces in range(4):
		message = b"a" + b" " * spaces + b"b"
		image, view = walked(PAIR, message)
		index = image.members(image.structs[0])[1]

		assert lead_bytes(view, index) == spaces
		assert chain_bits(view, index) == 8
		assert offset_bits(view, index) == 8 + spaces * 8
		assert content_bits(view, index) == 8
		assert size_bits(view, index) == 8 + spaces * 8


def test_the_struct_still_partitions_its_bytes() -> None:
	"""The rule a layout language cannot give up. Whitespace anywhere in a
	message is part of some member's span, so the extent is the message --
	which is what a run of these depends on and what the old JSON bill said
	a layout could not do."""
	for spaces in range(5):
		message = b"a" + b" " * spaces + b"b"
		_, view = walked(PAIR, message)
		assert struct_extent(view) == len(message)


def test_a_lead_reads_a_set_and_not_a_sequence() -> None:
	"""Any of the declared bytes, in any order, and it stops at the first
	byte that is not one. A scan would look for a sequence and answer where
	the whitespace STARTS, which is the other question entirely."""
	message = b"a \t\r\n \tb"
	_, view = walked(PAIR, message)
	index = 1

	assert lead_bytes(view, index) == 6
	assert struct_extent(view) == len(message)


def test_a_lead_that_runs_to_the_end_leaves_no_member() -> None:
	"""Whitespace to the end of the buffer is a message with nothing after
	it, not a malformed lead: the run is as long as it is, and the member's
	own bounds check is what reports that it is not there."""
	message = b"a   "
	_, view = walked(PAIR, message)

	assert lead_bytes(view, 1) == 3
	assert offset_bits(view, 1) == 4 * 8


# -- what it costs ---------------------------------------------------------


def test_a_lead_makes_the_offset_scanned() -> None:
	"""And says so in the axes, which is where a reader of the map finds
	out. `Dynamic` would have been the wrong word: arithmetic over values
	already read cannot fail, and reading a lead is a search."""
	resolved = build(PAIR)
	entry = next(e for e in resolved.structs["pair"].entries
	             if e.placement.name == "b")
	axes = {axis.value: value.base for axis, value in entry.vector.values}

	assert axes["offset"] == "Scanned"
	assert axes["access"] == "Sequential"
	assert axes["address"] == "Unstable"


def test_a_lead_is_recorded_in_the_wire_signature() -> None:
	"""A committed contract, because a member that may be preceded by
	whitespace frames differently from one that may not -- so a peer reading
	the signature has to be able to tell, and a change to the set has to be
	a diff somebody reviewed."""
	from situc import wire

	schema   = parse_text(PAIR)
	resolved = resolve(schema, solve(schema))
	rendered = wire.render(schema, resolved, "pair.situ")

	assert "skip=20090d0a" in rendered


# -- the set, and what it may hold -----------------------------------------


def test_a_bare_skip_needs_a_declared_set() -> None:
	"""Refused rather than defaulted. Which bytes may stand between tokens
	is the format's answer and the formats disagree, so a compiler that
	supplied one would be silently wrong about half of them."""
	with pytest.raises(SituError) as raised:
		build("target buffer;\nendian big;\n"
		      "struct s { u8 a; u8 b skip; }\n")
	assert "no whitespace declared" in str(raised.value)


def test_an_alternative_wider_than_a_byte_is_refused() -> None:
	"""A skip tests one byte at a time, so a two-byte alternative would be
	a sequence to match rather than a set to test -- a construct situ does
	not have, named rather than half-built."""
	with pytest.raises(SituError) as raised:
		build(PREAMBLE + "struct s { u8 a; u8 b skip \"ab\"; }\n")
	assert "a skip tests one" in str(raised.value)


def test_the_same_byte_twice_is_refused() -> None:
	"""A byte is in the set or it is not, so a repeat changes nothing and
	is more likely a typo than a choice -- the same refusal `until` makes
	about a repeated delimiter."""
	with pytest.raises(SituError) as raised:
		build(PREAMBLE + "struct s { u8 a; u8 b skip ' ' | ' '; }\n")
	assert "listed twice" in str(raised.value)


def test_a_directive_below_a_struct_is_refused() -> None:
	"""`encoding`'s rule and its reason: a bare `skip` is resolved where it
	is written, so a directive here would give the members above it a
	different set from the ones below, from a line that reads like a
	statement about the whole file."""
	with pytest.raises(SituError) as raised:
		build("target buffer;\nendian big;\n"
		      "struct s { u8 a; }\n"
		      "whitespace ' ';\n")
	assert "comes before the structs" in str(raised.value)


def test_an_imported_struct_is_not_this_files_struct() -> None:
	"""`import` splices the imported file's declarations in AHEAD of this
	file's own, so a schema that both imports a file carrying a directive
	and declares its own met a struct before its own directive -- and was
	refused by a diagnostic pointing at a line that is at the top of the
	file it names.

	The rule was always about one file. A directive governs the literals
	written after it IN THAT FILE, so what a struct from somewhere else
	sits before decides nothing. It reached `encoding` first, one construct
	earlier, and `whitespace` would have doubled it."""
	root = Path(tempfile.mkdtemp())
	(root / "b.situ").write_text(
		"target buffer;\nendian big;\nwhitespace ' ';\n"
		"struct b { u8 x; }\n", encoding="ascii")
	(root / "a.situ").write_text(
		"target buffer;\nendian big;\nimport \"b.situ\";\n"
		"whitespace '\\t';\n"
		"struct a { u8 y; u8 z skip; }\n", encoding="ascii")

	schema   = parse(Source(str(root / "a.situ"),
	                        (root / "a.situ").read_text(encoding="ascii")))
	resolved = resolve(schema, solve(schema))

	# And each file's own set governs its own members, which is the half a
	# refusal-free parse does not prove.
	assert placement(resolved, "a", "z").skip == (0x09,)


def test_a_member_may_name_its_own_set() -> None:
	"""HTTP's optional whitespace is space and tab where its line framing is
	CRLF, so one file can want two sets -- and a member that names its own
	needs no directive at all."""
	resolved = build("target buffer;\nendian big;\n"
	                 "struct s { u8 a; u8 b skip ' ' | '\\t'; }\n")
	assert placement(resolved, "s", "b").skip == (0x20, 0x09)


# -- what a lead may not be combined with ----------------------------------


REFUSALS = {
	"while":   "struct s { u8 n; e r[] skip while (t == 1); }\n"
	           "struct e { u8 t; }\n",
	"array":   "struct s { u8 n; u8 r[4] skip; }\n",
	"record":  "struct s { u8 n; e r[] skip until \"Z\"; }\n"
	           "struct e { u8 t; }\n",
	"cap":     "struct s { u8 n; u8 r[] skip until \"Z\" max 8; }\n",
	"located": "struct s { u8 n; u8 r[2] at n; }\n",
}


def test_every_refusal_names_what_it_refuses() -> None:
	"""A population assertion rather than five separate ones: what matters
	is that the whole list is refused and that each says WHY, because a
	refusal a reader cannot act on is a limit nobody can work around.

	`located` is written without `skip` and gets it here, so that the
	fixture cannot drift into testing a schema that is refused for some
	other reason."""
	for name, body in REFUSALS.items():
		text = PREAMBLE + (body.replace("u8  r[2] at n", "u8 r[2] skip at n")
		                   if name == "located" else body)
		if name == "located":
			text = PREAMBLE + "struct s { u8 n; u8 r[2] skip at n; }\n"
		with pytest.raises(SituError, match="(?s).") as raised:
			build(text)
		assert "skip" in str(raised.value) or "lead" in str(raised.value), name


def test_a_lead_and_a_cap_would_make_the_cap_a_claim_it_does_not_keep() -> None:
	"""The one refusal that is about arithmetic rather than ambiguity.
	`max N` bounds the whole member, delimiter included (8.6.1), and situ
	has no bound for a lead -- so accepting both would leave a documented
	guarantee false."""
	with pytest.raises(SituError) as raised:
		build(PREAMBLE + "struct s { u8 n; u8 r[] skip until \"Z\" max 8; }\n")
	assert "does not bound it" in str(raised.value)


# -- the spelling ----------------------------------------------------------


def test_the_spelling_survives_a_round_trip() -> None:
	"""A bare `skip` prints back bare and a named set prints back named.
	The expansion would be the same bytes and not the same source: a schema
	that stated its set once would come back stating it at every member,
	which is a round trip that has lost what the author wrote."""
	text = (PREAMBLE
	        + "struct s {\n\tu8  a;\n\tu8  b  skip;\n"
	          "\tu8  c  skip ' ' | '\\t';\n}\n")
	printed = unparse.unparse(parse_text(text))

	assert "whitespace ' ' | '\\t' | '\\r' | '\\n';" in printed
	assert "u8 b skip;" in printed
	assert "u8 c skip ' ' | '\\t';" in printed
	# And it parses back to the same placements, which is the half a
	# string comparison cannot check.
	again = build(printed)
	assert placement(again, "s", "b").skip == (0x20, 0x09, 0x0D, 0x0A)
	assert placement(again, "s", "c").skip == (0x20, 0x09)


# -- the generated code ----------------------------------------------------


def test_the_python_backend_places_a_spaced_member(tmp_path: Path) -> None:
	"""The walker's answer, from the other side. Two independent readers of
	one layout agreeing about the same bytes is the whole method here, and
	a lead is the newest place they could come apart."""
	module = load_module(tmp_path, "struct pair {\n\tu8  a;\n\tu8  b  skip;\n}\n",
	                     preamble = PREAMBLE)
	runtime = __import__("situ_runtime")

	for spaces in range(4):
		message = b"a" + b" " * spaces + b"b"
		view = module.pair.at(runtime.Message(bytearray(message)), 0,
		                      len(message))
		assert view.b == ord("b"), spaces
		assert view.b_start() == 1, spaces
		assert view.b_lead(1) == spaces, spaces


def test_the_runtime_helper_stops_at_the_first_byte_not_in_the_set() -> None:
	"""`skip_run` on its own, the way `test_characters` checks `scan_any`.

	The three answers that matter: a lead of nothing, a lead that ends, and
	a lead that runs to the end of what it was given. The third is not a
	failure -- whitespace to the end of a message is a message with no
	member after it, and the member's own bounds check is what reports that.
	"""
	from situ_runtime import skip_run  # type: ignore[import-not-found]

	space = (0x20, 0x09)
	assert skip_run(b"abc", 3, space) == 0
	assert skip_run(b"  abc", 5, space) == 2
	assert skip_run(b" \t \t", 4, space) == 4
	assert skip_run(b"", 0, space) == 0
	# Bounded by the length it was given, not by the buffer: a view is a
	# window, and a lead may not read past the frame it was handed.
	assert skip_run(b"    x", 2, space) == 2


# -- the corpus ------------------------------------------------------------


#: Documents whose bytes differ only in whitespace. Each is a JSON document
#: `json.loads` accepts, and each must measure its own length -- which is
#: what a run over a stream of them depends on.
SPACED = (
	b'{"a":12}',
	b'{ "a" : 12 }',
	b'  {"a": 12}',
	b'[1, 2, 30]',
	b'[ 1 , 2 , 30 ]',
	b'{"a": [1, {"b": "c"}]}',
	b'{\n\t"a" : "b",\n\t"c" : 3\n}',
	b'{ "a" : { "b" : [ 1 , 2 ] } }',
	b'"hi"',
	b' true',
)


def test_json_measures_a_pretty_document(tmp_path: Path) -> None:
	"""The example this was written for, end to end. Before `skip` the
	spaced forms measured short by exactly the bytes nobody had claimed,
	and `validate` was content -- a short measurement and no complaint,
	which is the answer this repository rates worst."""
	text   = (ROOT / "example" / "json" / "json.situ").read_text(encoding="ascii")
	module = load_module(tmp_path, "", preamble = text)
	runtime = __import__("situ_runtime")

	for document in SPACED:
		view = module.value.at(runtime.Message(bytearray(document)), 0,
		                       len(document))
		assert view._extent == len(document), document


def test_the_json_walker_agrees_with_the_json_backend() -> None:
	"""And the fifth column says the same, from a packed image rather than
	from compiled code.

	Every document either measures its own length or is refused BY NAME for
	nesting deeper than this build follows -- and both happen here, which is
	the point of asserting over the whole list rather than over a subset
	chosen to pass. `[depth = 64]` is the format's number and a walker
	spends its own, so a deep document is a refusal rather than a short
	answer, and a short answer is the thing this pair of assertions exists
	to catch."""
	text = (ROOT / "example" / "json" / "json.situ").read_text(encoding="ascii")

	measured, refused = [], []
	for document in SPACED:
		_, view = walked(text, document, struct = "value")
		try:
			assert struct_extent(view) == len(document), document
			measured.append(document)
		except TooDeep:
			refused.append(document)

	assert measured and refused
	assert len(measured) + len(refused) == len(SPACED)
