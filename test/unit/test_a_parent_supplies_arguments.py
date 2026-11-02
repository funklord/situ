"""A parent hands a child its arguments, by name, at the member (0068).

Step 1 of the record's build: the grammar and the rules that need no
layout. What these hold is that a schema's arguments are well formed
before anything else looks at them, and that each malformed one is refused
naming what is wrong.

The nesting refusal no longer stands for every shape -- steps 2, 4, 5 and
6 are built -- so the acceptance test below tracks which shapes generate
and which are still waiting. It is the file that goes stale when a step
lands, and twice it did (26.815).
"""

from __future__ import annotations

import pytest

from situc.diagnostics import SituError, Source
from situc.parser import parse_decls, parse_text
from situc.unparse import unparse

PREAMBLE = "target buffer;\nendian big;\nbit_order msb_first;\n"

ENTRY = "struct entry { parameter u16 block [stream]; u8 tag; u8 data[block]; }\n"
MEANS = "struct note { parameter u8 mode; u8 v [max = mode]; }\n"


def _refused(source: str) -> str:
	with pytest.raises(SituError) as refused:
		parse_text(source)
	return str(refused.value)


def test_the_arguments_round_trip() -> None:
	"""`Type(name = expr)` is printed as it was written."""
	source = (PREAMBLE + ENTRY
	          + "struct vol { u16 block; entry(block = block * 2) e; }\n")
	printed = unparse(parse_decls(Source("<t>", source)))
	assert "entry(block = block * 2) e;" in printed


@pytest.mark.parametrize(("member", "words"), [
	("u8(block = 1) e;", "takes no arguments"),
	("entry(size = 1) e;", "has no parameter `size`"),
	("entry(block = 1, block = 2) e;", "given twice"),
	("entry(block = later) e; u16 later;", "`later` is not in scope here"),
	("entry(block = size(n)) e;", "not something an argument may call"),
	("entry(block = mine) e;", "per-message argument"),
])
def test_a_malformed_argument_is_refused(member: str, words: str) -> None:
	assert words in _refused(PREAMBLE + ENTRY + "struct vol { parameter u16 "
	                         "mine; u16 n; " + member + " }\n")


def test_a_missing_argument_is_refused() -> None:
	two = "struct two { parameter u8 a; parameter u8 b; u8 x; }\n"
	assert "needs `b` as well" in _refused(
		PREAMBLE + two + "struct vol { two(a = 1) t; }\n")


def test_well_formed_arguments_are_accepted_or_wait_for_their_step() -> None:
	"""Every source rule 5 allows, and the shapes still waiting.

	**This asserted two refusals the document does not ask for.** Rule 5's
	last sentence is "a child parameter without `[stream]` (meaning only)
	may take any of them", so `note(mode = m)` is allowed and the previous
	docstring's "which rule 5 does not reach" read it backwards. The other
	was a real refusal that stopped being one when step 5a landed in
	`c43b57f`: this file was not re-run, because `-x` had stopped the suite
	in an earlier test, so master carried a red assertion about a refusal
	the code had deliberately lifted.

	Rule 5's refusal is not lost by narrowing this. A per-message parent
	parameter feeding a byte-moving child is one of
	`test_a_malformed_argument_is_refused`'s cases, which is where a
	refusal belongs: a test named for acceptance that also carries
	refusals is a test nobody re-reads when the acceptance set grows, and
	that is exactly how this one went stale.
	"""
	for source in (
			# Step 2: a parent field, and a constant.
			ENTRY + "struct vol { u16 block; entry(block = block) e; }\n",
			ENTRY + "const BLK = 512;\nstruct vol { entry(block = BLK) e; }\n",
			# Step 5: the parent's own `[stream]` parameter handed on to a
			# single nested member (26.812).
			ENTRY + "struct vol { parameter u16 b [stream]; "
			        "entry(block = b) e; }\n",
			# Rule 5, last sentence: a meaning-only child parameter may take
			# a per-message one, because it moves no bytes.
			MEANS + "struct vol { parameter u8 m; note(mode = m) e; }\n"):
		parse_text(PREAMBLE + source)
	for source in (
			# A run of them is step 5's other half.
			ENTRY + "struct vol { parameter u16 b [stream]; u8 n; "
			        "entry(block = b) e[n]; }\n",
			# Rule 6: a struct that names itself, or reaches itself through
			# any chain, is measured with a depth, and handing arguments
			# down that path is the rest of step 5.
			"struct self { parameter u8 b [stream]; u8 n; "
			"self(b = b) kid; }\n"):
		assert "cannot be a member yet" in _refused(PREAMBLE + source)
