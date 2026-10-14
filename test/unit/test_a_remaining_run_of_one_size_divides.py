"""`x[remaining]` over fixed-size elements: the count is a division.

Four backends got this wrong in three ways, and 26.638 has the history.
What matters for a reader of this file is the rule: the bytes left are
known and so is the stride, so the number of elements is
`(limit - offset) / size` -- no walk, and never zero because nothing
answered.

The shape has no corpus instance. Both of the corpus's `remaining` runs
of structs hold variable-size elements, which take the walking branch
and have worked all along; this one reached the fall-through that
answers `array_count or 0`, which for a `remaining` run is zero. So the
fixture here is inline, and it is the regression net: the four-way
differential reports the cell that exposed it as `agreed`, because no
driver asks a run for its count.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

import pytest					# noqa: E402

from situc.codegen.c import generate as generate_c	# noqa: E402
from situc.codegen.cpp import generate as generate_cpp	# noqa: E402
from situc.codegen.python import generate as generate_py	# noqa: E402
from situc.codegen.rust import generate as generate_rs	# noqa: E402
from situc.ast import Schema			# noqa: E402
from situc.layout import solve			# noqa: E402
from situc.parser import parse_text			# noqa: E402
from situc.resolve import ResolvedSchema, resolve	# noqa: E402

RUNTIME = ROOT / "runtime"
HOST_CC = "cc"

#: Three fixed bytes per element, a varint before the run so the run's
#: offset is dynamic, and the run last so `remaining` has something to
#: mean. The dynamic offset is the half that makes a constant count
#: impossible, which is why the fall-through's zero looked plausible.
SCHEMA = """target buffer;
endian big;

varint_type edge_varint {
	encoding  = be128;
	max_bits  = 64;
	max_bytes = 9;
}

struct rec { u16 a; u8 b; }

struct held {
	u16          n [max = 4];
	edge_varint  skip;
	rec          run[remaining];
}
"""

#: `n` is 2, `skip` is a one-byte varint, then three elements.
BYTES = bytes([0x00, 0x02, 0x00, 1, 2, 3, 4, 5, 6, 7, 8, 9])


def _parts() -> tuple[Schema, ResolvedSchema]:
	"""The schema, parsed and resolved, as every backend takes it."""
	parsed = parse_text(SCHEMA)
	return parsed, resolve(parsed, solve(parsed))


def test_every_backend_states_the_count_rather_than_declining_it() -> None:
	"""The emitted source of all four, which is where they differed.

	C answered `0u`, Python `None`, and C++ and Rust declined to emit
	the accessor at all on a reason that is false for a fixed-size
	element -- *one `rec` cannot be measured from its own bytes*. It can:
	`rec` is three bytes and has its own `_required`.
	"""
	parsed, resolved = _parts()

	c = generate_c(parsed, resolved, "cell").files()["cell.h"]
	assert "situ_held_run_count" in c
	assert "return (uint32_t)0u;" not in c
	assert "/ SITU_REC_SIZE_FIXED" in c

	py = generate_py(parsed, resolved, "cell").files()["cell.py"]
	assert "def run_count" in py and "return None" not in py
	assert "// rec.SIZE_BYTES" in py

	hpp = generate_cpp(parsed, resolved, "cell").header
	assert "run_count()" in hpp and "cannot be measured" not in hpp

	rs = generate_rs(parsed, resolved, "cell").files()["cell.rs"]
	assert "fn run_count" in rs and "cannot be measured" not in rs


def test_the_python_reader_reaches_every_element() -> None:
	"""Run rather than read, because `None` compiled fine and raised.

	`run(0)` was `TypeError: '<' not supported between instances of
	'int' and 'NoneType'` -- a count that is not a number passes every
	check that reads the source and fails the first one that uses it.
	"""
	parsed, resolved = _parts()
	module = generate_py(parsed, resolved, "cell").files()["cell.py"]

	namespace: dict[str, object] = {}
	sys.path.insert(0, str(RUNTIME / "python"))
	exec(compile(module, "cell.py", "exec"), namespace)	# noqa: S102
	from situ_runtime import Message	# type: ignore[import-not-found]

	held = namespace["held"].at(Message(BYTES), 0, len(BYTES))	# type: ignore[attr-defined]
	assert held.run_count == 3
	assert [(held.run(i).a, held.run(i).b) for i in range(3)] == [
		(0x0102, 3), (0x0405, 6), (0x0708, 9)]
	with pytest.raises(IndexError):
		held.run(3)


@pytest.mark.skipif(not (ROOT / "runtime" / "c" / "situ.c").exists(),
                    reason="no C runtime")
def test_the_c_reader_reaches_every_element(tmp_path: Path) -> None:
	"""The same bytes through C, where the count was zero.

	Every index refused before this, so no element of the run was
	reachable at all -- and `validate` passed, because nothing it checks
	depends on the count.
	"""
	parsed, resolved = _parts()
	for name, text in generate_c(parsed, resolved, "cell").files().items():
		(tmp_path / name).write_text(text, encoding="utf-8")

	(tmp_path / "probe.c").write_text("""
#include <stdio.h>
#include "cell.h"

int main(void)
{
	const uint8_t raw[] = { 0x00, 0x02, 0x00, 1, 2, 3, 4, 5, 6, 7, 8, 9 };
	situ_msg_t   msg;
	situ_view_t  view;
	situ_view_t  element;

	situ_msg_init(&msg, raw, sizeof raw);
	if (situ_held_view(&msg, 0, sizeof raw, &view) != SITU_OK) return 2;

	printf("%u %d\\n", situ_held_run_count(view),
	       (int)situ_held_validate(view));
	for (uint32_t i = 0; i < 4u; i++) {
		const situ_err_t e = situ_held_run_at(view, i, &element);

		if (e != SITU_OK) { printf("%u refused\\n", i); continue; }
		printf("%u %04X %02X\\n", i, situ_rec_a_get(element),
		       situ_rec_b_get(element));
	}
	return 0;
}
""", encoding="ascii")

	done = subprocess.run(
		[HOST_CC, "-std=c11", "-O1", "-Wall", "-Wextra",
		 f"-I{RUNTIME / 'c'}", f"-I{tmp_path}",
		 str(tmp_path / "probe.c"), str(tmp_path / "cell.c"),
		 str(RUNTIME / "c" / "situ.c"), "-o", str(tmp_path / "probe")],
		capture_output=True, text=True)
	assert done.returncode == 0, done.stderr

	ran = subprocess.run([str(tmp_path / "probe")], capture_output=True,
	                     text=True, cwd=tmp_path)
	assert ran.returncode == 0, ran.stderr
	assert ran.stdout.split("\n")[:5] == [
		"3 0", "0 0102 03", "1 0405 06", "2 0708 09", "3 refused"], \
		ran.stdout
