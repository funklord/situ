"""`required` asked about every short prefix of every struct in the corpus.

**The framing layer had no corpus-wide execution in any backend**, which is
what this file is for. Measured 2026-10-29: the differential driver
`situc/codegen/differ.py` -- the thing that compares all five spellings over
every schema, and the same driver the C++ fuzz harness is built from -- does
not mention `required` or `framed` anywhere. So the only things that ever ran
this function were `test_required_answers_every_prefix.py`'s seven
hand-written shapes and one sweep per backend over `example/http`.

That function has seven decision records against it in two weeks -- 26.683
through 26.686, 26.693, 26.723 and 26.724 -- and two of those arrived from a
consumer's fuzzer rather than from here.

**THE BUFFER IS THE INSTRUMENT.** Each prefix gets a `malloc` of exactly its
own length, so a read past it is a read past the allocation and the sanitizer
says so. The sweep that existed walked every prefix of a real request out of
a `const char req[]` on the stack: an over-read there lands inside some other
object, answers from whatever is in it, and reports nothing.

That is not a hypothetical about what it missed. 26.723 was reported from
bbq-predictor, and `example/ble`'s own `le_advertising_report` reproduces it
-- `reports_count()` reads offset 3 of a one-byte buffer, a
heap-buffer-overflow under ASan at `97d1343` and clean after it. **The corpus
had the shape all along.** Nothing was missing from the population; what was
missing was ever handing this function a buffer whose end was real.

The two properties asked here are the ones a synthetic buffer can carry
honestly:

- **progress** -- a truncated answer must exceed what arrived, or a caller
  asking "how many more" is told zero and never advances;
- **monotonicity** -- `need` is a lower bound on a total that does not move,
  so an answer that shrinks is a quantity being read that shrinks. This is
  26.724, and it is the property bbq-predictor's fuzzer holds.

Not asked: that no prefix is reported complete before the message ends. These
buffers are fills rather than messages, so there is no "ends" to compare
against -- `test_required_answers_every_prefix.py` owns that property, over
shapes whose whole message is written out.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pytest						# noqa: E402

from situc.codegen.c import generate as generate_c	# noqa: E402
from situc.codegen.cpp import generate as generate_cpp	# noqa: E402
from situc.diagnostics import Source			# noqa: E402
from situc.layout import solve				# noqa: E402
from situc.parser import parse				# noqa: E402
from situc.resolve import resolve			# noqa: E402

from every_schema import SCHEMAS as ALL_SCHEMAS		# noqa: E402

RUNTIME  = ROOT / "runtime"
HOST_CXX = shutil.which("g++") or shutil.which("clang++")
HOST_CC  = shutil.which("cc") or shutil.which("gcc")

#: EVERY schema this repository builds, not `example/` only. That list is
#: `every_schema.py`'s whole reason for existing, and its docstring names
#: this exact mistake: six files wanted the list and four had the shorter
#: one, so the file most likely to break a backend --
#: `test/schema/edges.situ`, which exists to carry the constructs the
#: worked examples happen not to have -- was the file the checks skipped.
#: This sweep was written with the shorter one and corrected before it was
#: committed twice, which is once more than the guard deserves.
SCHEMAS  = list(ALL_SCHEMAS)

#: TWO BACKENDS, and each is here for a reason the other cannot serve.
#:
#: C++ is where a missing bound is a MEMORY fault rather than a wrong
#: number: its accessors are the only ones that are not lax, a static
#: offset reading straight through `situ_get_be16` on the invariant that a
#: view has passed `view()`'s `length >= size_min` guard, and `required`
#: is the one caller that builds a view without it. That is why 26.723 was
#: C++ only and why a sanitizer has anything to find.
#:
#: C is where the WIDEST `required` lives. 26.728 measured all four frame
#: layers and C is the only one clean on every schema: it has a pad branch
#: in its extent chain the other three lack entirely, so five structs are
#: framed by C alone. Sweeping C++ therefore leaves C's widest arithmetic
#: -- including the only `align_up` term in any `required` -- unexecuted
#: by anything, over 42 schemas where C++ manages 40.
#:
#: Rust and Python are left out deliberately rather than forgotten: their
#: readers answer zero out of bounds, so a missing bound is a wrong number
#: there and `test_required_answers_every_prefix.py` holds the arithmetic
#: in Python over shapes whose whole message is written out.
WARNINGS = ["-std=c++17", "-pedantic-errors", "-O1", "-Wall", "-Wextra",
	"-Wconversion", "-Wsign-conversion", "-Werror", "-fno-exceptions",
	"-fno-rtti"]
C_WARNINGS = ["-std=c11", "-O1", "-Wall", "-Wextra"]
SANITIZE = ["-g", "-fsanitize=address,undefined",
	# LOAD-BEARING, and `test/generated/Makefile` says why at its own
	# fuzz flags: with recovery UBSan prints its report and the program
	# carries on and exits 0, so a gate that finds something passes.
	"-fno-sanitize-recover=all"]

#: How far to sweep. Every fixed prefix in the corpus but `sqlite`'s
#: hundred-byte file header sits inside this, and the short prefixes are
#: where a bound either exists or does not.
LONGEST = 48

#: The signature that takes bytes and nothing else, in the two lines the
#: emitter breaks it over. A struct with a `[stream]` argument takes it on
#: `required` too (0050), and calling one would mean inventing a value for
#: a fact the schema says is negotiated -- so those are left to the
#: backend's own tests rather than given a number here.
#:
#: **BOTH lines, because the argument lands on the second one.** Matching
#: the first alone let `edges.negotiated_signed` through, whose `required`
#: takes `(data, have, <arg>, need)`, and the compiler refused the
#: function pointer. That is the detector being wrong about its own
#: subject, which is the hazard this file's own `framed_classes` docstring
#: is about, met one line below it.
OPENS = "static ::situ::rt::err required(const std::uint8_t *data,"
CLOSES = "std::uint32_t have, std::uint32_t &need) noexcept"

#: schema -> (calls, truncated answers, monotonicity comparisons made),
#: filled in as the sweeps run. One per backend, so a companion can say
#: which arm stopped asking rather than that one of them did.
CORPUS_ASKED: dict[str, tuple[int, int, int]] = {}
C_ASKED: dict[str, tuple[int, int, int]] = {}


def ids(paths: list[Path]) -> list[str]:
	return [path.name for path in paths]


def framed_classes(header: str) -> list[str]:
	"""The classes in a generated header whose `required` takes only bytes.

	Read back out of the artifact rather than derived from the schema a
	second time. A predicate written here would be a second opinion about
	which structs are frameable, and the one that matters is the emitter's
	-- `evidence.md`'s rule that a check recomputing its subject has
	verified something other than what shipped.
	"""
	seen: list[str] = []
	current: str | None = None
	lines = header.splitlines()
	for index, line in enumerate(lines):
		found = re.match(r"class (\w+)\b", line)
		if found is not None:
			current = found.group(1)
		if OPENS not in line or current is None:
			continue
		tail = lines[index + 1] if index + 1 < len(lines) else ""
		if CLOSES in tail:
			seen.append(current)
	return seen


def _namespace(parsed: object) -> str:
	"""The C++ namespace the header puts these classes in.

	`situ` unless the schema declares one, which `namespaces.situ` is the
	test for and two corpus schemas use.
	"""
	declared = getattr(parsed, "namespace", None)
	return str(declared) if declared else "situ"


def probe(classes: list[str], namespace: str) -> str:
	"""A main() sweeping every prefix of every class, each in its own
	exact-size allocation."""
	calls = "\n".join(
		f'\t(void)sweep("{name}", &::{namespace}::{name}::required);'
		for name in classes)
	return f'''#include "unit.hpp"
#include <cstdio>
#include <cstdlib>
#include <cstring>

/* Four fills rather than one, and each is here for a different reach.
 * 0x00 reads every count as zero so every walk falls out at once; 0xFF
 * makes each length as large as the type allows, which is where a
 * saturating add earns its keep; 0x5A is neither.
 *
 * 0x01 IS THE ONE THAT TAKES A RUN APART, and it was added after the
 * other three were measured unable to. A uniform fill makes a `u16`
 * count either zero or enormous, so a run either does not start or never
 * finishes an element inside 48 bytes -- and the arithmetic worth
 * reaching is the step where an element AHEAD completes while the next
 * one reports its own short bound. 0x01 gives a count of 0x0101 and
 * elements of two bytes, so that step happens every other prefix.
 *
 * Measured on `edges.floor_run` against an export of `a176c43`, where
 * 26.724 is present: `need` falls from 1032 to 1029 at eight bytes
 * under this fill, and not at any prefix under the other three. */
static const std::uint8_t FILL[] = {{ 0x00u, 0xFFu, 0x5Au, 0x01u }};

static unsigned long asked;
static unsigned long told_short;
/* EVERY struct that broke, not the first. A probe that returns on the
 * first finding reports one fault per run of the gate, and the run after
 * the fix is the one that finds the second -- which is how a sweep used as
 * an instrument hides what it was built to show. Measured: `edges.situ`
 * holds two of these, and the first masked the second until this counted
 * instead of returning. */
static unsigned long broken;
/* How many monotonicity comparisons were actually MADE. The check skips
 * the first truncated answer of each run, having nothing to compare it
 * to, and stops at the first complete one -- so a struct that completes
 * early is barely checked, and nothing would say so. Measured: 18,091 of
 * the 18,959 truncated answers, which is 95 per cent, and the figure is
 * asserted below because a `done` that started being set early would
 * leave every other assertion here passing. */
static unsigned long compared;

typedef ::situ::rt::err (*ask_t)(const std::uint8_t *, std::uint32_t,
                                 std::uint32_t &);

static int sweep(const char *who, ask_t ask)
{{
	for (unsigned f = 0; f < sizeof FILL; f++) {{
		std::uint32_t prev = 0;
		bool          done = false;

		for (std::uint32_t n = 0; n <= {LONGEST}u; n++) {{
			const std::uint32_t size = n != 0u ? n : 1u;
			std::uint8_t *p = static_cast<std::uint8_t *>(
				std::malloc(size));
			std::uint32_t need = 0;

			if (p == nullptr) {{
				return 1;
			}}
			std::memset(p, FILL[f], size);

			const ::situ::rt::err got = ask(p, n, need);

			std::free(p);
			asked++;

			if (got == ::situ::rt::err::ok) {{
				done = true;
				continue;
			}}
			if (got != ::situ::rt::err::truncated) {{
				continue;	/* refused these bytes; not framing */
			}}
			told_short++;
			if (need <= n) {{
				std::printf("%s fill=%u have=%u: truncated and "
				            "need=%u, so a caller is told to read "
				            "%d more\\n", who, f, n, need,
				            (int)need - (int)n);
				broken++;
				break;
			}}
			/* Monotone only up to the first complete answer: past
			 * that these fills are no longer prefixes of one
			 * message, and the property is about one message. */
			if (!done && need < prev) {{
				std::printf("%s fill=%u have=%u: need FELL to %u "
				            "from %u\\n", who, f, n, need, prev);
				broken++;
				break;
			}}
			if (!done) {{
				if (prev != 0u) {{
					compared++;
				}}
				prev = need;
			}}
		}}
	}}
	return 0;
}}

int main()
{{
{calls}
	std::printf("asked=%lu truncated=%lu compared=%lu\\n",
	            asked, told_short, compared);
	return broken != 0ul ? 4 : 0;
}}
'''


@pytest.mark.skipif(HOST_CXX is None, reason="no host C++ compiler")
@pytest.mark.parametrize("schema", SCHEMAS, ids=ids(SCHEMAS))
def test_required_reads_only_bytes_that_arrived(schema: Path,
		tmp_path: Path) -> None:
	"""Every short prefix, in a buffer of exactly its own length."""
	source   = Source(str(schema), schema.read_text(encoding="ascii"))
	parsed   = parse(source)
	resolved = resolve(parsed, solve(parsed))
	built    = generate_cpp(parsed, resolved, "unit")

	(tmp_path / "unit.hpp").write_text(built.header, encoding="ascii")
	classes = framed_classes(built.header)
	if not classes:
		pytest.skip(f"{schema.name} emits no `required` taking only bytes")

	(tmp_path / "main.cpp").write_text(
		probe(classes, _namespace(parsed)), encoding="ascii")

	compiled = subprocess.run(
		[HOST_CXX or "g++", *WARNINGS, *SANITIZE,
		 f"-I{RUNTIME / 'c'}", f"-I{RUNTIME / 'cpp'}", f"-I{tmp_path}",
		 str(tmp_path / "main.cpp"), str(RUNTIME / "c" / "situ.c"),
		 "-o", str(tmp_path / "probe")],
		capture_output=True, text=True)
	if compiled.returncode != 0 and "sanitize" in compiled.stderr:
		pytest.skip("no address sanitizer")
	assert compiled.returncode == 0, compiled.stderr

	run = subprocess.run([str(tmp_path / "probe")],
	                     capture_output=True, text=True)
	assert run.returncode == 0, run.stdout + run.stderr

	counts = re.search(r"asked=(\d+) truncated=(\d+) compared=(\d+)",
	                   run.stdout)
	assert counts is not None, run.stdout
	CORPUS_ASKED[schema.name] = (int(counts.group(1)), int(counts.group(2)),
	                             int(counts.group(3)))

	# Per schema rather than once at the end, so a sweep that stopped
	# asking is named where it happened. A struct complete at zero bytes
	# is legitimate -- `id3` and `mqtt` each declare a `nothing` -- so the
	# floor is that SOMETHING was refused rather than that everything was.
	assert int(counts.group(2)) > 0, (
		f"{schema.name}: {counts.group(1)} calls and not one truncated "
		f"answer, so this schema's framing was never exercised")


#: C's signature, which is one line rather than two. A struct with a
#: `[stream]` argument takes it here too, so the pattern pins the whole
#: parameter list.
C_SIGNATURE = re.compile(
	r"static inline situ_err_t (\w+_required)\(const uint8_t \*data,\s*"
	r"uint32_t have, uint32_t \*need\)")


def c_probe(names: list[str]) -> str:
	"""The same sweep in C, over the `_required` functions a header has."""
	calls = "\n".join(f'\tsweep("{name}", &{name});' for name in names)
	return f'''#include "unit.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* The same four fills and the same two properties as the C++ probe above,
 * and the same exact-size allocation, which is the whole instrument. */
static const uint8_t FILL[] = {{ 0x00u, 0xFFu, 0x5Au, 0x01u }};

static unsigned long asked;
static unsigned long told_short;
static unsigned long compared;
static unsigned long broken;

typedef situ_err_t (*ask_t)(const uint8_t *, uint32_t, uint32_t *);

static void sweep(const char *who, ask_t ask)
{{
	for (unsigned f = 0; f < sizeof FILL; f++) {{
		uint32_t prev = 0u;
		int      done = 0;

		for (uint32_t n = 0; n <= {LONGEST}u; n++) {{
			const uint32_t size = n != 0u ? n : 1u;
			uint8_t       *p = malloc(size);
			uint32_t       need = 0;
			situ_err_t     got;

			if (p == NULL) {{
				exit(1);
			}}
			memset(p, FILL[f], size);
			got = ask(p, n, &need);
			free(p);
			asked++;

			if (got == SITU_OK) {{
				done = 1;
				continue;
			}}
			if (got != SITU_ERR_TRUNCATED) {{
				continue;
			}}
			told_short++;
			if (need <= n) {{
				printf("%s fill=%u have=%u: truncated and need=%u, "
				       "so a caller is told to read %d more\\n",
				       who, f, n, need, (int)need - (int)n);
				broken++;
				break;
			}}
			if (!done && need < prev) {{
				printf("%s fill=%u have=%u: need FELL to %u from %u\\n",
				       who, f, n, need, prev);
				broken++;
				break;
			}}
			if (!done) {{
				if (prev != 0u) {{
					compared++;
				}}
				prev = need;
			}}
		}}
	}}
}}

int main(void)
{{
{calls}
	printf("asked=%lu truncated=%lu compared=%lu\\n",
	       asked, told_short, compared);
	return broken != 0ul ? 4 : 0;
}}
'''


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
@pytest.mark.parametrize("schema", SCHEMAS, ids=ids(SCHEMAS))
def test_the_c_required_reads_only_bytes_that_arrived(schema: Path,
		tmp_path: Path) -> None:
	"""The same sweep against the backend with the widest `required`.

	C holds the only `align_up` term in any `required` -- 26.693's record
	measured that term rising one byte per round trip for `pickle_string`
	and nothing had executed it over the corpus since -- and after 26.729
	it frames two structs no other backend does for that reason.

	**This arm found 26.729 on its first run**, through the per-schema
	non-vacuity assertion below rather than through a property: `protobuf`
	answered 196 calls and not one truncated, because
	`situ_proto_message_required` returned `SITU_OK` with `*need = 0` for
	every input and the Reader built on it looped. A sweep that only
	checked the two properties would have passed it, twice over -- `need`
	never fell and was never below `have`, because it was never anything.
	"""
	source   = Source(str(schema), schema.read_text(encoding="ascii"))
	parsed   = parse(source)
	resolved = resolve(parsed, solve(parsed))
	built    = generate_c(parsed, resolved, "unit")

	(tmp_path / "unit.h").write_text(built.header, encoding="ascii")
	(tmp_path / "unit.c").write_text(built.source, encoding="ascii")
	names = sorted(set(C_SIGNATURE.findall(built.header)))
	if not names:
		pytest.skip(f"{schema.name} emits no `_required` taking only bytes")

	(tmp_path / "m.c").write_text(c_probe(names), encoding="ascii")
	compiled = subprocess.run(
		[HOST_CC or "cc", *C_WARNINGS, *SANITIZE,
		 f"-I{RUNTIME / 'c'}", f"-I{tmp_path}", str(tmp_path / "m.c"),
		 str(tmp_path / "unit.c"), str(RUNTIME / "c" / "situ.c"),
		 "-o", str(tmp_path / "probe")],
		capture_output=True, text=True)
	if compiled.returncode != 0 and "sanitize" in compiled.stderr:
		pytest.skip("no address sanitizer")
	assert compiled.returncode == 0, compiled.stderr

	run = subprocess.run([str(tmp_path / "probe")],
	                     capture_output=True, text=True)
	assert run.returncode == 0, run.stdout + run.stderr

	counts = re.search(r"asked=(\d+) truncated=(\d+) compared=(\d+)",
	                   run.stdout)
	assert counts is not None, run.stdout
	C_ASKED[schema.name] = (int(counts.group(1)), int(counts.group(2)),
	                        int(counts.group(3)))
	assert int(counts.group(2)) > 0, (
		f"{schema.name}: {counts.group(1)} calls and not one truncated "
		f"answer, so this schema's framing was never exercised")


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
def test_the_c_corpus_sweep_asked_something() -> None:
	"""The C arm's own non-vacuity companion.

	Measured after 26.729: 40 of the 46 schemas swept and 6 skipped for
	emitting no `_required` taking only bytes, giving 43,316 calls, 19,222
	truncated answers and 18,346 compared with the answer before them. It
	was 42 schemas and 43,904 calls before that fix, `coap` and
	`protobuf` having stopped emitting a `_required` at all.
	"""
	if not C_ASKED:
		pytest.skip("the per-schema sweeps did not run")
	calls = sum(asked for asked, _, _ in C_ASKED.values())
	short = sum(told for _, told, _ in C_ASKED.values())
	comp  = sum(made for _, _, made in C_ASKED.values())
	assert len(C_ASKED) >= 36, (
		f"only {len(C_ASKED)} schemas were swept, of {len(SCHEMAS)}")
	assert calls >= 35000, f"only {calls} calls to `_required`"
	assert short >= 15000, f"only {short} truncated answers of {calls}"
	assert comp >= 14000, (
		f"only {comp} of {short} truncated answers were compared with "
		f"the one before, where 18,346 of 19,222 were measured")


@pytest.mark.skipif(HOST_CXX is None, reason="no host C++ compiler")
def test_the_corpus_sweep_asked_something() -> None:
	"""The non-vacuity companion, since every assertion above is inside a
	program that might have been handed no classes to call.

	Measured when written: of the 46 schemas this repository builds, 40
	were swept and 6 skipped for emitting no `required` that takes only
	bytes, giving 42,924 calls over 49 prefix lengths and 4 fills, of
	which 18,959 answered truncated and 18,091 were compared with the
	answer before them. The rest are a complete message or
	a refusal, which a 48-byte fill of 0x00 or 0xFF very often is.

	The bounds are well under the figures, because a schema gaining a
	struct moves all three and what is worth catching is a collapse.
	"""
	if not CORPUS_ASKED:
		pytest.skip("the per-schema sweeps did not run")
	calls = sum(asked for asked, _, _ in CORPUS_ASKED.values())
	short = sum(told for _, told, _ in CORPUS_ASKED.values())
	comp  = sum(made for _, _, made in CORPUS_ASKED.values())
	assert len(CORPUS_ASKED) >= 36, (
		f"only {len(CORPUS_ASKED)} schemas were swept, of "
		f"{len(SCHEMAS)} in the corpus")
	assert calls >= 35000, f"only {calls} calls to `required`"
	assert short >= 15000, f"only {short} truncated answers of {calls}"
	# And the monotonicity check is the one that can go slack silently:
	# it needs a PAIR, so a struct that completes at its first prefix
	# contributes answers and no comparisons. 18,091 of 18,959 when
	# written.
	assert comp >= 14000, (
		f"only {comp} of {short} truncated answers were compared with "
		f"the one before, where 18,091 of 18,959 were measured; the "
		f"monotonicity half of this sweep has stopped asking")
