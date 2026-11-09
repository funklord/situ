"""An arm a message's version predates frames as no bytes (26.947).

`edges.versioned_arm` is `u8 rev; u8 pick;` then a variant whose second arm
is `[since = 2]`. A message with `rev = 1` and `pick = 2` selects an arm
its own version predates. The accessors have refused that arm with VERSION
since 26.424, and `view` and `validate` accepted the two bytes -- but
`required` answered a constant 3, and both walkers measured 3, so the
readers disagreed about where the message ends. The copyright holder's
answer: frame it as absent.

Held in all four backends' `required` and in both walkers, which measure
the variant themselves.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

import fourway

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from situc.codegen.c import generate as generate_c		# noqa: E402
from situc.codegen.python import generate as generate_py	# noqa: E402
from situc.diagnostics import Source				# noqa: E402
from situc.layout import solve					# noqa: E402
from situc.parser import parse					# noqa: E402
from situc.resolve import resolve				# noqa: E402

from test_walker_c import (COMPILER, c_widths, image_for,	# noqa: E402
                           python_widths)

EDGES = ROOT / "test" / "schema" / "edges.situ"

#: message -> where it ends. `0102` and `010209` select the version-2 arm
#: at version 1, so the message is the two bytes in front of it; the rest
#: carry their arm.
ENDS = {"0102": 2, "010209": 2, "010105": 3, "020209": 3}


def _resolved() -> tuple[object, object]:
	parsed = parse(Source(str(EDGES), EDGES.read_text(encoding="ascii")))
	return parsed, resolve(parsed, solve(parsed))


def _shape() -> int:
	parsed, _ = _resolved()
	return [s.name for s in parsed.structs()].index("versioned_arm")	# type: ignore[attr-defined]


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_the_c_backend_frames_it_absent(tmp_path: Path) -> None:
	parsed, resolved = _resolved()
	built = generate_c(parsed, resolved, "edges")			# type: ignore[arg-type]
	(tmp_path / "edges.h").write_text(built.header, encoding="ascii")
	(tmp_path / "edges.c").write_text(built.source, encoding="ascii")
	calls = "\n".join(
		f'\task((const uint8_t *)"{"".join(f"\\x{m[i:i + 2]}" for i in range(0, len(m), 2))}", {len(m) // 2}u);'
		for m in ENDS)
	(tmp_path / "m.c").write_text(f'''#include <stdio.h>
#include "edges.h"
static void ask(const uint8_t *b, uint32_t n)
{{
	uint32_t need = 0u;
	situ_err_t e = situ_versioned_arm_required(b, n, &need);
	printf("%d:%u ", e == SITU_OK, need);
}}
int main(void)
{{
{calls}
	return 0;
}}
''', encoding="ascii")
	compiled = subprocess.run(
		[COMPILER or "cc", "-std=c11", "-Wall", "-Wextra",
		 f"-I{ROOT / 'runtime' / 'c'}", f"-I{tmp_path}",
		 str(tmp_path / "m.c"), str(tmp_path / "edges.c"),
		 str(ROOT / "runtime" / "c" / "situ.c"), "-o", str(tmp_path / "m")],
		capture_output=True, text=True)
	assert compiled.returncode == 0, compiled.stderr
	run = subprocess.run([str(tmp_path / "m")], capture_output=True,
	                     text=True, timeout=60)
	assert run.stdout.split() == [f"1:{end}" for end in ENDS.values()]


def test_the_python_backend_frames_it_absent(tmp_path: Path) -> None:
	parsed, resolved = _resolved()
	path = tmp_path / "edges_py.py"
	path.write_text(generate_py(parsed, resolved, "edges_py").module,	# type: ignore[arg-type]
	                encoding="ascii")
	if "situ_runtime" not in sys.modules:
		spec = importlib.util.spec_from_file_location(
			"situ_runtime", ROOT / "runtime" / "python" / "situ_runtime.py")
		assert spec is not None and spec.loader is not None
		runtime = importlib.util.module_from_spec(spec)
		sys.modules["situ_runtime"] = runtime
		spec.loader.exec_module(runtime)
	spec = importlib.util.spec_from_file_location("edges_py", path)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)

	for message, end in ENDS.items():
		data = bytes.fromhex(message)
		assert module.versioned_arm.required(data) == end, message
		# One byte short of the end is short, never complete.
		with pytest.raises(module.TruncatedError):
			module.versioned_arm.required(data[:end - 1])


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_both_walkers_measure_it_absent(tmp_path: Path) -> None:
	blob  = image_for(EDGES)
	shape = _shape()
	for message, end in ENDS.items():
		data = bytes.fromhex(message)
		held = str(end - 2)		# rev and pick are a byte each
		assert python_widths(blob, data, shape)[2] == held, message
		assert c_widths(tmp_path, blob, data, shape)[2] == held, message


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs gcc, g++ and rustc")
def test_all_four_backends_frame_it_absent(tmp_path: Path) -> None:
	"""The differential drivers' `required()` line, in all four: an answer
	per prefix, `*` marking complete. C++ and Rust take the change through
	the same `_variant_length`, and this is what says they took it."""
	command = fourway.build(tmp_path, EDGES)
	for message, end in ENDS.items():
		data = bytes.fromhex(message)
		for backend, argv in command.items():
			said = fourway.answers(argv, data, tmp_path)
			held = said.split("-- versioned_arm\n")[1].split("\n-- ")[0]
			line = next(one for one in held.splitlines()
			            if one.startswith("required()"))
			# One answer per prefix from the empty one: complete at the
			# message's own end, never before it, and no shorter prefix
			# asking for more than that end.
			answers = line.split()[1:]
			assert answers[end] == f"*{end}", (backend, message, line)
			assert all(not one.startswith("*") and int(one) <= end
			           for one in answers[:end]), (backend, message, line)
