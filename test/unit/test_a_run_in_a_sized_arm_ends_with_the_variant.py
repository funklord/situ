"""A variant that states its size frames its arm, and every reader says so.

Reported by openmlx4 (2026-11-19) from NVIDIA's MFA MAP section: one
record's `nimages` raised by one was accepted inside the section and refused
as a bare record. Two faults, in all six readers:

- **A run in such an arm was walked to the end of the FRAME, not the
  variant**, and an element reaching past the variant was dropped from the
  walk rather than refused -- the run has no accessors in an arm, so the
  `_fits` check a top-level run gets was never asked. So `sized` over
  `03 05 AA BB` validated, and so did `03 00 02 AA BB`, whose second record
  read the byte after the variant as its own. The four backends and the
  Python walker agreed on the first; the walkers agreed on the second; every
  one of them was wrong, which is why the verdicts below are computed by
  hand and not by comparing readers.
- **A variant whose only arm is `default`** -- the one way to frame a single
  struct by a length the message carries -- got no arm accessor in any
  backend, while `validate` called one: the generated C did not compile and
  `verify` died on an AttributeError.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from situc.cli import main
from test_walker_c import c_verdict, image_for, python_verdict, shape_named

SCHEMA = """target buffer;
endian big;

struct rec { u8 n; u8 d[n]; }
struct sized {
	u8 len;
	variant body[len] switch (len) { default: rec r[remaining]; }
}
struct sized_plain {
	u8 len;
	u8 kind [max = 0];
	variant body[len] switch (kind) { case 0: rec r[remaining]; default: error; }
}
struct one {
	u8 len;
	variant body[len] switch (len) { default: rec r; }
}
"""

#: (struct, bytes, refused?), each worked by hand. `len` frames the body;
#: a `rec` is its count byte and that many bytes.
CASES = [
	("sized", "03 02 aa bb", False),		# one rec, exactly the body
	("sized", "03 05 aa bb", True),			# rec wants 6, body is 3
	("sized", "03 00 02 aa bb", True),		# 2nd rec reaches the bb after it
	("sized_plain", "03 00 02 aa bb", False),
	("sized_plain", "03 00 05 aa bb", True),
	("one", "03 02 aa bb", False),
	("one", "03 05 aa bb", True),
]


@pytest.fixture(scope="module")
def schema(tmp_path_factory: pytest.TempPathFactory) -> Path:
	path = tmp_path_factory.mktemp("sized") / "sized.situ"
	path.write_text(SCHEMA, encoding="ascii")
	return path


def test_verify_refuses_an_element_past_the_variant(schema: Path,
		tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	vectors = tmp_path / "v.vectors"
	vectors.write_text("".join(f"{name} case{at} {data}\n"
	                           for at, (name, data, _) in enumerate(CASES)),
	                   encoding="ascii")

	main(["verify", str(schema), str(vectors)])
	out = capsys.readouterr().out
	assert "failed while checking" not in out, out
	for at, (name, _, refused) in enumerate(CASES):
		assert (f"{name} `case{at}` does not conform" in out) is refused, out


def test_four_backends_refuse_by_hand_computed_truth(schema: Path,
		tmp_path: Path) -> None:
	command = fourway.build(tmp_path, schema)
	for name, data, refused in CASES:
		for backend, run in command.items():
			said = fourway.answers(run, bytes.fromhex(data), tmp_path)
			current = None
			verdict = None
			for line in said.splitlines():
				if line.startswith("-- "):
					current = line[3:].split()[0]
				elif current == name and line.startswith("validate "):
					verdict = line.split()[1]
			assert verdict is not None, (backend, name, said)
			assert (verdict != "0") is refused, (backend, name, data, verdict)


def test_both_walkers_refuse_by_hand_computed_truth(schema: Path,
		tmp_path: Path) -> None:
	blob = image_for(schema)
	for name, data, refused in CASES:
		shape   = shape_named(schema, name)
		message = bytes.fromhex(data)
		c_said  = c_verdict(tmp_path, blob, message, shape)
		py_said = python_verdict(blob, message, shape)
		assert c_said == py_said, (name, data, c_said, py_said)
		assert (py_said != "0") is refused, (name, data, py_said)

