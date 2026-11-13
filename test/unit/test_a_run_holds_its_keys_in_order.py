"""`[ascending]`: a run whose keys each sit above the one before (26.985).

hull's pack keeps an index sorted by digest, "each once", and its schema
says that is one of the things it cannot state, so `store/pack.c` checks it
by hand. `index_entry index[count] [ascending = digest]` states it: the four
backends refuse a run out of order or with a key twice, in the same order
`ordering = ascending` holds a `tlv` region's items to (26.741).

The walkers read where each key sits from the image and compare the same
bytes (26.988).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from situc.diagnostics import SituError
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve
from test_walker_c import (COMPILER, _inline_image, c_verdict, python_verdict,
                           shape_named_text)

SCHEMA = """target buffer;
endian big;
struct entry {
	u8  flags;
	u8  digest[4];
	u16 size;
}
struct by_digest {
	u8 count;
	entry index[count] [ascending = digest];
}
struct by_size {
	u8 count;
	entry index[count] [ascending = size];
}
struct by_signed {
	u8 count;
	i16 values[count] [ascending, endian = little];
}
struct by_byte {
	u8 count;
	u8 values[count] [ascending];
}
"""


def entry(digest: bytes, size: int) -> bytes:
	return bytes([0]) + digest + size.to_bytes(2, "big")


#: (struct, bytes, validate's code). 0 holds, 2 out of order.
CASES = [
	("by_digest", bytes([0]), "0"),
	("by_digest", bytes([1]) + entry(b"\x01\x02\x03\x04", 9), "0"),
	("by_digest", bytes([2]) + entry(b"\x01\x02\x03\x04", 9)
	              + entry(b"\x01\x02\x03\x05", 1), "0"),
	# The last byte decides it, and an earlier byte outranks it.
	("by_digest", bytes([2]) + entry(b"\x01\x02\x03\x05", 9)
	              + entry(b"\x01\x02\x03\x04", 1), "2"),
	("by_digest", bytes([2]) + entry(b"\x01\xff\x00\x00", 9)
	              + entry(b"\x02\x00\x00\x00", 1), "0"),
	("by_digest", bytes([2]) + entry(b"\x01\x02\x03\x04", 9)
	              + entry(b"\x01\x02\x03\x04", 9), "2"),	# twice
	("by_size", bytes([3]) + entry(b"aaaa", 1) + entry(b"bbbb", 256)
	            + entry(b"cccc", 257), "0"),
	("by_size", bytes([2]) + entry(b"aaaa", 256) + entry(b"bbbb", 255), "2"),
	# -1 then 1, little-endian: in order as numbers, not as bytes.
	("by_signed", bytes([2, 0xff, 0xff, 0x01, 0x00]), "0"),
	("by_signed", bytes([2, 0x01, 0x00, 0xff, 0xff]), "2"),
	("by_byte", bytes([3, 1, 2, 3]), "0"),
	("by_byte", bytes([3, 1, 3, 2]), "2"),
]


@pytest.mark.skipif(not fourway.COMPLETE, reason = "needs gcc, g++ and rustc")
def test_all_four_hold_the_order(tmp_path: Path) -> None:
	source = tmp_path / "unit.situ"
	source.write_text(SCHEMA, encoding = "ascii")
	command = fourway.build(tmp_path, source)
	assert len(command) == 4
	for struct, data, verdict in CASES:
		for backend, argv in command.items():
			answer  = fourway.answers(argv, data, tmp_path)
			section = answer[answer.index(f"-- {struct}\n"):].split("\n-- ")[0]
			line = next(one for one in section.splitlines()
			            if one.startswith("validate "))
			assert line.split()[1] == verdict, (struct, data.hex(), backend)


@pytest.mark.skipif(COMPILER is None, reason = "needs a C compiler")
def test_both_walkers_hold_the_order(tmp_path: Path) -> None:
	"""The image carries each ordered run's stride and key (26.988), so the
	walkers give the four backends' verdict rather than abstaining."""
	image = _inline_image(SCHEMA)
	for struct, data, verdict in CASES:
		shape = shape_named_text(SCHEMA, struct)
		assert python_verdict(image, data, shape) == verdict, (struct, data.hex())
		assert c_verdict(tmp_path, image, data, shape) == verdict, \
			(struct, data.hex())


@pytest.mark.parametrize("member, said", [
	("u8 n; u8 x[remaining] [ascending];", "a counted run"),
	("u8 n; entry x[n] [ascending];", "ordered by one of its members"),
	("u8 n; entry x[n] [ascending = nothing];", "has no member"),
	("u8 n; u8 x[n] [ascending = n];", "names no key"),
	("u8 n; f32 x[n] [ascending];", "not an integer"),
])
def test_what_cannot_be_held_is_refused(member: str, said: str) -> None:
	text = ("target buffer;\nendian big;\n"
	        "struct entry { u8 digest[4]; }\n"
	        f"struct t {{ {member} }}\n")
	with pytest.raises(SituError) as caught:
		schema = parse_text(text, path = "t.situ")
		resolve(schema, solve(schema))
	assert said in caught.value.diagnostic.render()


@pytest.mark.skipif(not fourway.COMPLETE, reason = "needs rustc")
def test_rust_holds_it_without_a_warning(tmp_path: Path) -> None:
	from test_codegen_rust import build
	built = build(tmp_path, SCHEMA, preamble = "")
	assert built.returncode == 0, built.stderr
