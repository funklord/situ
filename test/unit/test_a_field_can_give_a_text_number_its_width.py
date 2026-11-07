"""`[n]` on a text number counts digits, and a field can give that count.

`3:493` is hull's atom and an ISO 8583 LLVAR's shape. Refused from 26.880 to
26.924, when the four readers had four answers for it. Every verdict here is by
hand: 0 whole, 1 short, 2 refused by what the bytes say.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

import fourway
from situc.layout import solve
from situc.parser import parse_text
from test_nesting_costs_no_more_than_its_length import pack_text
from test_walker_c import (VERDICT, _build_driver, c_verdict, python_verdict,
                           shape_named_text)

HEAD = "target buffer;\nendian big;\n"

SCHEMA = HEAD + """struct g { u8 n; decimal u16 code[n]; u8 last; }
struct m { u8 n; decimal u16 code[n] [minimal]; u8 last; }
struct p { u8 n; decimal u16 code[n + 1]; u8 last; }
struct h { decimal u8 w[1]; hex u32 v[w]; u8 d[v]; u8 last; }
"""

#: struct, message, member, what it reads, the verdict.
SAMPLES = [
	("g", "03" "343933" "07", "code", "ok=1 value=493", "0"),
	("g", "01" "39" "07", "code", "ok=1 value=9", "0"),
	("g", "05" "3030303039" "07", "code", "ok=1 value=9", "0"),
	("g", "03" "343933" "07", "last", "7", "0"),
	("g", "03" "3439", "code", "ok=0 value=0", "1"),		# cut
	("g", "02" "3434", "last", "0", "1"),			# no `last`
	("g", "03" "347833" "07", "code", "ok=0 value=0", "2"),	# not a digit
	("g", "00" "07", "code", "ok=0 value=0", "2"),		# no digits
	("g", "05" "3635353336" "07", "code", "ok=0 value=0", "2"),	# past u16
	("m", "02" "3037" "07", "last", "7", "2"),			# padded
	("m", "02" "3730" "07", "code", "ok=1 value=70", "0"),
	("p", "02" "343933" "07", "code", "ok=1 value=493", "0"),
	("h", "31" "32" "aabb" "07", "v", "ok=1 value=2", "0"),
	("h", "32" "3041" + "00" * 10 + "07", "v", "ok=1 value=10", "0"),
	("h", "31" "32" "aabb", "last", "0", "1"),
]


def _section(said: str, struct: str) -> str:
	return said[said.index(f"-- {struct}\n"):].split("\n-- ")[0]


def _field(section: str, name: str) -> str:
	return next(line.split(" ", 1)[1] for line in section.splitlines()
	            if line.startswith(name + " "))


@pytest.mark.skipif(not fourway.COMPLETE, reason = "needs gcc, g++ and rustc")
def test_all_four_read_the_digits_a_field_counts(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding = "ascii")
	command = fourway.build(tmp_path, schema)
	for struct, message, name, value, verdict in SAMPLES:
		for backend, argv in command.items():
			said = _section(fourway.answers(argv, bytes.fromhex(message),
			                                tmp_path), struct)
			assert _field(said, name) == value, (backend, struct, message)
			assert _field(said, "validate") == verdict, \
				(backend, struct, message)


def test_both_walkers_give_the_same_verdicts(tmp_path: Path) -> None:
	blob  = pack_text(SCHEMA)
	drive = _build_driver(tmp_path, blob, VERDICT)
	for struct, message, _name, _value, verdict in SAMPLES:
		shape = shape_named_text(SCHEMA, struct)
		data  = bytes.fromhex(message)
		assert c_verdict(tmp_path, blob, data, shape, drive) == verdict, \
			(struct, message)
		assert python_verdict(blob, data, shape) == verdict, (struct, message)


@pytest.mark.skipif(not fourway.COMPLETE, reason = "needs gcc, g++ and rustc")
def test_six_readers_agree_on_nearly_right_messages(tmp_path: Path) -> None:
	"""Digits, counts a little off, and a byte broken here and there."""
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding = "ascii")
	command = fourway.build(tmp_path, schema)
	blob    = pack_text(SCHEMA)
	drive   = _build_driver(tmp_path, blob, VERDICT)
	rng     = random.Random(26924)
	for _ in range(150):
		digits = bytes(rng.choice(b"0123456789") for _ in range(rng.randrange(7)))
		data = bytearray(bytes([max(0, len(digits) + rng.randrange(-1, 2))])
		                 + digits + b"\x07")
		if rng.randrange(3) == 0:
			data[rng.randrange(len(data))] = rng.randrange(256)
		if rng.randrange(4) == 0:
			del data[rng.randrange(len(data)):]
		message = bytes(data)
		for struct in ("g", "m", "p"):
			said = {backend: _field(_section(
				fourway.answers(argv, message, tmp_path), struct), "validate")
				for backend, argv in command.items()
				if "no-view" not in _section(
					fourway.answers(argv, message, tmp_path), struct)}
			shape = shape_named_text(SCHEMA, struct)
			if said:
				said["c walker"] = c_verdict(tmp_path, blob, message, shape,
				                             drive)
				said["python walker"] = python_verdict(blob, message, shape)
			assert len(set(said.values())) <= 1, (struct, message.hex(), said)


@pytest.mark.parametrize("body", [
	"struct g { decimal u16 code[3]; }",
	"const W = 4;\nstruct g { decimal u32 code[W]; }",
	"struct g { decimal u32 code until \":\"; }",
	"struct g { decimal u32 n[4]; u8 d[n]; }",
])
def test_the_other_spellings_stand(body: str) -> None:
	"""A written width, a constant, a delimiter, and a number that sizes."""
	solve(parse_text(HEAD + body, path="s.situ"))
