"""A nested struct inside a sealed region is compared (26.761).

Two faults kept every such schema out of the differential:

- Python named a gated member by the last component of its path, so
  `body.p.len` was `len` on the gate where C, C++ and Rust say `p_len`,
  and the driver asking `gate.p_len` raised `AttributeError`;
- the differ's gated byte-run probe checked whether a run's count names a
  field of the REGION, and `p.body[len]` names `len` of the nested struct,
  so it asked a run all four decline (sized from inside the region,
  26.752) and the C driver did not compile.

Each form below builds all four drivers, which agree, and answers the
nested members exactly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway

from test_a_tag_after_a_run_sized_inside_its_region import HEAD

INSIDE = ("struct inside {{\n\tu8 kind;\n\ttag u8 mac[16] covers(body);\n"
          "\tsealed body(sealing_aead) {{ {0} }}\n}}\n")

#: name -> (schema, the gated lines all four print over PACKET)
FORMS = {
	"run_sized_inside": (
		"struct piece { u8 len; u8 body[len]; }\n" + INSIDE.format("piece p;"),
		["p_len 2"]),
	"fixed_run": (
		"struct piece { u8 len; u8 body[2]; }\n" + INSIDE.format("piece p;"),
		["p_len 2", "p_body len=2 last=8"]),
	"beside_a_member": (
		"struct pair { u8 a; u8 b[2]; }\n" + INSIDE.format("u8 x; pair q;"),
		["x 2", "q_a 7", "q_b len=2 last=9"]),
}

PACKET = bytes([1]) + bytes(16) + bytes([2, 7, 8, 9])


def _gated_lines(output: str) -> list[str]:
	section = output.split("-- inside\n", 1)[1].split("\n-- ", 1)[0]
	lines   = section.split("body refused=1 opened=1\n", 1)[1].splitlines()
	return [line for line in lines
	        if not line.startswith(("validate", "required()"))]


@pytest.mark.parametrize("form", sorted(FORMS))
def test_all_four_read_the_nested_members(form: str, tmp_path: Path) -> None:
	text, expected = FORMS[form]
	schema = tmp_path / "unit.situ"
	schema.write_text(HEAD + text, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	given = {name: fourway.answers(argv, PACKET, tmp_path)
	         for name, argv in command.items()}
	first = next(iter(given.values()))
	assert all(answer == first for answer in given.values()), given
	assert _gated_lines(first) == expected, first
