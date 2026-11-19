"""`[chars = "..."]`: the bytes a run may hold (26.1099).

fuzznet's ADDRESS host is letters, digits and `.-:`; an HTTP token is
RFC 9110's `tchar`; a DNS label is letters, digits and `-`. Each is a run
whose every byte is in a set, and `[encoding = ascii]` was the nearest
thing situ could say -- wider by every other printable byte. A class is
checked byte by byte against one 256-bit table, in every reader.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

import pytest							# noqa: E402

import fourway							# noqa: E402
from situc.chars import canonical, parse, table			# noqa: E402
from situc.diagnostics import Source, SituError			# noqa: E402
from situc.parser import parse as parse_schema			# noqa: E402
from situc.wellformed import check				# noqa: E402

HEAD = "target buffer;\nendian big;\n"

SCHEMA = HEAD + """struct address {
	u8 skip;
	u8 pre[skip];
	u8 n;
	u8 host[n] [chars = "A-Za-z0-9.:-"];
	u8 word[] until ";" [chars = "a-z"];
	u8 port[remaining] [chars = "0-9"];
}
"""


@pytest.mark.parametrize("text, admits, refuses", [
	("A-Za-z0-9.:-", b"aZ09.:-", b" _/"),
	("-a-c", b"-abc", b"d"),
	("a-", b"a-", b"b"),
	('\\x00-\\x1F"', b"\x00\x1f\"", b" "),
])
def test_a_class_is_the_bytes_it_names(text: str, admits: bytes,
		refuses: bytes) -> None:
	held = parse(text.encode().decode("unicode_escape"), None)	# type: ignore[arg-type]
	assert all(one in held for one in admits)
	assert not any(one in held for one in refuses)
	assert parse(canonical(held).encode().decode("unicode_escape"),
	             None) == held					# type: ignore[arg-type]
	bits = table(held)
	assert all(bits[one >> 3] >> (one & 7) & 1 for one in admits)


@pytest.mark.parametrize("member, why", [
	('u16 n [chars = "a"];', "means nothing here"),
	('decimal u8 n[3] [chars = "0-9"];', "means nothing here"),
	('u8 n[2] [chars = ""];', "names no byte"),
	('u8 n[2] [chars = "z-a"];', "runs backwards"),
])
def test_a_class_where_it_cannot_be_checked_is_refused(member: str,
		why: str) -> None:
	text = HEAD + f"struct s {{ {member} }}\n"
	with pytest.raises(SituError, match=why):
		check(parse_schema(Source("c.situ", text)))


def test_a_class_inside_a_coded_region_is_refused() -> None:
	"""A coded interior is the codec's input, with no accessor on the
	struct's view for any reader to check a class through."""
	text = HEAD + """codec zz {
	expansion = unbounded;
	granularity = stream;
	not seekable;
	invertible;
}
impl zz extern "zz";
struct s { coded stream(zz) { u8 a[remaining] [chars = "a"]; } }
"""
	with pytest.raises(SituError, match="member of the struct"):
		check(parse_schema(Source("c.situ", text)))


def _validate(said: str, struct: str) -> str:
	lines = said.splitlines()
	at = lines.index(f"-- {struct}")
	return next(line.split()[1] for line in lines[at + 1:]
	            if line.startswith("validate "))


#: An arm's class, named and default, checked only where its arm is
#: selected (26.1105).
TAGGED = HEAD + """struct tagged {
	u8 kind;
	u8 n;
	variant body switch (kind) {
		case 1:  u8 word[n] [chars = "a-z"];
		case 2:  u8 code[n];
		default: u8 digits[n] [chars = "0-9"];
	}
}
"""

TAGGED_CASES = [(b"\x01\x03abc", "0"), (b"\x01\x03aBc", "2"),
                (b"\x02\x03aB!", "0"),		# no class on this arm
                (b"\x07\x0242", "0"), (b"\x07\x024x", "2"),
                (b"\x01\x05ab", "1")]			# short, not a class


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_four_backends_check_an_arms_class(tmp_path: Path) -> None:
	schema = tmp_path / "tagged.situ"
	schema.write_text(TAGGED, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for raw, want in TAGGED_CASES:
		for name, argv in command.items():
			got = _validate(fourway.answers(argv, raw, tmp_path), "tagged")
			assert got == want, (name, raw.hex(), got)


def test_both_walkers_check_an_arms_class(tmp_path: Path) -> None:
	from test_walker_c import (COMPILER, VERDICT, _build_driver, c_verdict,
	                           packed_text, python_verdict, shape_named_text)
	blob  = packed_text(TAGGED)
	shape = shape_named_text(TAGGED, "tagged")
	drive = (_build_driver(tmp_path, blob, VERDICT)
	         if COMPILER is not None else None)
	for raw, want in TAGGED_CASES:
		assert python_verdict(blob, raw, shape) == want, ("python", raw.hex())
		if drive is not None:
			assert c_verdict(tmp_path, blob, raw, shape, drive) == want, \
				("c walker", raw.hex())


CASES = [(b"\x00\x05ab-9:abc;80", "0"),
         (b"\x01x\x05ab-9:abc;80", "0"),		# a run before: offset moves
         (b"\x00\x05ab_9:abc;80", "2"),		# `_` is not in the host's class
         (b"\x00\x05ab-9:aBc;80", "2"),		# nor `B` in the word's
         (b"\x00\x05ab-9:abc;8x", "2"),		# nor `x` in the port's
         (b"\x00\x05ab-9:abc;", "0")]		# an empty run holds no bad byte


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_four_backends_check_the_class(tmp_path: Path) -> None:
	schema = tmp_path / "address.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	assert set(command) == {"c", "cpp", "rust", "python"}, command
	for raw, want in CASES:
		for name, argv in command.items():
			got = _validate(fourway.answers(argv, raw, tmp_path), "address")
			assert got == want, (name, raw, got)


def test_both_walkers_check_the_class(tmp_path: Path) -> None:
	from test_walker_c import (COMPILER, VERDICT, _build_driver, c_verdict,
	                           packed_text, python_verdict, shape_named_text)
	blob  = packed_text(SCHEMA)
	shape = shape_named_text(SCHEMA, "address")
	drive = (_build_driver(tmp_path, blob, VERDICT)
	         if COMPILER is not None else None)
	for raw, want in CASES:
		assert python_verdict(blob, raw, shape) == want, ("python", raw)
		if drive is not None:
			assert c_verdict(tmp_path, blob, raw, shape, drive) == want, \
				("c walker", raw)
