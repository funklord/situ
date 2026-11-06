"""What a chain of length-prefixed members costs to read (26.918).

Every field a length read found itself by summing the lengths ahead of it, and
each of those read a field the same way, twice: three calls for every link.
Twelve strings took fifteen million calls to validate in C. Nothing about the
message mattered, and neither a sample of a schema with two lengths nor the
nesting costs, whose schemas hold still while the message grows, could see it.

So the schema is what grows here. The counter and the six readers are the
nesting costs' own, because one pass over the members is a second way of finding
a field, and it has to find the same one.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

import fourway
from test_nesting_costs_no_more_than_its_length import (
	HEAD, TARGETS, build, build_walker, cost, flipped, pack_text, settled,
	walker_cost)
from test_walker_c import (VERDICT, _build_driver, c_verdict, python_verdict,
                           shape_named_text)
from walker.walk import Refused


#: A link of each kind that finds its length differently, and its bytes.
LINKS = {
	"strings": ("u8 l{at};\n\tu8 t{at}[l{at}];", b"\x01\x09"),
	"nested":  ("hdr l{at};\n\tu8 t{at}[l{at}.l];", b"\x01m\x09"),
	"varints": ("v l{at};\n\tu8 t{at}[l{at}];", b"\x01\x09"),
	"digits":  ('decimal u8 l{at} until ";" max 3;\n\tu8 t{at}[l{at}];',
	            b"1;\x09"),
	"records": ("u8 l{at};\n\tone t{at}[l{at}];", b"\x01\x00"),
}

#: What a link can name.
TYPES = ("struct hdr { u8 l; u8 m; }\n"
         "struct one { u8 l; u8 d[l]; }\n"
         "varint_type v { encoding = leb128; max_bits = 14; max_bytes = 2; }\n")


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("link", list(LINKS))
def test_twice_the_links_cost_a_polynomial(link: str, target: str,
		tmp_path: Path) -> None:
	"""A quartic at most, sixteen times the calls, where the old code took 729.

	Measured at 6, 12, 24 and 48 links a run of records costs 8.0 times per
	doubling in C and about 13 in Python, and holds there. An exponential
	would square the ratio each time.
	"""
	member, data = LINKS[link]
	counted: dict[int, int] = {}
	for count in (6, 12):
		members = "".join(f"\t{member.format(at=at)}\n" for at in range(count))
		held = tmp_path / str(count)
		held.mkdir()
		command = build(held, HEAD + TYPES + "struct x {\n" + members
		                + "\tu8 end;\n}\n", target, "x")
		found, _ = cost(command, data * count + b"\x07", held)
		assert found is not None
		counted[count] = found
	assert counted[12] < counted[6] * 16, counted


@pytest.mark.parametrize("target", ("c", "python"))
@pytest.mark.parametrize("link", list(LINKS))
def test_the_walkers_cost_a_polynomial_too(link: str, target: str,
		tmp_path: Path) -> None:
	"""They walk front to back, and this says they go on doing so."""
	member, data = LINKS[link]
	counted: dict[int, int] = {}
	for count in (6, 12):
		members = "".join(f"\t{member.format(at=at)}\n" for at in range(count))
		held = tmp_path / str(count)
		held.mkdir()
		command = build_walker(held, HEAD + TYPES + "struct x {\n" + members
		                       + "\tu8 end;\n}\n", "x", target)
		found, _ = walker_cost(command, data * count + b"\x07", held)
		assert found is not None
		counted[count] = found
	assert counted[12] < counted[6] * 16, counted


#: Each a member a pass has to step over, three times, so that a field is read
#: from where another such field put it.
SHAPES = {
	"plain": ("""
struct x {
	u8 a; u8 s[a];
	u8 b; u8 t[b];
	u8 c; u8 u[c];
	u8 end;
}
""", b"\x02pp\x01q\x03rrr!"),

	# Each length reads two fields, so a pass that kept only the nearest
	# would still double at every link.
	"two_back": ("""
struct x {
	u8 a; u8 b;
	u8 s[a + b];
	u8 c; u8 t[b + c];
	u8 d; u8 u[c + d];
	u8 end;
}
""", b"\x01\x01pp\x01qq\x01rr!"),

	"nested": ("""
struct hdr { u8 l; u8 m; }
struct x {
	hdr a; u8 s[a.l];
	hdr b; u8 t[b.l];
	hdr c; u8 u[c.l];
	u8 end;
}
""", b"\x01mp\x02mqq\x01mr!"),

	"varint": ("""
varint_type v { encoding = leb128; max_bits = 14; max_bytes = 2; }
struct x {
	v a; u8 s[a];
	v b; u8 t[b];
	v c; u8 u[c];
	u8 end;
}
""", b"\x01p\x02qq\x01r!"),

	"digits": ("""
struct x {
	decimal u8 a until ";" max 3; u8 s[a];
	decimal u8 b until ";" max 3; u8 t[b];
	decimal u8 c until ";" max 3; u8 u[c];
	u8 end;
}
""", b"1;p2;qq1;r!"),

	"records": ("""
struct one { u8 l; u8 d[l]; }
struct x {
	u8 a; one s[a];
	u8 b; one t[b];
	u8 c; one u[c];
	u8 end;
}
""", b"\x01\x01p\x02\x00\x01q\x01\x00!"),

	"arms": ("""
struct nothing { }
struct x {
	u8 a;
	variant s switch (a) { case 1: u8 p; default: nothing q; }
	u8 b;
	variant t switch (b) { case 1: u8 r; default: nothing s2; }
	u8 c;
	variant u switch (c) { case 1: u8 t2; default: nothing u2; }
	u8 end;
}
""", b"\x01p\x00\x01r!"),

	"padded": ("""
struct x {
	u8 a; u8 s[a];
	pad_to(4);
	u8 b; u8 t[b];
	pad_to(4);
	u8 c; u8 u[c];
	u8 end;
}
""", b"\x01p\x00\x00\x02qq\x00\x01r!"),

	"handed": ("""
struct w { parameter u8 n [stream]; u8 body[n]; }
struct x {
	u8 a; w(n = a) s;
	u8 b; w(n = b) t;
	u8 c; w(n = c) u;
	u8 end;
}
""", b"\x01p\x02qq\x01r!"),

	"delimited": ("""
struct x {
	u8 a; u8 s[a];
	u8 name[] until ";";
	u8 b; u8 t[b];
	u8 c; u8 u[c];
	u8 end;
}
""", b"\x01pab;\x02qq\x01r!"),

	"lead": ("""
whitespace ' ';
struct x {
	u8 a; u8 s[a];
	u8 b skip; u8 t[b];
	u8 c skip; u8 u[c];
	u8 end;
}
""", b"\x01p  \x02qq \x01r!"),
}


@pytest.mark.parametrize("shape", list(SHAPES))
def test_six_readers_agree_on_a_chain(shape: str, tmp_path: Path) -> None:
	"""Whole, cut at every length, and with a byte wrong."""
	text, whole = SHAPES[shape]
	source = tmp_path / "chain.situ"
	source.write_text(HEAD + text, encoding="ascii")
	commands = fourway.build(tmp_path, source)
	blob   = pack_text(source.read_text(encoding="ascii"))
	struct = shape_named_text(source.read_text(encoding="ascii"), "x")
	drive  = _build_driver(tmp_path, blob, VERDICT)

	rng     = random.Random(shape)
	buffers = [whole, *(whole[:at] for at in range(len(whole))),
	           *(flipped(rng, whole) for _ in range(40))]
	asked = 0
	for buffer in buffers:
		said = {target: settled(fourway.answers(command, buffer, tmp_path))
		        for target, command in commands.items()}
		assert len(set(said.values())) == 1, (buffer.hex(), said)
		try:
			walked = python_verdict(blob, buffer, struct)
		except Refused:
			continue
		answered = c_verdict(tmp_path, blob, buffer, struct, drive)
		assert answered == walked or "cannot-say" in (answered, walked), \
			buffer.hex()
		asked += 1
	assert asked, "no message reached both walkers"
