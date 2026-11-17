"""`[fills_frame]` on a fixed-size struct is enforced by all six (26.1063).

26.981 made `struct s [fills_frame]` refuse bytes after `s` in its frame,
and measured it over fuzznet's message, whose last arm runs to the frame's
end. A FIXED struct's view in C, C++ and Python was cut to the struct's
own size, so its frame was the struct, `remaining` never exceeded
`size(s)`, and those three accepted a byte after it that Rust and both
walkers refused. Found the day the construct was put in `edges`.

The root view of such a struct takes the rest of the message now. A
nested one is still cut by its parent to its own extent, where the rule
holds by construction (26.981), and the last case here says so.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

import pytest							# noqa: E402

from test_walker_c import (COMPILER, _build_driver, _run_driver,	# noqa: E402
                           image_for, shape_named)
from walker import report					# noqa: E402
from walker.image import load					# noqa: E402
from walker.walk import acquire					# noqa: E402

pytestmark = pytest.mark.skipif(COMPILER is None, reason="needs a C compiler")

SCHEMA = ("target buffer;\nendian big;\n"
          "struct exact [fills_frame] {\n\tu8 kind;\n\tu16 value;\n}\n"
          "struct holder {\n\texact inner;\n\tu8 after;\n}\n")

#: The four backends' `validate` and the walkers' verdict: 0 OK, 2 the
#: `when` refusing, and no view at all for a frame too short to hold it.
CASES = {bytes([1, 2, 3]): "validate 0",
         bytes([1, 2, 3, 4]): "validate 2",
         bytes([1, 2]): None}

VALIDATE = """if (i != 0u) {
			continue;
		}
		printf("%d\\n", (int)situ_walk_validate(&image, msg, len, shape));"""


def _schema(tmp_path: Path) -> Path:
	path = tmp_path / "exact.situ"
	path.write_text(SCHEMA, encoding="ascii")
	return path


def _section(text: str, name: str) -> list[str]:
	lines, on = [], False
	for line in text.splitlines():
		if line.startswith("-- "):
			on = line.split()[1] == name
		elif on:
			lines.append(line)
	return lines


def test_the_four_backends_refuse_a_byte_after(tmp_path: Path) -> None:
	import fourway

	command = fourway.build(tmp_path, _schema(tmp_path))
	for message, want in CASES.items():
		for backend, run in command.items():
			said = [line for line in _section(
				fourway.answers(run, message, tmp_path), "exact")
				if line.startswith("validate")]
			assert said == ([] if want is None else [want]), \
				(backend, message.hex(), said)


def test_the_walkers_agree(tmp_path: Path) -> None:
	path  = _schema(tmp_path)
	blob  = image_for(path)
	image = load(blob)
	shape = shape_named(path, "exact")
	for message, want in CASES.items():
		if want is None:
			continue
		view = acquire(image, message, shape)
		assert f"validate {report._validate(image, view, shape)}" == want


def test_a_nested_one_is_its_own_extent(tmp_path: Path) -> None:
	"""`after` follows `inner` inside `holder`, and that is not a byte
	after `exact` in ITS frame."""
	import fourway

	command = fourway.build(tmp_path, _schema(tmp_path))
	for backend, run in command.items():
		said = [line for line in _section(
			fourway.answers(run, bytes([1, 2, 3, 9]), tmp_path), "holder")
			if line.startswith("validate")]
		assert said == ["validate 0"], (backend, said)


#: A fixed struct whose `length` says how many bytes follow it, and a
#: frame holding anything else refused -- 26.980's own shape.
WRITTEN = ("target buffer;\nendian big;\n"
           "struct sized {\n\tu8 kind;\n\tu8 length;\n\tu16 value;\n}\n"
           "when 2 + sized.length != remaining refuse bytes_after "
           "\"not what length says\";\n")


def test_a_when_the_author_wrote_is_held_too(tmp_path: Path) -> None:
	"""The same vacuity for a `when` reading `remaining` that no attribute
	implied (26.980): the frame of a fixed struct was its own size."""
	import fourway

	path = tmp_path / "sized.situ"
	path.write_text(WRITTEN, encoding="ascii")
	command = fourway.build(tmp_path, path)
	for message, want in ((bytes([1, 2, 0xAA, 0xBB]), "validate 0"),
	                      (bytes([1, 2, 0xAA, 0xBB, 0xCC]), "validate 2")):
		for backend, run in command.items():
			said = [line for line in _section(
				fourway.answers(run, message, tmp_path), "sized")
				if line.startswith("validate")]
			assert said == [want], (backend, message.hex(), said)
