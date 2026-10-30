"""Nothing inside an arm in a sealed region is read unguarded (26.763).

26.760 guarded a SCALAR arm of a variant inside a sealed region and
declined every other arm shape. What it left behind was the members of a
STRUCT arm -- `case 1: pair p;` -- which every backend still read through
the gate as plain members, at their offset whatever the discriminant said:
before 26.760, C and C++ read `v_p_b` past the frame under ASan. And
26.760 made it worse in one place: C's `validate` now found the arm and
called its `_view`, which nothing emits, so the generated C did not
compile. A run arm (`case 1: u8 r[3];`) was declined by all four and still
asked by the differ.

Now a member inside such an arm is declined in words by all four, as the
arm is; C's `validate` leaves a sealed arm alone, since it runs on the
plain view; and the differ asks neither. The waived form, read on the
plain view, gets the same answer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway

from situc.cli import analyse
from situc.codegen.c import generate as generate_c
from situc.codegen.cpp import generate as generate_cpp
from situc.codegen.python import generate as generate_py
from situc.codegen.rust import generate as generate_rs
from situc.parser import parse

from test_a_tag_after_a_run_sized_inside_its_region import HEAD

PAIR = "struct pair { u8 a; u16 b; }\n"


def _inside(waived: str, arm: str) -> str:
	return ("struct inside {\n\ttag u8 mac[16] covers(body);\n"
	        f"\tsealed body(sealing_aead){waived} {{\n\t\tu8 k;\n"
	        f"\t\tvariant v switch (k) {{ case 1: {arm}; default: u8 z; }}\n"
	        "\t}\n}\n")


FORMS = {
	"struct_arm":        PAIR + _inside("", "pair p"),
	"run_arm":           _inside("", "u8 r[3]"),
	"waived_struct_arm": PAIR + _inside(" [allow_unverified_read]", "pair p"),
	"waived_run_arm":    _inside(" [allow_unverified_read]", "u8 r[3]"),
}

PACKETS = [bytes(16) + bytes(tail) for tail in
           ([1, 2], [2, 7], [1, 2, 3, 4], [2, 7, 8, 9])]


@pytest.mark.parametrize("form", sorted(FORMS))
def test_all_four_build_and_agree(form: str, tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(HEAD + FORMS[form], encoding="ascii")
	command = fourway.build(tmp_path, schema)
	assert set(command) == {"c", "cpp", "rust", "python"}, command
	for packet in PACKETS:
		given = {name: fourway.answers(argv, packet, tmp_path)
		         for name, argv in command.items()}
		first = next(iter(given.values()))
		assert all(answer == first for answer in given.values()), given


def test_no_backend_reads_a_member_of_the_arm(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(HEAD + FORMS["struct_arm"], encoding="ascii")
	source, resolved, _ = analyse(schema)
	parsed = parse(source)
	texts = {
		"C":      generate_c(parsed, resolved, "unit").header,
		"C++":    generate_cpp(parsed, resolved, "unit").header,
		"Rust":   generate_rs(parsed, resolved, "unit").module,
		"Python": generate_py(parsed, resolved, "unit").module,
	}
	for backend, text in texts.items():
		assert "v_p_b(" not in text and "v_p_b_get(" not in text, backend
