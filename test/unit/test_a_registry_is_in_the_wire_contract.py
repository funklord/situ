"""A registry's keyspace is part of the wire contract (26.1106).

Which keys exist, what each cell is a message of, its bounds and default
and which way it is safe to move: a contract between peers as a struct's
layout is. None of it was in the signature, so `wire --check` passed a key
removed or a bound narrowed.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pytest							# noqa: E402

from situc.diagnostics import Source				# noqa: E402
from situc.layout import solve					# noqa: E402
from situc.parser import parse					# noqa: E402
from situc.resolve import resolve				# noqa: E402
from situc.wire import compare, render				# noqa: E402

SCHEMA = """target buffer;
endian big;
struct count_cell { decimal u32 value[remaining] [minimal]; }
struct word_cell  { u8 word[remaining]; }
registry settings {
	"journal/window" : count_cell [min = 1, max = 36500, default = "60",
	                               unit = "days"] "how far back it reaches";
	"name/"          : word_cell;
}
"""


def _signature(text: str) -> str:
	schema = parse(Source("r.situ", text))
	return render(schema, resolve(schema, solve(schema)), "r.situ")


def test_the_keyspace_is_rendered() -> None:
	said = _signature(SCHEMA)
	assert "registry settings" in said
	assert ('key="journal/window" cell=count_cell default="60" max=36500 '
	        'min=1') in said
	assert 'key="name/" cell=word_cell family' in said
	assert "days" not in said and "reaches" not in said


@pytest.mark.parametrize("change, kind", [
	(('"name/"          : word_cell;', ""), "breaking"),
	(('"name/"          : word_cell;',
	  '"name/" : word_cell; "extra" : word_cell;'), "forward"),
	(('"name/"          : word_cell;', '"name/" : count_cell;'), "breaking"),
	(("max = 36500", "max = 3650"), "backward"),
])
def test_a_keyspace_change_is_classified(change: tuple[str, str],
		kind: str) -> None:
	verdict = compare(_signature(SCHEMA),
	                  _signature(SCHEMA.replace(*change)))
	kinds = {one.kind for one in verdict.findings
	         if one.subject.startswith("registry ")}
	assert kind in kinds, verdict.findings


def test_a_label_change_is_not_a_contract_change() -> None:
	verdict = compare(_signature(SCHEMA), _signature(
		SCHEMA.replace('"days"', '"d"').replace("reaches", "goes")))
	assert not [one for one in verdict.findings
	            if one.subject.startswith("registry ")], verdict.findings
