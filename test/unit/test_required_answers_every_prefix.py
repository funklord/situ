"""`required` is asked about every PREFIX, not about a whole message.

**This file is the instrument that found four faults in one afternoon.**
A framing function has one answer per prefix length; a complete buffer is
the one input that cannot fail, so testing it there tests nothing.

The sweep was not new -- `test_codegen_rust.py::test_every_prefix_of_a_
real_request_answers_honestly` has done it since 26.32, and a record of
this pass wrongly said nothing in the tree did. What was true is
narrower: it existed for ONE backend and ONE schema, and `example/http`
has no counted run of records with a fixed member after it and no varint
at all. So the shape carrying three of these four faults was outside the
one sweep there was. Three lines of loop over four shapes found:

- 26.683, `_span` shrinking on a truncated element, so two bytes of six
  came back complete;
- 26.684, a nested member's extent shrinking the same way, so
  `outer { u8 h; col c; }` answered complete at two bytes of five -- open,
  recorded, not fixed here;
- 26.685, the running TOTAL passed as the element's OFFSET, so a run with
  a fixed member after it answered complete at five bytes of six;
- and a single varint's `_len` answering zero for one it cannot read, so
  `one { uvar v; }` called a one-byte buffer whole and `col`'s walk then
  took three such elements and called two bytes of five complete.

**The safety property is the one worth asserting**, and it is one-sided:
`required` may report a message INCOMPLETE when it is complete -- the cost
is one more read of a socket the caller was reading anyway -- and may
never report it COMPLETE when it is not, because the caller then stops
reading and parses a message with bytes missing.

Two more follow from being usable at all. The answer must be monotone:
`need` that goes backwards as bytes arrive is a caller looping. And where
a message is incomplete, `need` must EXCEED what has arrived, or a caller
asking "how many more" gets zero and never progresses.

The sequences are pinned as well as the properties, because the properties
alone admit the loose answers these faults were fixed from: a lower bound
of `have + 1` satisfies all three and still asks a caller for a message's
bytes one at a time.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pytest						# noqa: E402

from situc.codegen.python import generate			# noqa: E402
from situc.layout import solve				# noqa: E402
from situc.parser import parse_text			# noqa: E402
from situc.resolve import resolve			# noqa: E402

RUNTIME = ROOT / "runtime" / "python"

#: The shapes, the bytes of one whole message, and what `required` answers
#: for each prefix length -- `None` where it reports complete.
#:
#: Read as a table of framing answers: a row of numbers is what a caller
#: asking "how many bytes do you need" is told after each read.
SHAPES = """target buffer;
endian little;

varint_type uvar { encoding = leb128; max_bits = 64; max_bytes = 10; }

struct one { uvar v; }

// A run of records that is the last member: the count is all that bounds
// it, so nothing follows to make the total differ from the offset.
struct col {
	uvar count [max = 100];
	one  d[count];
}

// The same run with a FIXED member after it, which is 26.685: the total
// opens at 3 and the run starts at 1.
struct withtail {
	u8   n;
	one  d[n];
	u16  t;
}

// A run of VARINTS as the last member, which is what a loose minimum got
// wrong -- one byte credited to an element that had already spent one.
struct lastrun {
	u8    n;
	uvar  v[n];
}

// One varint and nothing else: the shape whose `_len` answers zero for a
// value it cannot read.
struct lone {
	uvar  v;
	u8    k;
}
"""

#: struct -> (whole message, [answer per prefix length, 1..len]).
#: `None` means "reported complete".
CASES: dict[str, tuple[bytes, list[int | None]]] = {
	"col":      (bytes([0x03, 0x81, 0x01, 0x05, 0x06]),
	             [4, 5, 5, 5, None]),
	"withtail": (bytes([2, 0x81, 0x01, 0x05, 0x00, 0x00]),
	             [3, 3, 6, 6, 6, None]),
	"lastrun":  (bytes([1, 0x81, 0x01]),
	             [2, 3, None]),
	"lone":     (bytes([0x81, 0x01, 0x77]),
	             [2, 3, None]),
}


def _runtime() -> ModuleType:
	cached = sys.modules.get("situ_runtime")
	if cached is not None:
		return cached
	spec = importlib.util.spec_from_file_location(
		"situ_runtime", RUNTIME / "situ_runtime.py")
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	sys.modules["situ_runtime"] = module
	spec.loader.exec_module(module)
	return module


@pytest.fixture(scope="module")
def shapes(tmp_path_factory: pytest.TempPathFactory) -> ModuleType:
	"""The generated module, built once for every case below.

	The Python backend because it needs no compiler: the same answers are
	compared across all four in the differential, and what this file is
	for is the PREFIX axis rather than the backend axis.
	"""
	schema   = parse_text(SHAPES)
	resolved = resolve(schema, solve(schema))
	path     = tmp_path_factory.mktemp("prefix") / "shapes.py"
	path.write_text(generate(schema, resolved, "shapes").module,
	                encoding="ascii")

	_runtime()
	spec = importlib.util.spec_from_file_location("shapes", path)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def _answers(module: ModuleType, name: str,
		whole: bytes) -> list[int | None]:
	"""What `required` says for each prefix, `None` for complete."""
	view = getattr(module, name)
	out: list[int | None] = []
	for n in range(1, len(whole) + 1):
		try:
			view.required(whole[:n])
			out.append(None)
		except module.TruncatedError as short:
			out.append(short.needed)
	return out


@pytest.mark.parametrize("name", sorted(CASES))
def test_no_prefix_is_reported_complete(name: str,
		shapes: ModuleType) -> None:
	"""The safety property, and the only one-sided one.

	Reporting a short message complete is what makes a caller stop reading
	and parse bytes that are not there. Reporting a complete one short
	costs a read it was making anyway.
	"""
	whole, _ = CASES[name]
	got = _answers(shapes, name, whole)
	for n, answer in enumerate(got[:-1], start=1):
		assert answer is not None, (
			f"{name}: {n} bytes of {len(whole)} reported COMPLETE")
	assert got[-1] is None, (
		f"{name}: the whole message was not reported complete")


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_answer_never_goes_backwards(name: str,
		shapes: ModuleType) -> None:
	"""Monotone, or a caller loops.

	`need` is a lower bound on a total that does not change as bytes
	arrive, so an answer that shrinks is a quantity being read that
	shrinks -- which is the whole of 26.683, 26.684 and 26.685.
	"""
	whole, _ = CASES[name]
	seen = [answer for answer in _answers(shapes, name, whole)
	        if answer is not None]
	assert seen == sorted(seen), f"{name}: `need` went backwards: {seen}"


@pytest.mark.parametrize("name", sorted(CASES))
def test_an_incomplete_message_asks_for_more_than_arrived(name: str,
		shapes: ModuleType) -> None:
	"""Or "how many more" is zero and the caller cannot progress.

	This is what a `need` of 0 did: `one { uvar v; }` answered OK with 0
	for a truncated varint, and `col` summed three of those into a total
	BELOW what had already arrived.
	"""
	whole, _ = CASES[name]
	for n, answer in enumerate(_answers(shapes, name, whole), start=1):
		if answer is not None:
			assert answer > n, (
				f"{name}: {n} bytes arrived and `need` is {answer}, "
				f"so a caller is told to read {answer - n} more")


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_answers_are_the_ones_measured(name: str,
		shapes: ModuleType) -> None:
	"""The pin, because the properties above admit a useless answer.

	`have + 1` at every step satisfies safety, monotonicity and progress,
	and asks a caller for a message one byte at a time. These numbers are
	exact from the point the message determines them.
	"""
	whole, want = CASES[name]
	assert _answers(shapes, name, whole) == want
