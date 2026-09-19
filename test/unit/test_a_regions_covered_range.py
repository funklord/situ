"""What a tag says it covers, run rather than read (26.448).

An `authenticated` region is not a member: it consumes no bytes of its
own, so `own_members` drops it. Three backends nevertheless asked the
region placement for its own offset, and the answer is the struct's
SIZE_MIN plus its variable part -- the END of the frame, not where the
region sits.

Two symptoms from that one cause, both on shapes the corpus already
had. A NON-EMPTY dynamic region came out inverted, `mac covers 10..6`,
so the accessor always refused and that MAC could not be computed at
all outside C. An EMPTY one came out as a zero-length range parked at
the frame's end -- harmless, and disagreeing with C, which had its own
version of the fault and the dangerous one: `0u` to `view.limit`, a tag
covering the whole frame including its own bytes.

C was right about the non-empty case throughout, which is why the four
cells below are the test rather than any single value.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import cast

from situc.codegen.python import generate as generate_py
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve

from every_schema import ROOT


def load(tmp_path: Path, text: str) -> tuple[ModuleType, ModuleType]:
	"""Generate this schema's Python module and import it."""
	schema   = parse_text(text)
	resolved = resolve(schema, solve(schema))
	(tmp_path / "unit.py").write_text(
		generate_py(schema, resolved, "unit").module, encoding="ascii")

	if "situ_runtime" not in sys.modules:
		spec = importlib.util.spec_from_file_location(
			"situ_runtime", ROOT / "runtime" / "python" / "situ_runtime.py")
		assert spec is not None and spec.loader is not None
		runtime = importlib.util.module_from_spec(spec)
		sys.modules["situ_runtime"] = runtime
		spec.loader.exec_module(runtime)

	spec = importlib.util.spec_from_file_location("unit", tmp_path / "unit.py")
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module, sys.modules["situ_runtime"]


def covered(module: ModuleType, runtime: ModuleType, name: str,
		buf: bytes) -> tuple[int, int]:
	"""`mac_covered()` for this struct over these bytes.

	A variable-length struct's `at` takes an extent and a fixed-length
	one does not, so both spellings are tried. Asked of the class rather
	than of the layout, because which of the two a struct gets is the
	generator's decision and not something this test should restate.
	"""
	held = getattr(module, name)
	message = runtime.Message(bytearray(buf))
	try:
		view = held.at(message, 0, len(buf))
	except TypeError:
		view = held.at(message, 0)
	return cast("tuple[int, int]", view.mac_covered())


PREAMBLE = "target buffer;\nendian big;\n"

#: The four cells. `region` is empty or not; `position` is static or
#: decided by the message. Only the product of EMPTY and DYNAMIC reached
#: C's `0u`, and only DYNAMIC reached the other three's frame-end -- so a
#: test of one cell cannot tell the two causes apart.
EMPTY_DYNAMIC = PREAMBLE + """
struct s {
	u8 n;
	u8 v[n];
	authenticated body { }
	tag u8 mac[4] covers(body);
}
"""

FULL_DYNAMIC = PREAMBLE + """
struct s {
	u8 n;
	u8 v[n];
	authenticated body { u8 x; }
	tag u8 mac[4] covers(body);
}
"""

EMPTY_STATIC = PREAMBLE + """
struct s {
	u8 n;
	authenticated body { }
	tag u8 mac[4] covers(body);
}
"""

FULL_STATIC = PREAMBLE + """
struct s {
	u8 n;
	authenticated body { u8 x; }
	tag u8 mac[4] covers(body);
}
"""


def test_a_corpus_tag_could_not_compute_its_own_range(tmp_path: Path) -> None:
	"""`edges.covered_tail`, which the corpus has carried all along.

	`n [max = 4]`, `pad[n]`, then `authenticated body { beacon head; }`
	and `tag u8 mac[4] covers(body)`. With `n = 2` the beacon sits at 3
	and is three bytes, so the answer is (3, 3) -- which is what C
	answers and what this backend now answers.

	Before the fix this raised `BoundsError: mac covers 10..6`. Not
	"covered the wrong bytes": the range was INVERTED, so the accessor
	refused every message and the MAC was uncomputable in three of the
	four backends. No test called it in any of them -- the generated C
	checks call `_covered` for two other structs, and C was the one
	backend that had it right.
	"""
	module, runtime = load(
		tmp_path, ROOT.joinpath("test/schema/edges.situ").read_text())
	buf = bytes([2, 0xAA, 0xBB, 0x00, 0x01, 0x07, 1, 2, 3, 4])
	view = module.covered_tail.at(runtime.Message(bytearray(buf)), 0, len(buf))
	assert view.mac_covered() == (3, 3)


def test_an_empty_region_behind_a_variable_member(tmp_path: Path) -> None:
	"""The cell that was wrong in all four, two different ways.

	`n = 2`, so `v` is two bytes and the empty region sits at 3, where
	`mac` also begins. A tag over nothing is length 0 -- the value C
	answered was (0, frame), its own bytes included.
	"""
	module, runtime = load(tmp_path, EMPTY_DYNAMIC)
	assert covered(module, runtime, "s", bytes([2, 0xAA, 0xBB, 1, 2, 3, 4])) \
		== (3, 0)


def test_a_full_region_behind_a_variable_member(tmp_path: Path) -> None:
	"""The cell that shares the cause and not the symptom: one byte at 3."""
	module, runtime = load(tmp_path, FULL_DYNAMIC)
	assert covered(module, runtime, "s",
	               bytes([2, 0xAA, 0xBB, 0x5A, 1, 2, 3, 4])) == (3, 1)


def test_an_empty_region_at_a_static_offset(tmp_path: Path) -> None:
	"""CONTROL. Static placement returns before any of this, and was
	already correct -- `start = 1u; end = 1u`. It has to stay that way,
	or a fix to the dynamic path has been paid for from here."""
	module, runtime = load(tmp_path, EMPTY_STATIC)
	assert covered(module, runtime, "s", bytes([0, 1, 2, 3, 4])) == (1, 0)


def test_a_full_region_at_a_static_offset(tmp_path: Path) -> None:
	"""CONTROL, and the only cell that was right in all four backends
	before the fix. If this one ever moves, the change was not the one
	described here."""
	module, runtime = load(tmp_path, FULL_STATIC)
	assert covered(module, runtime, "s", bytes([0, 0x5A, 1, 2, 3, 4])) == (1, 1)
