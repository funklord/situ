"""A bound may name an arm of the member's own enum, bare (26.1094).

fuzznet writes `fzn_gather_type type [must_eq = lines]`, and the four
backends refused it -- "neither a constant nor a member" -- while `map` and
`wire` passed it, the layout skipping a bound it cannot fold and `wire`
publishing the bare word as though it named the run beside it. It is the
arm, `gather_kind.lines`, where no scalar member or `const` has the name;
a scalar sibling keeps it, being what a bound may read.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

import pytest							# noqa: E402

import fourway							# noqa: E402

SCHEMA = """target buffer;
endian big;
enum gather_kind : u8 { query = 1, lines = 2 }
enum shade_kind : u8 { idle = 0, query = 1 }
struct bare_arm {
	gather_kind  type  [must_eq = lines];
	u8           lines[remaining];
}
struct shadowed_arm {
	u8           query;
	shade_kind   type  [min = query];
}
"""


def _validate(said: str, struct: str) -> str:
	lines = said.splitlines()
	at = lines.index(f"-- {struct}")
	return next(line.split()[1] for line in lines[at + 1:]
	            if line.startswith("validate "))


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_four_backends_read_the_bare_arm_and_the_sibling(
		tmp_path: Path) -> None:
	schema = tmp_path / "arm.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	assert set(command) == {"c", "cpp", "rust", "python"}, command
	cases = [("bare_arm", b"\x02abc", "0"), ("bare_arm", b"\x01abc", "2"),
	         # shadowed: type may not be below the MEMBER `query`; read as
	         # the arm, `query = 2, type = 1` would pass.
	         ("shadowed_arm", b"\x02\x01", "2"),
	         ("shadowed_arm", b"\x01\x01", "0")]
	for struct, raw, want in cases:
		for name, argv in command.items():
			got = _validate(fourway.answers(argv, raw, tmp_path), struct)
			assert got == want, (name, struct, raw, got)
