"""`u8 ciphertext[remaining]; u8 tag[16];`: the frame less what follows (26.894).

fuzznet's blob leaf is `commitment || ciphertext || tag` and carries no length,
on purpose: the container frames it. So the middle is the frame less the tail,
and 26.669 left that open for want of a decision.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from situc.diagnostics import SituError
from situc.layout import solve
from situc.parser import parse_text

HEAD = "target buffer;\nendian big;\n"
LEAF = HEAD + """struct leaf {
	u8  commitment[4];
	u8  ciphertext[remaining];
	u8  tag[2];
	u16 sequence;
}
"""


def refusal(body: str) -> str:
	with pytest.raises(SituError) as caught:
		solve(parse_text(HEAD + body, path="s.situ"))
	return caught.value.diagnostic.render()


def run_of(body: str, name: str) -> int:
	solved = solve(parse_text(HEAD + body, path="s.situ"))
	found  = next(held for layout in solved.structs.values()
	              for held in layout.placements if held.name == name)
	return found.tail_bytes


def test_the_run_leaves_what_follows() -> None:
	assert run_of(LEAF.removeprefix(HEAD), "ciphertext") == 4


def test_a_run_with_nothing_after_it_leaves_nothing() -> None:
	assert run_of("struct s { u8 a; u8 rest[remaining]; }", "rest") == 0


def test_a_nested_fixed_struct_counts() -> None:
	assert run_of("struct t { u16 a; u8 b; }\n"
	              "struct s { u8 rest[remaining]; t last; u8 z; }", "rest") == 4


@pytest.mark.parametrize("after", [
	"u8 n; u8 z[n];",
	'u8 z[] until ",";',
	"u8 more[remaining];",
])
def test_what_follows_has_to_be_a_number(after: str) -> None:
	said = refusal(f"struct s {{ u8 rest[remaining]; {after} }}\n")
	assert "must be the last member of its frame" in said
	assert "fixed-size" in said


def test_a_run_of_records_keeps_its_refusal() -> None:
	"""It walks to the frame's end, and has no length to take a tail from."""
	said = refusal("struct r { u8 a; }\n"
	               "struct s { r rest[remaining]; u8 z; }\n")
	assert "must be the last member of its frame" in said


def test_an_arm_with_something_after_the_variant_keeps_its_refusal() -> None:
	"""26.782: the arm is not the frame's last, and only one arm has the run."""
	said = refusal("struct s { u8 k; variant v switch (k) {"
	               " case 1: u8 rest[remaining]; default: u8 one; } u16 z; }\n")
	assert "must be the last member" in said


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_the_four_find_the_tail_from_the_end(tmp_path: Path) -> None:
	source = tmp_path / "unit.situ"
	source.write_text(LEAF, encoding="ascii")
	command = fourway.build(tmp_path, source)
	assert len(command) == 4
	whole = bytes(range(1, 14))
	for packet in (whole, *(whole[:length] for length in range(len(whole)))):
		given = {name: fourway.answers(argv, packet, tmp_path)
		         for name, argv in command.items()}
		assert len(set(given.values())) == 1, (packet.hex(), given)
	said = fourway.answers(command["c"], whole, tmp_path)
	assert "ciphertext len=5 first=5" in said
	assert "tag len=2 first=10" in said
	assert "sequence 3085" in said
