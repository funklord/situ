"""`[padded]`: a fixed-width text number's digits stop at a NUL or a space.

The copyright holder's answer to where a fixed width's digits end (26.948):
at a NUL or a space. ustar writes `mode` as `0000644` and a NUL, `chksum`
as six digits, a NUL and a space, and an unused `devmajor` as eight NULs,
and without the attribute the first and last are refused and the middle
one cannot be described at all. Opt-in, because cpio's `newc` header and
SMTP's reply code write every digit, and `25 ` is not a reply code.

Held in all four backends, both walkers and the attribute's placement:
the padding is NULs and spaces only, a digit after it is refused, a field
that is all padding is zero as every tar reader takes it, and a field
that fills its width with digits needs no terminator.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import fourway
from test_walker_c import (COMPILER, c_verdict, packed_text, python_verdict,
                           shape_named_text)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from situc.capmap import render as render_map		# noqa: E402
from situc.diagnostics import Source, SituError		# noqa: E402
from situc.layout import solve				# noqa: E402
from situc.parser import parse				# noqa: E402
from situc.resolve import resolve			# noqa: E402
from situc.wellformed import check			# noqa: E402

SCHEMA = """target buffer;
endian big;
struct entry {
	octal u32  mode[8] [padded];
	octal u32  n[4]    [padded];
	u8         d[n];
}
"""

#: The verdict, `mode`'s line, and the bytes. `n` is `1` and a NUL in each
#: but the last two, so `d` is one byte and the frame is fourteen.
CASES = {
	"seven digits and a NUL":   ("0", "mode ok=1 value=420",
	                             b"0000644\x001\x00\x00\x00x"),
	"seven digits and a space": ("0", "mode ok=1 value=420",
	                             b"0000644 1   x"),
	"three digits, then NULs":  ("0", "mode ok=1 value=420",
	                             b"644\x00\x00\x00\x00\x001\x00\x00\x00x"),
	"a NUL then spaces":        ("0", "mode ok=1 value=420",
	                             b"644\x00    1\x00 \x00x"),
	"every digit, no NUL":      ("0", "mode ok=1 value=420",
	                             b"000006441\x00\x00\x00x"),
	"all padding is zero":      ("0", "mode ok=1 value=0",
	                             b"\x00" * 8 + b"1\x00\x00\x00x"),
	"a digit after the NUL":    ("2", "mode ok=0 value=0",
	                             b"64\x004\x00\x00\x00\x001\x00\x00\x00x"),
	"a leading space":          ("2", "mode ok=0 value=0",
	                             b" 0000644" + b"1\x00\x00\x00x"),
	"an 8 is still not octal":  ("2", "mode ok=0 value=0",
	                             b"0000648\x001\x00\x00\x00x"),
	"the length, padded":       ("0", "mode ok=1 value=420",
	                             b"0000644\x002 \x00\x00xy"),
}


def test_all_four_read_padding(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for label, (verdict, line, packet) in CASES.items():
		given = {name: fourway.answers(argv, packet, tmp_path)
		         for name, argv in command.items()}
		first = next(iter(given.values()))
		assert all(answer == first for answer in given.values()), \
			(label, given)
		lines = first.splitlines()
		assert line in lines, f"{label}: {lines!r}"
		assert f"validate {verdict}" in lines, f"{label}: {lines!r}"


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_both_walkers_read_padding(tmp_path: Path) -> None:
	blob  = packed_text(SCHEMA)
	shape = shape_named_text(SCHEMA, "entry")
	for label, (verdict, _line, packet) in CASES.items():
		assert python_verdict(blob, packet, shape) == verdict, label
		assert c_verdict(tmp_path, blob, packet, shape) == verdict, label


def _compile(text: str) -> object:
	parsed = parse(Source("unit.situ", text))
	check(parsed)
	return parsed


@pytest.mark.parametrize("member", [
	"octal u32 n until \"\\x00\" [padded];",	# a delimiter ends its digits
	"octal i32 n[8] [padded];",			# a sign has a grammar
	"u32 n [padded];",				# not a text number
	"octal u32 n[8] [padded, minimal];",		# one spelling, and several
])
def test_padded_is_refused_where_it_cannot_mean_that(member: str) -> None:
	with pytest.raises(SituError):
		_compile(f"target buffer;\nendian big;\nstruct s {{\n\t{member}\n}}\n")


def test_a_padded_number_is_not_canonical() -> None:
	"""Three spellings of one value: the bytes do not follow from it."""
	parsed = _compile(SCHEMA)
	layout = solve(parsed)					# type: ignore[arg-type]
	resolved = resolve(parsed, layout)			# type: ignore[arg-type]
	text = render_map(parsed, resolved, "unit.situ")		# type: ignore[arg-type]
	mode = next(row for row in text.splitlines()
	            if row.lstrip().startswith("entry.mode "))
	assert "canonical=NonCanonical" in mode, mode
