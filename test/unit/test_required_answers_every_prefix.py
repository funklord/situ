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
  `outer { u8 h; col c; }` answered complete at two bytes of five, and
  the same behind a discriminant, which is what bbq-predictor reported;
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
from situc.traverse import framing_floor		# noqa: E402

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

// A NESTED variable struct, which is 26.684: the extent shrinks when the
// bytes run out, so `required` reported complete for anything carrying
// the fixed prefix.
struct outer {
	u8   h;
	col  c;
}

// A WIDE FIXED PREFIX in front of a counted run of records, which is the
// arrangement none of the shapes above has: every one of them reads its
// count from offset 0 or from a varint there, so one byte already carries
// it. This one reads a `u16` at offset 8. bbq-predictor's `stations_body`
// is this shape member for member, and it carried two faults the sweep
// could not see -- an out-of-bounds read of the count (26.723) and a
// `need` that fell as bytes arrived (26.724).
//
// `[min = 1]` on the element's length is load-bearing: it is what makes
// `item`'s SIZE_MIN two where its STRUCTURAL minimum is one, and the gap
// between those two numbers is the whole of 26.724.
struct item {
	u8  len [min = 1, max = 32];
	u8  id[len];
}

struct wide {
	i64   stamp;
	u16   count [max = 4096];
	item  ids[count];
}

struct nothing { }

// The same fault behind a discriminant, which is the shape
// bbq-predictor reported: `framed` inside a variant arm answered OK for
// a three-byte buffer where the arm's own struct says four.
struct arms {
	u8  has;
	variant x switch (has) {
		case 0:  nothing  none;
		default: col      c;
	}
}
"""

#: struct -> (whole message, [answer per prefix length, 1..len]).
#: `None` means "reported complete".
CASES: dict[str, tuple[bytes, list[int | None]]] = {
	"col":      (bytes([0x03, 0x81, 0x01, 0x05, 0x06]),
	             [4, 5, 5, 5, None]),
	# Was `[3, 3, ...]` while `required` opened with a floor of `SIZE_MIN`:
	# three bytes, the fixed `n` and `t` with the run counted as empty. The
	# floor went in 26.693 and the walk answers, which is TIGHTER -- it
	# reads `n = 2` and knows two varint elements must follow, so five at
	# one byte and six at two. The floor can only beat the walk where it is
	# derived from a constraint the bytes violate, which is the case that
	# made it unsafe.
	"withtail": (bytes([2, 0x81, 0x01, 0x05, 0x00, 0x00]),
	             [5, 6, 6, 6, 6, None]),
	"lastrun":  (bytes([1, 0x81, 0x01]),
	             [2, 3, None]),
	"lone":     (bytes([0x81, 0x01, 0x77]),
	             [2, 3, None]),
	"outer":    (bytes([0xAA, 0x03, 0x81, 0x01, 0x05, 0x06]),
	             [2, 5, 6, 6, 6, None]),
	# Eight zero bytes, `count = 2`, then a one-byte id and a two-byte
	# one: fifteen bytes in all. The first ten answers are the fixed
	# prefix, which is 26.723's guard -- before it, C++ read the count out
	# of the buffer and the other three answered from a lax zero.
	#
	# Then 12, 13, 13, 15, 15. The pair of 13s is 26.724: the element
	# ahead completes and `at` grows by the two bytes it took, while the
	# tail stops crediting it with the one byte an element structurally
	# needs. Quoting `item.SIZE_MIN` there instead made that step
	# 14 -> 13 and broke monotonicity in a CONFORMANT schema.
	"wide":     (bytes(8) + bytes([0x02, 0x00])
	             + bytes([0x01, 0x78]) + bytes([0x02, 0x79, 0x79]),
	             [10, 10, 10, 10, 10, 10, 10, 10, 10,
	              12, 13, 13, 15, 15, None]),
	# The variant answers exactly what the plain nested case does, which
	# is the point: the discriminant costs a byte and changes nothing
	# else about the framing.
	"arms":     (bytes([0x01, 0x03, 0x81, 0x01, 0x05, 0x06]),
	             [2, 5, 6, 6, 6, None]),
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
def test_the_walker_frames_the_same_prefixes(name: str) -> None:
	"""The walker's `Reader` against the generated `required`.

	26.684's record asked for this rather than a test of the walker's
	own: the walker exists to be a second opinion, so what matters is
	that it agrees about WHICH prefixes are whole messages -- not that
	its own numbers are self-consistent.

	`Reader` framed through `struct_extent` until this pass, and an
	extent measures what is THERE. Measured before the fix: `col` handed
	back a whole message at ONE byte of five, and again at two, three
	and four, each with a different length. It asks `frame_min` now, and
	takes the extent afterwards for the length -- two questions, which
	is the distinction the fault turned on.
	"""
	from situc import pack as packer			# noqa: PLC0415
	from walker.image import load			# noqa: PLC0415
	from walker.session import Reader		# noqa: PLC0415

	schema   = parse_text(SHAPES)
	resolved = resolve(schema, solve(schema))
	blob, _  = packer.pack(schema, resolved, metadata=True)
	image    = load(blob)
	index    = {image.struct_name(i): i
	            for i in range(len(image.structs))}[name]

	whole, want = CASES[name]
	for n, answer in enumerate(want, start=1):
		reader = Reader(image, index)
		reader.push(whole[:n])
		got = reader.next()
		if answer is not None:
			assert got is None, (
				f"{name}: the walker calls {n} bytes of {len(whole)} a "
				f"whole message, {len(whole) - n} short")
		else:
			assert got is not None, (
				f"{name}: the walker does not see the whole message")
			assert reader.ready == len(whole), (
				f"{name}: the walker frames {reader.ready} bytes where "
				f"the message is {len(whole)}")


#: Every schema in the corpus, for the one claim below that is about the
#: corpus rather than about these shapes.
SCHEMAS = sorted((ROOT / "example").glob("*/*.situ"))


def _corpus_module(path: Path, out: Path) -> ModuleType:
	"""The Python backend for one corpus schema."""
	from situc.diagnostics import Source			# noqa: PLC0415
	from situc.parser import parse			# noqa: PLC0415

	schema   = parse(Source(str(path), path.read_text(encoding="ascii")))
	resolved = resolve(schema, solve(schema))
	module   = out / (path.stem + ".py")
	module.write_text(generate(schema, resolved, path.stem).module,
	                  encoding="ascii")

	_runtime()
	spec = importlib.util.spec_from_file_location(path.stem, module)
	assert spec is not None and spec.loader is not None
	built = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(built)
	return built


def test_the_framing_floor_is_never_above_what_an_empty_buffer_is_told(
		tmp_path: Path) -> None:
	"""`framing_floor` is a LOWER bound on every answer, over the corpus.

	The counted-run walk credits each element it has not reached with
	`framing_floor` of that element, and the whole of 26.724 is that the
	figure has to be no larger than the smallest answer the element's own
	`required` can give -- or the total FALLS when the element ahead
	completes and is re-counted at its own honest bound. `SIZE_MIN` was
	larger for an element whose length carries a `[min]`, which is how a
	conformant schema came to break monotonicity.

	The smallest answer is the one for zero bytes, because an element's
	own answer is monotone in what has arrived, so that is the one input
	this asks about. It asks it of every struct in the corpus rather than
	of the elements of the one corpus run, because 95 structs is a
	population and `ble`'s `adv_report` is not -- and `adv_report` is the
	ONLY counted run of variable-size records in the corpus, where
	`SIZE_MIN` and the floor are both ten and the fault cannot appear.

	A struct that reports a complete message for no bytes at all is
	skipped: `id3`'s and `mqtt`'s `nothing` are the two, and every backend
	refuses such a struct as a run element outright, which is why the
	comparison does not reach them.
	"""
	asked = exact = above = 0
	for path in SCHEMAS:
		module = _corpus_module(path, tmp_path)
		schema = parse_text(path.read_text(encoding="ascii"))
		for name, struct in resolve(schema, solve(schema)).structs.items():
			view = getattr(module, name, None)
			if view is None or not hasattr(view, "required"):
				continue
			try:
				view.required(b"")
				continue		# complete at nothing; never an element
			except module.TruncatedError as short:
				told = int(short.needed)
			except Exception:			# noqa: BLE001
				continue	# refuses rather than frames; not this claim
			floor = framing_floor(
				resolve(schema, solve(schema)).structs, struct)
			asked += 1
			exact += int(told == floor)
			above += int(told > floor)
			assert floor <= told, (
				f"{path.name}:{name}: the floor is {floor} and its own "
				f"`required` answers {told} for no bytes, so a run of "
				f"these would be credited more than the element will "
				f"ever report and the total can fall")

	# Not vacuous, and the figures are the ones measured when written:
	# 95 structs answered a bound, the floor was EXACT for 93 of them, and
	# `dnsname.question` (five against four) and `mqtt.will_message` (two
	# against one) were the two it was strictly below.
	#
	# `exact` is the guard that matters. A floor that collapsed to zero or
	# to one everywhere would satisfy the assertion above it trivially --
	# a lower bound of zero is sound and useless -- and would send this
	# count to about ten, which is how many structs open with a varint.
	assert asked >= 90, f"only {asked} structs answered a bound"
	assert exact >= 85, (
		f"the floor is exact for only {exact} of {asked}, where 93 of 95 "
		f"were measured; a floor that has gone slack is sound and buys "
		f"nothing, and this is the only thing that notices")
	assert above >= 1, "the floor is exact everywhere, where two were above"


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
