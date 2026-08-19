"""`prefix(...)`: coverage over bytes the message does not contain (14.2a).

TCP's and UDP's checksums run over a pseudo-header built from the IP layer's
addresses -- which is why the kernel's `csum_tcpudp_nofold` takes `saddr` and
`daddr` as arguments rather than reading them out of the datagram.

A pseudo-header is a byte layout, which is what situ describes; what situ
cannot do is fill one in from this message. So the clause names a declared
struct, and the generated code says how many bytes the caller supplies and in
what shape. Computing the sum was already the caller's (14.1), so this widens
*which bytes are covered* and nothing else.
"""

from __future__ import annotations

import pytest

from situc.codegen.c import generate
from situc.diagnostics import SituError
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve

PREAMBLE = "target buffer;\nendian big;\nbit_order msb_first;\n\n"

PSEUDO = """struct pseudo {
	u32 source_address;
	u32 destination_address;
	u8  zero;
	u8  protocol;
	u16 length;
}
"""

COVERED = """struct msg {
	authenticated summed {
		u16 port;
		checksum u8 sum[2] covers(summed)%s [self_as = 0];
	}
}
"""


def emit(body: str) -> str:
	schema   = parse_text(PREAMBLE + body)
	resolved = resolve(schema, solve(schema))
	return generate(schema, resolved, "t").header


def refusal(body: str) -> str:
	with pytest.raises(SituError) as caught:
		emit(body)
	return caught.value.diagnostic.render()


def test_the_prefix_size_is_emitted() -> None:
	"""What a caller cannot safely guess, and what a schema compiler is for:
	RFC 768's pseudo-header is twelve bytes."""
	header = emit(PSEUDO + COVERED % " prefix(pseudo)")
	assert "#define SITU_MSG_SUM_PREFIX_BYTES 12u" in header


def test_the_header_says_the_bytes_are_not_situ_s_to_supply() -> None:
	"""The division of labour is the whole design. Situ knows the layout and
	the length and cannot know the contents, so it says both and stops."""
	header = emit(PSEUDO + COVERED % " prefix(pseudo)")
	assert "which this message does not" in header
	assert "not situ's to supply" in header


def test_without_the_clause_nothing_is_said_about_a_prefix() -> None:
	"""The control: a checksum over its own message only, which is ICMP's
	shape, must not grow a prefix nobody asked for."""
	header = emit(PSEUDO + COVERED % "")
	assert "PREFIX_BYTES" not in header
	assert "does not" not in header.split("sum covers")[1][:400]


def test_an_unknown_prefix_is_refused() -> None:
	text = refusal(PSEUDO + COVERED % " prefix(nowhere)")
	assert "unknown prefix `nowhere`" in text
	assert "`pseudo`" in text


def test_a_struct_may_not_prefix_itself() -> None:
	"""A prefix is bytes the message does not contain, so naming the message
	is either a no-op or a second pass over the same bytes."""
	text = refusal(PSEUDO + COVERED % " prefix(msg)")
	assert "names its own struct as its prefix" in text


# -- the packer bug this uncovered ------------------------------------------


COVERED_PAYLOAD = """struct u {
	authenticated s {
		u16 port;
		u16 length [min = 8];
		checksum u8 sum[2] covers(s) [self_as = 0];
		u8 payload[length - 8];
	}
}
"""


def test_a_member_inside_a_region_keeps_its_size_program() -> None:
	"""`layout.place_authenticated` does not extend the path -- its members
	keep the enclosing struct's namespace (5.3) -- and `_ast_members` did,
	recording `u.s.payload` for a placement whose path is `u.payload`. The
	lookup missed, the member got no size program, and a counted run with no
	program measures zero.

	The consequence was the walker's worst failure shape: `validate` answered
	OK for a message every backend refuses. It stayed invisible because no
	covered region held a member whose bounds C would reject -- `examples/icmp`
	has the same shape and its fields never had a program either.
	"""
	from situc import pack as packer
	from walker.image import NONE, load
	from walker import report

	schema   = parse_text(PREAMBLE + COVERED_PAYLOAD)
	resolved = resolve(schema, solve(schema))
	blob, _  = packer.pack(schema, resolved, metadata=True)
	image    = load(blob)

	payload = image.placements[3]
	assert payload.size_code != NONE, "the payload has no size program"

	# And the answer it exists to give: a length of 0x2F20 in a ten-byte
	# frame puts the payload past the end, which is BOUNDS and not OK.
	listing = report.listing(image, bytes.fromhex("50202f2045540a0a802e"))
	assert "validate 1" in listing
	assert "validate 0" not in listing
