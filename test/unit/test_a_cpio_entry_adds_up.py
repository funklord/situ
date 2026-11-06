"""`newc_crc`'s check is stated, and held to GNU cpio's own archive (26.917).

`example/cpio` said the sum was not one situ could generate. A `when` reaches
the nested `check` now (26.915) and `sum` reads the file's bytes, wherever the
name put them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from every_schema import ROOT
from situc.diagnostics import SituError
from situc.layout import solve
from situc.parser import parse_text
from test_walker_c import (COMPILER, _inline_image, c_verdict, python_verdict,
                           shape_named_text)

SCHEMA = ROOT / "example" / "cpio" / "cpio.situ"


def vector(name: str) -> bytes:
	"""A committed vector's bytes: what GNU cpio wrote."""
	text = (ROOT / "example" / "cpio" / "cpio.vectors").read_text(encoding = "ascii")
	line = next(one for one in text.splitlines()
	            if one.startswith(f"cpio_entry {name} "))
	return bytes.fromhex("".join(line.split()[2:]))


def tampered() -> bytes:
	"""One bit of the file changed, with the check left alone."""
	data = bytearray(vector("greeting_crc"))
	data[data.index(b"hello") + 1] ^= 1
	return bytes(data)


def messages() -> dict[str, tuple[bytes, str]]:
	return {"crc": (vector("greeting_crc"), "0"),
	        "newc": (vector("greeting"), "0"),
	        "tampered": (tampered(), "2")}


@pytest.mark.skipif(not fourway.COMPLETE, reason = "needs gcc, g++ and rustc")
def test_all_four_add_it_up(tmp_path: Path) -> None:
	command = fourway.build(tmp_path, SCHEMA)
	for label, (data, verdict) in messages().items():
		for backend, argv in command.items():
			answer = fourway.answers(argv, data, tmp_path)
			section = answer[answer.index("-- cpio_entry\n"):].split("\n-- ")[0]
			line = next(one for one in section.splitlines()
			            if one.startswith("validate "))
			assert line.split()[1] == verdict, (label, backend)


@pytest.mark.skipif(COMPILER is None, reason = "needs a C compiler")
def test_both_walkers_add_it_up(tmp_path: Path) -> None:
	text  = SCHEMA.read_text(encoding = "ascii")
	image = _inline_image(text)
	shape = shape_named_text(text, "cpio_entry")
	for label, (data, verdict) in messages().items():
		assert python_verdict(image, data, shape) == verdict, label
		assert c_verdict(tmp_path, image, data, shape) == verdict, label


def test_a_byte_enum_in_a_when_is_a_diagnostic() -> None:
	"""It was `UnknownName` out of the generator."""
	with pytest.raises(SituError) as caught:
		solve(parse_text(
			"target buffer;\nendian big;\n"
			'enum format : u8[2] { a = "AA", b = "BB" }\n'
			"struct s { format m; u8 k; }\n"
			'when s.k == format.a refuse bad "m";\n'))
	assert "an enum of bytes" in str(caught.value)
