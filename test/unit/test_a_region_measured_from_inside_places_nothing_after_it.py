"""A region whose length is read from inside it places nothing after it
(26.759).

From outside a coded region its bytes are the codec's output, so a length
read there is not a length of anything (26.440). `traverse.region_extent`
knew the walks -- a delimited run, a `while` run, a counted run of records
-- and not the reads, and three of them reached the backends:

- a nested struct of no single size inside a `coded` region: C++, Rust
  and Python placed the member after the region by reading the nested
  struct's `len` out of the encoded bytes, and C named a
  `_p_extent` function it never emitted, so its header did not compile;
- a variant whose discriminant is inside the region: all four generators
  raised `UnknownName` rendering the arm lengths;
- a count naming an interior field, which the backends had each declined
  on their own (26.440) and the differ did not (26.758).

The same shapes driven from OUTSIDE the region are the 26.35 rule, and
still place what follows.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import fourway

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from situc.cli import analyse				# noqa: E402
from situc.codegen.c import generate as generate_c	# noqa: E402
from situc.codegen.cpp import generate as generate_cpp	# noqa: E402
from situc.codegen.python import generate as generate_py	# noqa: E402
from situc.codegen.rust import generate as generate_rs	# noqa: E402
from situc.parser import parse				# noqa: E402

from test_a_tag_after_a_run_sized_inside_its_region import HEAD	# noqa: E402

PIECE = "struct piece { u8 len; u8 body[len]; }\n"

#: name -> (schema, whether `after` is placed)
FORMS = {
	"nested_inside": (PIECE + """struct inside {
	u8  kind;
	coded body(doubling) { piece p; }
	u8  after;
}
""", False),
	"discriminant_inside": ("""struct inside {
	u8  kind;
	coded body(doubling) {
		u8  k;
		variant v switch (k) { case 1: u16 a; default: u8 b; }
	}
	u8  after;
}
""", False),
	"discriminant_outside": ("""struct inside {
	u8  k;
	coded body(doubling) {
		variant v switch (k) { case 1: u16 a; default: u8 b; }
	}
	u8  after;
}
""", True),
	"fixed_nested_inside": ("""struct piece { u8 a; u16 b; }
struct inside {
	u8  kind;
	coded body(doubling) { piece p; }
	u8  after;
}
""", True),
}

PACKET = bytes([1, 2, 0, 1, 0, 2]) + bytes(20)


@pytest.mark.parametrize("form", sorted(FORMS))
def test_the_four_agree_on_what_follows(form: str, tmp_path: Path) -> None:
	text, placed = FORMS[form]
	schema = tmp_path / "unit.situ"
	schema.write_text(HEAD + text, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	given = {name: fourway.answers(argv, PACKET, tmp_path)
	         for name, argv in command.items()}
	first = next(iter(given.values()))
	assert all(answer == first for answer in given.values()), given
	assert ("\nafter " in first) == placed, first


def test_a_sealed_discriminant_generates(tmp_path: Path) -> None:
	"""The sealed spelling, which all four generators raised on. Its
	differential still waits on a gated arm reader Python lacks, so this
	asks only that each generator answers and declines the tag."""
	schema = tmp_path / "unit.situ"
	schema.write_text(HEAD + """struct inside {
	u8  kind;
	sealed body(sealing_aead) {
		u8  k;
		variant v switch (k) { case 1: u16 a; default: u8 b; }
	}
	tag u8  mac[16] covers(body);
	u8  after;
}
""", encoding="ascii")
	source, resolved, _ = analyse(schema)
	parsed = parse(source)
	texts = {
		"C":      generate_c(parsed, resolved, "unit").header,
		"C++":    generate_cpp(parsed, resolved, "unit").header,
		"Rust":   generate_rs(parsed, resolved, "unit").module,
		"Python": generate_py(parsed, resolved, "unit").module,
	}
	assert "situ_inside_after_get" not in texts["C"]
	assert "def after(" not in texts["Python"]
	assert "pub fn after(" not in texts["Rust"]
	assert " after() const" not in texts["C++"]
