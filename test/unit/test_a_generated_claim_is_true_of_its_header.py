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

import pytest

from every_schema import SCHEMAS, ids
from situc.codegen.c import generate as generate_c
from situc.diagnostics import Source
from situc.layout import solve
from situc.parser import parse
from situc.resolve import resolve

#: Claim shapes worth checking, because each is falsifiable by reading the
#: same file. Prose that merely explains is not a claim: what makes these
#: checkable is that they quantify over the header they sit in.
EXCLUSIVE = re.compile(
	r"(the only (?:thing|one|way)[^.]{0,120}\.|"
	r"nothing (?:else|but|other than)[^.]{0,120}\.|"
	r"no other[^.]{0,120}\.)", re.I)

#: The one claim this file is named for, kept as a literal so the test fails
#: if it comes back by any route rather than only through the emitter line
#: that wrote it.
RETIRED = "no other scalar getter here can"


def _comments(text: str) -> list[str]:
	return [" ".join(one.replace("*", " ").split())
	        for one in re.findall(r"/\*\*?(.*?)\*/", text, re.S)]


def _header(path: Path) -> str:
	source   = Source(str(path), path.read_text(encoding="utf-8"))
	schema   = parse(source)
	resolved = resolve(schema, solve(schema))
	return generate_c(schema, resolved, path.stem).header


@pytest.mark.parametrize("path", SCHEMAS, ids=ids(SCHEMAS))
def test_no_header_claims_a_getter_is_the_only_fallible_one(
		path: Path) -> None:
	"""The retired claim, which was false five times in one file."""
	assert RETIRED not in _header(path), (
		f"{path.name}: a getter claims no other scalar getter can fail, in a "
		"header that may have a text number, a `[since]` member and a varint")


@pytest.mark.parametrize("path", SCHEMAS, ids=ids(SCHEMAS))
def test_an_exclusivity_claim_is_not_made_twice_in_one_header(
		path: Path) -> None:
	"""Whatever else a header asserts uniquely, it asserts once.

	This is the general form and the reason this file is not a single
	assertion: a claim of the shape *the only thing that* is falsified by
	appearing twice, whatever it is about, and no reading of the subject is
	needed to see it. A claim that recurs is either about different things --
	in which case it names them and the texts differ -- or it is the same
	claim made of several, which cannot be true of more than one.
	"""
	seen: dict[str, int] = {}
	for comment in _comments(_header(path)):
		for claim in EXCLUSIVE.findall(comment):
			seen[claim] = seen.get(claim, 0) + 1

	repeated = {claim: count for claim, count in seen.items() if count > 1}
	assert not repeated, (
		f"{path.name}: an exclusivity claim appears more than once, so it is "
		f"false of all but one of them: {repeated}")


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
