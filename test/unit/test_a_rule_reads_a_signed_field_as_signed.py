"""A rule and a discriminant over a signed field, in Rust (26.920).

Every read in Rust's expressions was cast to `usize`, which is a length's type.
`i8 x; when s.x >= 0` read -1 as a huge number and refused it where the other
three did not, and `case -1` did not compile. The verdicts are by hand, since
the fault was one backend's and the point is which one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway

SCHEMA = """target buffer;
endian big;
varint_type z { encoding = leb128; transform = zigzag; max_bits = 32; }
struct flat { i8 x; u8 y; }
struct coded { z n; u8 y; }
struct inner { i8 x; }
struct nested { inner g; u8 y; }
struct chosen { i8 k; variant v switch (k) {
	case -1: u8 a; case 1: u16 b; default: u32 c; } }
when flat.x >= 0 refuse p "p";
when coded.n >= 0 refuse q "q";
when nested.g.x >= 0 refuse r "r";
"""

#: struct -> message -> `validate`. `03` is -2 under zigzag and `04` is 2.
CASES = {
	"flat":   {"ff00": "0", "0100": "2"},
	"coded":  {"0300": "0", "0400": "2"},
	"nested": {"ff00": "0", "0100": "2"},
	"chosen": {"ff00": "0", "010000": "0", "0200000000": "0", "02000000": "1"},
}


def _validate(answers: str, struct: str) -> str:
	section = answers[answers.index(f"-- {struct}\n"):].split("\n-- ")[0]
	if "no-view" in section:
		return "1"
	return next(line.split()[1] for line in section.splitlines()
	            if line.startswith("validate "))


@pytest.mark.skipif(not fourway.COMPLETE, reason = "needs gcc, g++ and rustc")
def test_all_four_read_it_as_signed(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding = "ascii")
	command = fourway.build(tmp_path, schema)
	for struct, cases in CASES.items():
		for message, verdict in cases.items():
			given = {backend: _validate(fourway.answers(
			             argv, bytes.fromhex(message), tmp_path), struct)
			         for backend, argv in command.items()}
			assert set(given.values()) == {verdict}, (struct, message, given)
