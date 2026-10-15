"""Every `Member` kind the differential asks about, or a named reason.

`situc/codegen/differ.py` ends its docstring with *"what is probed is a
subset, and the subset is the thing to grow"*, and it lists what the
subset has cost each time it was short: a wide-scalar run whose count
came from the message (26.47), HTTP's status code, cpio's thirteen hex
fields, SMTP's reply code, and -- most recently -- a run of structs
asked nothing at all, which is what let a count of zero live in C
(26.638, 26.639).

Three of those were found by a defect and one by a lens. This asks the
question with neither: `traverse.classify` decides what kind a member
is, and every kind either reaches a probe or is named below with the
reason it does not. A hole then fails this test rather than waiting for
somebody to read a long `elif` chain.

The exemptions hold in BOTH directions, which is what keeps them from
outliving their reason: a kind named here that starts being probed
fails too, so the entry has to go when the gap closes.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

from every_schema import SCHEMAS, load_schema		# noqa: E402

from situc import traverse				# noqa: E402
from situc.codegen.c.names import c_name			# noqa: E402
from situc.codegen import differ				# noqa: E402
from situc.layout import solve				# noqa: E402
from situc.resolve import resolve			# noqa: E402
from situc.traverse import Member				# noqa: E402

#: A kind the drivers do not ask about, and why. Each of these is a
#: statement about the four BACKENDS rather than about this file: the
#: question is either not answerable through an accessor at all, or not
#: answerable through the same one in four languages.
EXEMPT: dict[Member, str] = {
	Member.RESERVED:
		"reserved bytes have no accessor in any backend -- their "
		"content is a policy, not a value -- and the policy is what "
		"`validate` checks, which every driver already asks.",
	# `LOCATED` was exempt here and is not any more: `Probe.LOCATED`
	# asks it, shaped like `ARM_BYTES`' because the accessor is fallible
	# in three of the four, and each driver reaches it its own way --
	# C fills a `situ_view_t` and takes `&msg`, C++ a
	# `::situ::rt::bytes` and takes `msg.raw()`, Rust returns
	# `Result<&[u8]>` over the raw slice, Python is a `memoryview`
	# property that raises. The exemption said "there is no one probe to
	# spell", which was true and was not a reason to ask nothing
	# (26.652).
}


def _coverage() -> tuple[dict[Member, int], dict[Member, int]]:
	"""How many members of each kind the corpus has, and how many of
	them the drivers ask something about.

	Asked by NAME, because an `Ask` carries the member's local name and
	a probe for `held.run` is the question about `run`.

	**Through `c_name`, because that is what `asks` stores.** Comparing
	a raw `local_name` against it passed the first sabotage and failed
	the second: the two spellings are identical for an ordinary
	identifier and differ for exactly the kind this file exempts --
	`<reserved0>` against `reserved0` -- so the detector could not see a
	probe for the one member it was most important to see. A count
	inherits its detector, in a test written to catch that very shape.
	"""
	seen: dict[Member, int] = {}
	asked: dict[Member, int] = {}
	for path in SCHEMAS:
		parsed   = load_schema(path)
		resolved = resolve(parsed, solve(parsed))
		names    = set(resolved.structs)
		for struct in resolved.structs.values():
			probes = {ask.local for ask in
			          differ.asks(struct, names, resolved.structs)}
			for held in traverse.own_members(struct):
				kind  = traverse.classify(struct, held, names)
				local = c_name(traverse.local_name(struct, held))
				seen[kind] = seen.get(kind, 0) + 1
				if local in probes:
					asked[kind] = asked.get(kind, 0) + 1
	return seen, asked


def test_every_kind_in_the_corpus_is_probed_or_exempt() -> None:
	"""The partition, asserted rather than read off a chain.

	A kind with members and no probe is the hole this file exists for.
	It is the shape `test_a_refusal_says_what_the_member_is.py` uses for
	the builder's refusals, and for the same reason: the cell that
	matters is the empty one, and nothing else reports it.
	"""
	seen, asked = _coverage()
	unasked = {kind for kind, count in seen.items()
	           if count and not asked.get(kind)}

	assert unasked <= set(EXEMPT), (
		"these member kinds have members in the corpus and no probe "
		f"asks anything about them: {sorted(str(k) for k in unasked - set(EXEMPT))}")


def test_no_exemption_outlives_its_reason() -> None:
	"""A kind named exempt that IS probed has to lose its entry.

	The two-directional half. An exemption is a claim about what four
	backends cannot be asked in one voice, and the day one of them can
	be, the claim is false -- `test_composed_schemas.py`'s `KNOWN` list
	holds its entries to the same rule and says so in as many words.
	"""
	seen, asked = _coverage()
	for kind, why in EXEMPT.items():
		assert why.strip(), f"{kind} is exempt with no reason given"
		if not seen.get(kind):
			continue
		assert not asked.get(kind), (
			f"{kind} is named exempt and the drivers now ask about "
			f"{asked[kind]} of its {seen[kind]} members -- remove the "
			"entry rather than leaving a reason that is no longer true")


def test_a_kind_with_no_corpus_population_is_not_claimed_either_way() -> None:
	"""What this test cannot say, said out loud.

	`NOTHING`, `OPAQUE` and `UNPLACED` have no members in the corpus, so
	the partition above is silent about them: a kind nothing produces is
	neither probed nor a hole. Asserted so that the day a corpus schema
	grows one, this says so by failing -- which is how the figure gets
	re-measured instead of inherited.
	"""
	seen, _ = _coverage()
	absent = {kind for kind in Member if not seen.get(kind)}

	assert absent == {Member.NOTHING, Member.OPAQUE, Member.UNPLACED}, (
		"the kinds with no corpus population have changed, so the "
		f"partition's silence covers something new: {sorted(str(k) for k in absent)}")
