"""The editor can follow a cross-message relation (26.594).

`walker.report.relate` has existed since 26.95 and had **no caller outside
its own tests**: a built, tested predicate with no consumer. Decision 0034
names this tool as the one it was built for -- *follow a relation between
two messages* is one of the five things it lists as what a read-only
editor is worth shipping with -- and nothing in `editor/` mentioned it.

The same shape as an interface whose least-used method has no caller, one
level up: the predicate is right, the tests prove it, and no front end
asks.

WHAT THE PREDICATE WILL NOT SAY. `OK` or `ERR_CONSTRAINT` and no more,
because a walker that reported WHICH `must` failed would answer a question
the four compiled backends cannot. So the editor names the relation and
the two structs rather than inventing a reason.

AND THE ORDER IS TEMPORAL, which the corpus cannot demonstrate: dns's
`reply_to` is `reply.id == query.id` and `reply.opcode == query.opcode`,
two equalities, so it is symmetric by construction and holds for a pair
either way round. The asymmetric relation below is written for the
purpose, so that passing the pair in the wrong order is a case with an
answer rather than a case nobody can tell apart.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

import pytest					# noqa: E402
from editor.document import open_document		# noqa: E402
from every_schema import load_schema		# noqa: E402
from situc import pack as packer			# noqa: E402
from situc.diagnostics import Source		# noqa: E402
from situc.layout import solve			# noqa: E402
from situc.parser import parse			# noqa: E402
from situc.resolve import resolve			# noqa: E402
from walker.walk import Refused			# noqa: E402

#: A DNS header: id, flags, then four counts. 12 bytes.
QUERY = bytes.fromhex("1234" "0100" "0001000000000000")
REPLY = bytes.fromhex("1234" "8180" "0001000100000000")
OTHER = bytes.fromhex("9999" "8180" "0001000100000000")

#: An asymmetric relation, because dns's is not. `later.seq > earlier.seq`
#: holds one way round and not the other, which is what makes the temporal
#: order observable at all.
TICKS = """
endian big;

struct tick {
	u16  seq;
	u16  pad;
}

relation follows(earlier: tick, later: tick) {
	must later.seq > earlier.seq;
}
"""


def _image(text: str) -> bytes:
	parsed   = parse(Source("t.situ", text))
	resolved = resolve(parsed, solve(parsed))
	return packer.pack(parsed, resolved, metadata=True)[0]


def _dns() -> bytes:
	parsed   = load_schema(ROOT / "example" / "dns" / "dns.situ")
	resolved = resolve(parsed, solve(parsed))
	return packer.pack(parsed, resolved, metadata=True)[0]


def test_a_matching_pair_holds_and_a_mismatched_one_does_not() -> None:
	"""dns's `reply_to`, which is the corpus's only relation.

	Both directions of the predicate, because a check that only ever saw
	the true case would pass against a `relate` that returned OK always.
	"""
	image = _dns()
	query = open_document(image, QUERY, "dns_header")

	held, why = query.relate("reply_to",
	                         open_document(image, REPLY, "dns_header"))
	assert held, why
	assert "reply_to" in why

	held, why = query.relate("reply_to",
	                         open_document(image, OTHER, "dns_header"))
	assert not held, why


def test_the_order_is_temporal() -> None:
	"""The pair in the wrong order is a different claim.

	Written against an asymmetric relation on purpose: dns's two `must`s
	are equalities, so its relation holds either way round and could not
	tell a correct implementation from one that sorted its arguments.
	"""
	image   = _image(TICKS)
	earlier = open_document(image, bytes.fromhex("0001" "0000"), "tick")
	later   = open_document(image, bytes.fromhex("0009" "0000"), "tick")

	held, _ = earlier.relate("follows", later)
	assert held, "4 then 9 does not follow"

	held, _ = later.relate("follows", earlier)
	assert not held, (
		"9 then 1 follows, so the two views are reaching the predicate in "
		"the wrong order or in no order at all")


def test_an_unknown_relation_names_the_ones_there_are() -> None:
	"""A reader who misremembers the name gets the list, not a bare no."""
	image = _dns()
	query = open_document(image, QUERY, "dns_header")

	with pytest.raises(Refused, match="reply_to"):
		query.relate("nosuch", open_document(image, REPLY, "dns_header"))


def test_a_relation_refuses_the_wrong_struct() -> None:
	"""`reply_to` takes two `dns_header`s and says so of anything else.

	THE SECOND DOCUMENT COMES FROM ANOTHER IMAGE, which is not incidental:
	dns declares one struct, so the only way to hold a document that is not
	a `dns_header` is to open one against a different schema. That is also
	the case that caught the first version of this guard -- it compared
	struct INDICES, and `tick` is index 0 in its image exactly as
	`dns_header` is in dns's, so a message from another schema passed
	straight through and would have been handed a verdict about neither.

	Comparing names is what both cases turn on. Identity on the image was
	tried in between and was worse: every `open_document` loads its own, so
	two documents over the same bytes are different objects and the check
	refused the ordinary case.
	"""
	query = open_document(_dns(), QUERY, "dns_header")
	tick  = open_document(_image(TICKS), bytes.fromhex("0001" "0000"), "tick")

	with pytest.raises(Refused, match="second") as caught:
		query.relate("reply_to", tick)
	assert "dns_header" in str(caught.value), str(caught.value)
	assert "tick" in str(caught.value), (
		"the refusal does not name what it was given, so a reader fixing "
		"it cannot see which side is wrong")


def test_a_document_lists_the_relations_it_could_be_half_of() -> None:
	"""One message cannot answer a predicate over two, so it says which
	pairings it is eligible for -- which is what an editor showing one
	message can honestly offer."""
	image = _dns()
	query = open_document(image, QUERY, "dns_header")

	found = query.relations()
	assert found, "dns_header is both halves of `reply_to` and lists neither"
	assert all(name == "reply_to" for name, _, _ in found), found
	assert {role for _, role, _ in found} == {"request", "response"}, (
		f"dns_header is the request AND the response of `reply_to`, so both "
		f"roles should be listed: {found}")
