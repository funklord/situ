"""Every refusal the builder emits, checked against the member it names.

Five of this builder's increments produced a message that was right in
VERDICT and wrong in REASON, and four of those had one cause: a branch
ordered by what a member lacks rather than by what it is. A `before` run
was told its delimiter count, a nested field was called a run, a varint
was called a nested struct, and seventeen refusals hedged between two
reasons the code could tell apart. Twice a test of mine had pinned the
wrong message in place, so correcting it would have gone red.

A refusal is the builder's only interface for everything it cannot do,
and it was the least-checked thing in it. So this is the gate: each
message carries a CLAIM about the member, and the claim is asserted
against the member the message names. A message that says one thing
while the member is another fails here rather than being believed.

It also holds two shapes that are not about any one message. Every
member-level refusal names its member first, so a reader knows which
one -- and the only refusals that name none are the three that are about
the struct. And every claim in the table has a corpus instance or is
listed as constructed, so the table cannot accumulate rows for messages
nothing emits.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

import pytest					# noqa: E402
from collections.abc import Callable		# noqa: E402
from every_schema import SCHEMAS, load_schema	# noqa: E402
from situc import traverse			# noqa: E402
from situc.codegen.c import build		# noqa: E402
from situc.codegen.c import generate as generate_c	# noqa: E402
from situc.layout import Placement, solve		# noqa: E402
from situc.resolve import ResolvedSchema, ResolvedStruct, resolve  # noqa: E402

#: What each message asserts about the member it names. The fragment has
#: to be unique across the table -- two rows matching one message would
#: mean the gate could not say which claim was being made.
Claim = Callable[[Placement, ResolvedSchema], bool]

CLAIMS: tuple[tuple[str, Claim], ...] = (
	("repeats while a condition holds",
	 lambda pl, res: pl.repeat_while is not None),
	("holds a run of",
	 lambda pl, res: pl.scalar is not None and pl.array_count is not None),
	("is a run of",
	 lambda pl, res: pl.element_bits != 8),
	("is a sealed region", lambda pl, res: pl.kind == "sealed"),
	("is a coded region", lambda pl, res: pl.kind == "coded"),
	("is a TLV region", lambda pl, res: pl.kind == "tlv"),
	("is a marker", lambda pl, res: pl.kind == "marker"),
	("which follows it", lambda pl, res: bool(pl.tag_covers)),
	("is sized by the expression",
	 lambda pl, res: pl.size_expr is not None),
	("is a tag whose schema names no codec",
	 lambda pl, res: pl.kind in ("tag", "checksum") or bool(pl.tag_covers)),
	("-bit tag, and the runtime stores",
	 lambda pl, res: pl.kind in ("tag", "checksum") or bool(pl.tag_covers)),
	("is reserved and has no stated length",
	 lambda pl, res: pl.kind in ("reserved", "preamble")),
	("names the varint type", lambda pl, res: pl.varint is not None),
	("varint, and the runtime writes LEB128",
	 lambda pl, res: pl.varint is not None),
	("escapes its delimiter",
	 lambda pl, res: bool(pl.delimiter_escape)),
	("is trimmed", lambda pl, res: bool(pl.trim_set)),
	("is quoted", lambda pl, res: bool(pl.delimiter_quote)),
	("delimiters, so which byte to write after it is not stated",
	 lambda pl, res: len(pl.delimiters) > 1),
	("run, so nothing it writes ends it",
	 lambda pl, res: bool(pl.delimiters) and not pl.delimiter_consumed),
	("is a signed number written in digits",
	 lambda pl, res: pl.radix is not None),
	("is written in digits with neither a width",
	 lambda pl, res: pl.radix is not None),
	("is fixed point", lambda pl, res: pl.scalar is not None),
	("is packed and its value is not its bits",
	 lambda pl, res: build.packed(pl)),
	("bits wide, so it shares a byte",
	 lambda pl, res: (pl.scalar is not None
	                  and pl.scalar.bits % 8 != 0)),
	("which is whole bytes and not a width the runtime stores",
	 lambda pl, res: (pl.scalar is not None and pl.scalar.bits % 8 == 0
	                  and pl.scalar.bits not in (8, 16, 32, 64))),
	("comes from a struct argument", lambda pl, res: bool(pl.parameter)),
	("is located by an expression", lambda pl, res: bool(pl.located)),
	("is an index table", lambda pl, res: pl.index_table is not None),
	("takes whatever is left", lambda pl, res: bool(pl.remaining_cap)),
	("is the stated size of both", lambda pl, res: True),
	("has no stated length, so nothing says when",
	 lambda pl, res: not pl.delimiters and pl.array_count is None),
	("whose own bytes do not state its extent",
	 lambda pl, res: pl.type_name in res.structs),
	("which ends `before` a byte it does not own",
	 lambda pl, res: pl.type_name in res.structs),
	("is neither a scalar nor a struct of this schema",
	 lambda pl, res: pl.type_name not in res.structs and pl.scalar is None),
	("is a variant", lambda pl, res: pl.kind == "variant"),
	("is the discriminant of a variant", lambda pl, res: True),
	("pads to an alignment", lambda pl, res: pl.pad_to is not None),
	("is a length-hiding pad", lambda pl, res: pl.pad_bounds is not None),
	("is a peeked", lambda pl, res: bool(pl.peek)),
	("packs into a group", lambda pl, res: build.packed(pl)),
	("is a packed", lambda pl, res: build.packed(pl)),
	("ends at a multi-byte delimiter",
	 lambda pl, res: bool(pl.delimiters) and len(pl.delimiters[0]) > 1),
	("is minimal and ends at any of",
	 lambda pl, res: pl.radix is not None),
	("is minimal and does not consume its delimiter",
	 lambda pl, res: pl.radix is not None),
	("is minimal with no `max`", lambda pl, res: pl.radix is not None),
	("which is a digit at radix", lambda pl, res: pl.radix is not None),
	("has no byte order for its codec's output",
	 lambda pl, res: pl.kind in ("tag", "checksum") or bool(pl.tag_covers)),
	("is spelled the same as one of the writer's own locals",
	 lambda pl, res: True),
	("sizes `", lambda pl, res: True),
	("is not a member of this struct", lambda pl, res: True),
	("holds something this writer cannot place",
	 lambda pl, res: pl.scalar is None and pl.type_name not in res.structs),
)

#: The refusals that are about the STRUCT and so name no member. Anything
#: else without a leading member name is a message a reader cannot act on.
ABOUT_THE_STRUCT = (
	"is a register, which is a bus transaction",
	"is entirely literal, so there is nothing to build",
	"has no members to write",
	"bits, not a whole number of bytes",
)


def _shape(parsed, resolved):			# type: ignore[no-untyped-def]
	header = generate_c(parsed, resolved, "x").files().get("x.h", "")
	return build.Shape(header, "situ", resolved.structs,
	                   {decl.name: decl for decl in parsed.varints()})


def _member(struct: ResolvedStruct, quoted: str) -> Placement | None:
	"""The placement a message names, by its local name or its own."""
	for entry in struct.entries:
		if traverse.local_name(struct, entry.placement) == quoted:
			return entry.placement
		if entry.placement.name == quoted:
			return entry.placement
	return None


def _refusals(path: Path):			# type: ignore[no-untyped-def]
	parsed   = load_schema(path)
	resolved = resolve(parsed, solve(parsed))
	shape    = _shape(parsed, resolved)	# type: ignore[no-untyped-call]
	return resolved, build.refusals(resolved, shape)


@pytest.mark.parametrize("path", SCHEMAS, ids=lambda p: p.stem)
def test_every_refusal_names_a_member_and_a_claim_that_holds(
		path: Path) -> None:
	resolved, refused = _refusals(path)

	for name, why in refused:
		struct = resolved.structs[name.split(".")[0]]

		if not why.startswith("`"):
			assert any(part in why for part in ABOUT_THE_STRUCT), (
				f"{name}: a refusal that names no member has to be about "
				f"the struct, and this is not: {why}")
			continue

		quoted = why.split("`")[1]
		held   = _member(struct, quoted)
		assert held is not None, (
			f"{name}: the message names `{quoted}`, which is not a member "
			f"of `{struct.name}`: {why}")

		matched = [(fragment, claim) for fragment, claim in CLAIMS
		           if fragment in why]
		assert len(matched) == 1, (
			f"{name}: {len(matched)} claims match this message, and the "
			f"gate needs exactly one to know what is being claimed: "
			f"{why}\n  matched: {[f for f, _ in matched]}")

		fragment, claim = matched[0]
		assert claim(held, resolved), (
			f"{name}: the message claims `{fragment}` and the member is "
			f"not that. kind={held.kind} type={held.type_name!r} "
			f"scalar={held.scalar is not None} varint={held.varint!r} "
			f"delimiters={held.delimiters!r} arr={held.array_count}\n"
			f"  {why}")


def test_every_claim_in_the_table_is_one_the_builder_emits() -> None:
	"""A table of claims nothing emits is a table that rots.

	Each row is either seen over the corpus or named below as constructed
	-- a shape no corpus schema has, which the builder's own tests reach
	with an inline one. The alternative is rows accumulating for messages
	that were reworded or deleted, which is the stale-claim class
	`evidence.md` names.
	"""
	seen: set[str] = set()
	for path in SCHEMAS:
		_, refused = _refusals(path)
		for _, why in refused:
			seen.update(fragment for fragment, _ in CLAIMS
			            if fragment in why)

	#: Reached by the builder's own inline fixtures rather than by any
	#: corpus schema, or by a guard whose population is empty here.
	CONSTRUCTED = {
		"is minimal and ends at any of", "is minimal and does not consume",
		"is minimal with no `max`", "which is a digit at radix",
		"is written in digits with neither a width",
		"is spelled the same as one of the writer's own locals",
		"is the stated size of both", "is not a member of this struct",
		"pads to an alignment", "is a length-hiding pad",
		"is a peeked", "packs into a group", "is a packed",
		"has no byte order for its codec's output",
		"holds something this writer cannot place",
		"is the discriminant of a variant", "is quoted", "is a variant",
		"is minimal and ends at any of",
		"is minimal and does not consume its delimiter",
		# This one HAD a corpus population and lost it, which is the more
		# interesting way a row goes quiet: every sub-byte member in the
		# corpus is now written as part of a packed group, so the only
		# routes left are an arm holding a sub-byte scalar and a sub-byte
		# size field, and the corpus has neither.
		"bits wide, so it shares a byte",
		"ends at a multi-byte delimiter", "sizes `",
		"names the varint type", "is fixed point",
		"is neither a scalar nor a struct of this schema",
	}
	unseen = {fragment for fragment, _ in CLAIMS} - seen - CONSTRUCTED
	assert not unseen, (
		"these claims are in the table, are not emitted over the corpus, "
		f"and are not listed as constructed: {sorted(unseen)}")
