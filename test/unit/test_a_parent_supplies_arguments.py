"""A parent hands a child its arguments, by name, at the member (0068).

Step 1 of the record's build: the grammar and the rules that need no
layout. Nothing here is generated yet -- the nesting refusal still stands
for every shape -- so what these hold is that a schema reaches that refusal
only once its arguments are well formed, and that each malformed one is
refused naming what is wrong.
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


def test_well_formed_arguments_reach_the_nesting_refusal() -> None:
	"""Every source rule 5 allows, and a meaning-only parameter fed from a
	per-message one -- which rule 5 does not reach."""
	for source in (
			ENTRY + "struct vol { u16 block; entry(block = block) e; }\n",
			ENTRY + "const BLK = 512;\nstruct vol { entry(block = BLK) e; }\n",
			ENTRY + "struct vol { parameter u16 b [stream]; "
			        "entry(block = b) e; }\n",
			MEANS + "struct vol { parameter u8 m; note(mode = m) e; }\n"):
		assert "cannot be a member yet" in _refused(PREAMBLE + source)
