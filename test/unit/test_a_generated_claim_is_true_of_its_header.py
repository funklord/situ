"""An exclusivity claim in generated output has to hold in the file it is in.

26.564 was found by reading a generated doc comment and noticing the emitter
broke the rule it stated. Swept for the mechanically checkable form of that --
a claim of the shape *the only thing that*, *nothing else*, *no other* -- the
corpus's C headers carry six distinct ones. Five are the gate's, and they hold:
each `situ_<region>_t` is produced by its own `_open` and by nothing else, in
all five schemas that have a sealed region.

The sixth did not. A text number's getter said *this one takes an
out-parameter because the conversion can fail, which no other scalar getter
here can*, and `edges.h` carries that sentence **five times** -- five getters
each claiming to be the only one. The same header has 23 scalar getters that
take an out-parameter and return an error, a `[since]` member's getter and a
varint's among them, because absence and overlong are failures too.

The sentence now contrasts with a fixed-width read, which is what it was
reaching for and is true however many fallible getters the header has.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol

import pytest

from every_schema import SCHEMAS, ids
from situc import ast
from situc.codegen.c import generate as generate_c
from situc.codegen.cpp import generate as generate_cpp
from situc.codegen.python import generate as generate_python
from situc.codegen.rust import generate as generate_rust
from situc.diagnostics import Source
from situc.layout import solve
from situc.parser import parse
from situc.resolve import ResolvedSchema, resolve

#: Claim shapes worth checking, because each is falsifiable by reading the
#: same file. Prose that merely explains is not a claim: what makes these
#: checkable is that they quantify over the header they sit in.
EXCLUSIVE = re.compile(
	r"(the only (?:thing|one|way)[^.]{0,120}\.|"
	r"nothing (?:else|but|other than)[^.]{0,120}\.|"
	r"no other[^.]{0,120}\.)", re.I)

#: Claims retired because they were false, kept as literals so each fails if
#: it comes back by any route rather than only through the emitter line that
#: wrote it.
#:
#: **Both are needed and the repetition test does not replace them.** The
#: Rust claim appeared 40 times across 6 schemas -- 22 in `edges.rs`, 8 in
#: `http.rs`, and exactly ONE in `slip.rs`. A claim made once is not repeated,
#: so the general test is blind to it there, and `slip` is the case that says
#: a retired wording wants naming rather than only counting (26.566).
RETIRED = (
	"no other scalar getter here can",
	"the only thing parse can catch here",
)


class Emitted(Protocol):
	def files(self) -> dict[str, str]: ...


class Backend(Protocol):
	"""What the four generators have in common for this file's purpose.

	Named rather than widened to `Any`, because a dict over the four
	collapses the callable to `object` and `.files()` is then unreachable
	through it -- which mypy says and three sibling test files already
	answer this way.
	"""

	def __call__(self, schema: ast.Schema, resolved: ResolvedSchema,
	             basename: str) -> Emitted: ...


#: One entry per backend: how to generate it, and how its comments are
#: spelled. All four rather than C alone, which is the gap 26.566 went
#: through: Rust emitted *which is the only thing parse can catch here* into
#: 40 schemas, eight times in `http.rs` alone, and this file could not see it
#: because it read C headers.
BACKENDS: dict[str, tuple[Backend, str]] = {
	"c":      (generate_c,      r"/\*\*?(.*?)\*/"),
	"cpp":    (generate_cpp,    r"/\*\*?(.*?)\*/"),
	"python": (generate_python, r'"""(.*?)"""'),
	# Rust has no block comment for docs: a run of `///` lines is one.
	"rust":   (generate_rust,   r"((?:^[ \t]*///.*\n)+)"),
}


def _comments(text: str, pattern: str) -> list[str]:
	return [" ".join(one.replace("*", " ").replace("///", " ").split())
	        for one in re.findall(pattern, text, re.S | re.M)]


def _emitted(path: Path, backend: str) -> tuple[str, str]:
	"""Everything the backend writes, and the comment shape to read it with.

	Every file rather than the header, because Rust and Python emit one
	module and C++ one header -- asking for `.header` would have read nothing
	for two of the four.
	"""
	generate, pattern = BACKENDS[backend]
	source   = Source(str(path), path.read_text(encoding="utf-8"))
	schema   = parse(source)
	resolved = resolve(schema, solve(schema))
	built    = generate(schema, resolved, path.stem)
	return "".join(one for _, one in sorted(built.files().items())), pattern


def _header(path: Path) -> str:
	return _emitted(path, "c")[0]


@pytest.mark.parametrize("backend", sorted(BACKENDS))
@pytest.mark.parametrize("path", SCHEMAS, ids=ids(SCHEMAS))
def test_no_retired_claim_comes_back(path: Path, backend: str) -> None:
	"""Each was false, and each is false by a different route.

	One said no other scalar getter in the header could fail, in headers with
	up to 23 that can. The other said a missing delimiter is the only thing
	parse can catch about a delimited member, in modules that also check its
	`[encoding]`, its token set and the cap on its scan.
	"""
	emitted, _ = _emitted(path, backend)
	for claim in RETIRED:
		assert claim not in emitted, (
			f"{path.name} ({backend}): the retired claim `{claim}` is back")


@pytest.mark.parametrize("backend", sorted(BACKENDS))
@pytest.mark.parametrize("path", SCHEMAS, ids=ids(SCHEMAS))
def test_an_exclusivity_claim_is_not_made_twice_in_one_header(
		path: Path, backend: str) -> None:
	"""Whatever else a header asserts uniquely, it asserts once.

	This is the general form and the reason this file is not a single
	assertion: a claim of the shape *the only thing that* is falsified by
	appearing twice, whatever it is about, and no reading of the subject is
	needed to see it. A claim that recurs is either about different things --
	in which case it names them and the texts differ -- or it is the same
	claim made of several, which cannot be true of more than one.
	"""
	emitted, pattern = _emitted(path, backend)
	seen: dict[str, int] = {}
	for comment in _comments(emitted, pattern):
		for claim in EXCLUSIVE.findall(comment):
			seen[claim] = seen.get(claim, 0) + 1

	repeated = {claim: count for claim, count in seen.items() if count > 1}
	assert not repeated, (
		f"{path.name} ({backend}): an exclusivity claim appears more than "
		f"once, so it is false of all but one of them: {repeated}")


@pytest.mark.parametrize("path", SCHEMAS, ids=ids(SCHEMAS))
def test_a_gate_is_produced_by_its_own_open_and_nothing_else(
		path: Path) -> None:
	"""The five claims that DO hold, held to.

	A `sealed` region's gate type says in its own doc comment that the only
	thing producing one is `<region>_open()`, and 26.564 is what happens when
	something else reads the bytes it guards. This checks the claim as
	written: every function that returns the gate type or takes an
	out-pointer to it is that region's `open`.

	Schemas with no sealed region pass by having nothing to check, which is
	why the claim-count assertion above is the one that cannot go vacuous.
	"""
	header = _header(path)
	gates  = [one for one in re.findall(r"\}\s*(situ_\w+_t)\s*;", header)
	          if f"{one[:-2]}_open" in header]

	for gate in gates:
		producers = set(re.findall(
			rf"^(?:static\s+inline\s+)?\w[\w \t*]*?\b(situ_\w+)\s*\("
			rf"[^;]*?{re.escape(gate)}\s*\*", header, re.M))
		producers |= set(re.findall(
			rf"^(?:static\s+inline\s+)?{re.escape(gate)}\s+(situ_\w+)\s*\(",
			header, re.M))
		assert producers == {f"{gate[:-2]}_open"}, (
			f"{path.name}: `{gate}` is produced by {sorted(producers)}, and "
			f"its own doc comment says only `{gate[:-2]}_open` can")
