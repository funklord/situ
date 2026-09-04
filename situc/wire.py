"""The byte-level contract a schema states (project.md section 19.3).

`situc map` says what a field *costs*. This says what a byte *means*, which is
a different question with a different audience: the map is for whoever calls
the accessors, and this is for whoever is already running the old version.

The two were conflated, and the gap was not academic. `situc diff` compares
capability vectors, so flipping the byte order of a whole schema reported "No
capability change", and moving a field out of an authenticated region reported
an *improvement* -- correctly, by the lattice's ordering, because a field with
no tag over it is cheaper to write. A cost ordering is not a compatibility
ordering.

Committed beside the schema and checked, for the reason the map is (18.1): a
wire break should be a red diff at the moment somebody edits the schema, not
something a peer discovers in production. Every line here is a promise to a
receiver that is already deployed and cannot be recompiled.

**Positional, not nominal.** Situ has no field numbers; position carries
identity (section 4). So a rename changes nothing on the wire, and this file
records the name only so a diff can say *which* field moved -- a rename shows
up as an API change and a wire non-event, which is exactly what it is.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePath

from situc import ast
from situc.types import pinned_shown
from situc.layout import (
	Arm, BITS_PER_BYTE, IndexTable, KnownTag, Placement, ValueRule)
from situc.resolve import ResolvedSchema, ResolvedStruct
from situc.traverse import own_members

#: Bumped from 0, which 0041 kept because new facts on existing lines are
#: what the fact list is for and a comparator ignores tokens it does not know.
#: This adds new *kinds* of line -- the varint block, and the `name what: ...`
#: lines a variant and a tlv region state their contract on -- and a v0
#: comparator reading one of those counts it as a member, which slides every
#: member after it by one position and reports the slide as a break. A reader
#: that cannot tell the two apart has to be told, so the number moves.
FORMAT_VERSION = 1


def render(schema: ast.Schema, resolved: ResolvedSchema, path: str) -> str:
	"""The whole signature, in a form a reviewer can read a diff of."""
	lines = [
		f"# situ wire signature v{FORMAT_VERSION}",
		f"# schema: {PurePath(path).name}",
		"#",
		"# What the bytes mean, and nothing about what they cost. Committed and",
		"# checked so that a change here is a change somebody reviews.",
		"#",
		"# One line per member: where it starts, how wide it is, and every",
		"# constraint a sender has to satisfy or a receiver may rely on.",
		"# Positions are byte offsets, or byte:bit off a boundary; `~` marks one",
		"# the data decides rather than the schema.",
		"#",
		"# A construct whose contract is a list rather than an extent -- a",
		"# variant's arms, a tlv region's grammar, an indexed region's offset",
		"# table, the bytes a tag covers -- states it on `name what: value`",
		"# lines below the members, one line per entry so that a diff names the",
		"# entry that moved.",
	]

	lines.extend(_directives(schema))
	lines.extend(_varints(schema))
	lines.extend(_enums(schema, resolved))

	for name in sorted(resolved.structs):
		lines.append("")
		lines.extend(_struct(resolved.structs[name]))

	return "\n".join(lines) + "\n"


def _directives(schema: ast.Schema) -> list[str]:
	"""The schema-wide facts, which are wire facts before they are anything.

	Byte order first, because it is the change that alters every byte in every
	message while leaving the structure identical -- the one a structural diff
	is guaranteed to miss.
	"""
	found = []
	for decl in schema.decls:
		if isinstance(decl, ast.EndianDirective):
			found.append(f"endian {decl.endian.value}")
		elif isinstance(decl, ast.BitOrderDirective):
			found.append(f"bit_order {decl.bit_order.value}")
		elif isinstance(decl, ast.TargetDirective):
			found.append(f"target {decl.kind.value}")

	return ["", *sorted(found)] if found else []


def _varints(schema: ast.Schema) -> list[str]:
	"""How each varint type spells a number.

	Here for `_directives`' reason rather than `_enums`': the encoding *is* the
	byte order of every value of the type, so `be128` -> `leb128` reads every
	varint in every message backwards and leaves every offset, width and name
	exactly where it was. The member line said `varint=sqlite_varint` and
	stopped, which records the label on the change and not the change.

	`bytes` is derived rather than declared for most types, and is here anyway:
	it is the ceiling a receiver stops scanning at, so a format that declares a
	shorter one (SQLite's nine) has told its peers something they act on.
	"""
	lines = []
	for decl in schema.varints():
		facts = [f"bits={decl.max_bits}", f"bytes={decl.max_bytes}"]
		# Whether one number has one spelling. Without it a receiver must
		# accept a padded encoding of a value it also sees unpadded, which is
		# a fact about the bytes and not about the values they carry.
		if decl.minimal:
			facts.append("minimal")
		if decl.transform is not None:
			facts.append(decl.transform.value)
		lines.extend([
			"",
			f"varint {decl.name} : {decl.encoding.value} {' '.join(facts)}",
		])
	return lines


def _enums(schema: ast.Schema, resolved: ResolvedSchema) -> list[str]:
	"""Which values each enum admits, and what happens to the rest.

	A wire fact and an easily missed one. Under `default = error` -- which is
	the default (8.7) -- a receiver rejects any value not listed here, so
	adding a member is something new senders may do and old receivers will
	refuse. Under `default = pass` it is free. The two look identical in the
	schema and could not differ more in a deployment.
	"""
	lines = []
	for decl in schema.enums():
		# A byte-run enum's arms are spans, and the signature prints them as
		# written: the point of the construct is that a signature has no
		# byte order, so rendering `BM` as a number here would put one back
		# in the one document a reader checks the wire against (0052).
		if decl.width is not None:
			runs   = resolved.layout.env.byte_enums[decl.name]
			listed = " ".join(f'{name}="{pinned_shown(run)}"'
			                  for name, run in runs.items())
			width  = f"[{decl.width}]"
		else:
			values = resolved.layout.env.enums[decl.name]
			listed = " ".join(f"{member.name}={values[member.name]}"
			                  for member in decl.members)
			width  = ""
		lines.extend([
			"",
			f"enum {decl.name} : {decl.backing.name}{width} "
			f"unknown={decl.effective_default.value}",
			f"  {listed}" if listed else "  (no members)",
		])
	return lines


def _struct(struct: ResolvedStruct) -> list[str]:
	layout = struct.layout
	if layout.is_fixed_size:
		extent = f"size={layout.size_bytes}"
	elif layout.size_max_bytes is not None:
		extent = f"size={layout.size_bytes}..{layout.size_max_bytes}"
	else:
		extent = f"size={layout.size_bytes}.."

	head    = f"struct {struct.name} {extent}"
	version = _version_field(struct)
	if version is not None:
		head += f" version={version}"

	lines = [head]
	lines.extend(f"  {_member(placement)}" for placement in own_members(struct))
	lines.extend(_composites(struct))
	lines.extend(_coverage(struct))
	return lines


def _version_field(struct: ResolvedStruct) -> str | None:
	"""Which member's value says which version a message is (19.4).

	A property of the struct rather than of any one member, and the fact that
	decides whether a `since` member's bytes are present at all. Moving the
	version to another member leaves every offset, width and name identical
	and makes the same bytes a different length of message, which is the shape
	this file exists to catch. It is also what `_compare_member` leans on to
	call an appended `since` member provably safe -- so the claim and the
	field it rests on now travel together.
	"""
	for placement in own_members(struct):
		if placement.version_field is not None:
			return placement.version_field
	return None


def _member(placement: Placement) -> str:
	"""One member's contract, as one line so a diff points at one thing."""
	parts = [
		_position(placement).ljust(9),
		_width(placement).ljust(9),
		placement.type_name.ljust(10),
		placement.name,
	]
	facts = _constraints(placement)
	return " ".join(parts).rstrip() + (f"  {' '.join(facts)}" if facts else "")


def _position(placement: Placement) -> str:
	if placement.offset_bits is None:
		# The data decides. Which member it follows is what fixes it, and that
		# is the line above -- so a diff that moves a `~` member has already
		# shown the reason two lines up.
		return "~"

	byte, bit = divmod(placement.offset_bits, BITS_PER_BYTE)
	return f"@{byte:#06x}" + (f":{bit}" if bit else "")


def _width(placement: Placement) -> str:
	if placement.delimiter is not None:
		return f"until:{placement.delimiter.hex()}"
	if placement.size_max_bits != placement.size_bits:
		hi = ("" if placement.size_max_bits is None
		      else str(placement.size_max_bits // BITS_PER_BYTE))
		return f"{placement.size_bits // BITS_PER_BYTE}..{hi}"
	if placement.size_bits % BITS_PER_BYTE:
		return f"{placement.size_bits}bit"
	return str(placement.size_bits // BITS_PER_BYTE)


def _constraints(placement: Placement) -> list[str]:
	"""Everything a peer may rely on, and nothing it may not.

	A receiver written against this file is entitled to assume each of these
	holds of any message it is sent, so removing one is a promise withdrawn
	and adding one is a promise demanded. Which of those two is a break
	depends on the direction, and the comparison names it.
	"""
	facts: list[str] = []

	if placement.endian is not None and placement.size_bits > BITS_PER_BYTE:
		facts.append(placement.endian.value)
	if placement.bit_order is not None and placement.size_bits % BITS_PER_BYTE:
		facts.append(placement.bit_order.value)
	if placement.marker is not None:
		facts.append(f"endian-from={placement.marker}")
	if placement.sized_by is not None:
		facts.append(f"sized-by={placement.sized_by}")
	# The same question -- where does the length come from -- where the answer
	# is arithmetic rather than a name. `sized_by` holds a path and holds
	# nothing at all for `u8 body[hi * 256 + lo]`, so the commonest shape there
	# is (a length in units, or split across two fields) recorded no length:
	# swapping `hi` and `lo` moves every payload boundary in every message and
	# produced a byte-identical signature. One key for both, because a reader
	# asks the same thing of either.
	elif placement.size_expr is not None:
		# The shown form, not the stored one. This file is a contract a peer
		# reads and diffs, and `size_expr` is parenthesised at every operator
		# so that four host compilers cannot disagree about it -- a property
		# of the *generated code*, which is nothing to do with the wire. A
		# signature that churned to `sized-by=(length-8)` would be reporting
		# a change no peer can observe.
		facts.append(f"sized-by="
		             f"{_squash(placement.size_shown or placement.size_expr)}")
	# `at hdr.pixel_offset`: where the bytes *are*. `_position` renders `~` for
	# this member exactly as it does for one the data merely displaced, and the
	# two are different promises -- a displaced member follows the line above,
	# and this one goes wherever a field says, however far that is from
	# anything. The line above is not the reason, so it has to be stated.
	if placement.located is not None:
		facts.append(f"at={placement.located}")
	# Where a run stops, which is the boundary between this member and the
	# next. Nothing else ends one, so the condition is the width.
	if placement.repeat_while is not None:
		facts.append(f"while="
		             f"{_squash(placement.repeat_shown or placement.repeat_while)}")
	if placement.repeat_cap is not None:
		facts.append(f"while-max={placement.repeat_cap}")
	# The alignment a pad promises (0043): a peer that pads to a different
	# multiple disagrees about where the next field starts, so it is contract.
	if placement.pad_to is not None:
		facts.append(f"pad-to={placement.pad_to}")
	if placement.radix is not None:
		facts.append(f"radix={placement.radix}")
	if placement.radix_minimal:
		facts.append("minimal")
	if placement.trimmed:
		facts.append("trim")
	if placement.case_insensitive:
		facts.append("fold-case")
	if placement.delimiter_quote is not None:
		facts.append(f"quote={placement.delimiter_quote:#04x}")
	if placement.delimiter_escape is not None:
		facts.append(f"escape={placement.delimiter_escape:#04x}")
	if placement.delimiter_cap is not None:
		facts.append(f"cap={placement.delimiter_cap}")
	if placement.since is not None:
		facts.append(f"since={placement.since}")
	if placement.varint is not None:
		facts.append(f"varint={placement.varint}")
	if placement.codec is not None:
		facts.append(f"codec={placement.codec}")
	# Which field seeds the nonce and which selects the key (0040) is what
	# the region's bytes *mean* -- a receiver that disagrees about either
	# cannot interoperate -- and both are enforced, which is this file's
	# admission test (0041).
	if placement.sealed_nonce is not None:
		facts.append(f"nonce={placement.sealed_nonce}")
	if placement.sealed_key is not None:
		facts.append(f"key={placement.sealed_key}")

	facts.extend(_attribute_facts(placement))
	return facts


def _squash(source: str) -> str:
	"""Schema source as one fact token.

	A fact is delimited by spaces, so an expression cannot keep its own. The
	result is still the schema's arithmetic and still unambiguous -- what it
	loses is the spacing, which no peer can observe.
	"""
	return "".join(source.split())


#: Attributes a peer can observe in the bytes. `[secret]` and the register
#: access modes are deliberately absent: they change the generated API and
#: nothing a receiver could detect.
#:
#: `self_as` is here for `tag_prefix`'s reason (see `_coverage`): the value a
#: checksum field is taken as while the sum runs over it is invisible in the
#: structure, and two peers that disagree about it compute different sums over
#: byte-identical messages.
WIRE_ATTRS = (
	"must_eq", "min", "max", "must_be_zero", "must_be_one",
	"encoding", "nul_terminated", "self_as",
)


def _attribute_facts(placement: Placement) -> list[str]:
	from situc.unparse import expr_to_source

	facts = []
	for attr in placement.attrs:
		if attr.name not in WIRE_ATTRS:
			continue
		if attr.value is None:
			facts.append(attr.name)
		else:
			facts.append(f"{attr.name}={expr_to_source(attr.value)}")
	return facts


def _composites(struct: ResolvedStruct) -> list[str]:
	"""The constructs whose contract is a list rather than an extent.

	A variant, a tlv region and an `indexed` one each get one member line, and
	that line says where the construct starts and how wide it can be -- which
	is the same whatever the discriminant selects, whatever the grammar says
	and wherever the offsets are measured from. Swapping two arms of
	`keystore.params` rewrote eighty-two lines of generated C and no byte of
	this file; so did moving a protobuf tag from 1 to 7, changing `tag >> 3`
	to `tag >> 4`, halving wire type 1's value, and moving `sqlite.cells` from
	`base = page_type` to `base = region`. Every one of those is a peer
	reading different bytes as a different thing, which is the whole subject.

	Rendered as one line per entry rather than as fact tokens on the member
	line, for the reason `_coverage` is: an arm list is 21 entries in modbus
	and a grammar is fifteen in protobuf, and folded onto one line a changed
	entry is a 600-character line that differs somewhere. This file's value is
	the reviewable diff, so the entry is the unit.
	"""
	lines = []
	for entry in struct.entries:
		held = entry.placement
		if held.kind == "variant":
			lines.extend(_arms(struct, held))
		elif held.kind == "tlv":
			lines.extend(_tlv(held))
		elif held.kind == "indexed":
			lines.extend(_index(held))
	return lines


def _arms(struct: ResolvedStruct, placement: Placement) -> list[str]:
	"""Which discriminant value selects which arm, and what that arm is.

	The mapping is the variant, and none of it survives into the member line:
	`discriminant` and `arm_cases` were read by every backend and by nothing
	here. The arm's *type* is named beside the member because an arm's members
	appear in no member line of their own, so a swap to another struct of the
	same worst case would otherwise be invisible too.
	"""
	by_path = {entry.placement.path: entry.placement for entry in struct.entries}
	lines   = [f"  {placement.name} switch: {placement.discriminant}"]

	# By the value on the wire rather than by declaration order, `default`
	# last. Reordering the `case` clauses changes nothing a peer can observe,
	# and a signature that churns for it teaches people to skim the diff --
	# which is the one thing this file cannot survive.
	for arm in sorted(placement.arm_cases, key=_arm_order):
		label = "default" if arm.value is None else str(arm.value)
		if arm.member is None:
			# `default: error` selects nothing, and that is a wire fact in its
			# own right: a discriminant with no arm is a message this build
			# refuses rather than one it reads short.
			lines.append(f"  {placement.name} case {label}: error")
			continue

		picked = by_path.get(arm.member)
		named  = arm.member.rpartition(".")[2]
		lines.append(f"  {placement.name} case {label}: {named}"
		             + (f" {picked.type_name}" if picked is not None else ""))
	return lines


def _arm_order(arm: Arm) -> tuple[int, int]:
	"""Discriminant order, with `default` after every value it stands in for."""
	return (1, 0) if arm.value is None else (0, arm.value)


def _rule_order(rule: ValueRule) -> tuple[int, int]:
	return (1, 0) if rule.label is None else (0, rule.label)


def _tlv(placement: Placement) -> list[str]:
	"""How a tlv region's items are found, and what its tags mean.

	Rendered in full rather than digested. A digest would say that the grammar
	changed and refuse to say what, and this file is committed so that a
	reviewer can read the change -- a fifteen-line block that a one-line edit
	moves one line of is worth more than eight opaque hex digits. It stays
	bounded because a grammar is written once per region: protobuf's is the
	only one in the tree and it is fifteen lines. A `known` map of a hundred
	tags would be a hundred lines, and would deserve them, being a hundred
	separate promises.
	"""
	name    = placement.name
	grammar = placement.tlv_grammar
	lines   = []

	# The tag's own type first: it is read before anything else in the item
	# and a different varint reads a different tag out of the same bytes.
	if placement.tlv_tag_varint is not None:
		lines.append(f"  {name} tlv tag: {placement.tlv_tag_varint}")

	if grammar is not None:
		# Sorted throughout for `_arms`' reason: a decode part, a value rule
		# and a known tag are each found by name, wire type or tag, so the
		# order they were written in is not a fact about any message.
		for part in sorted(grammar.tag_decode, key=lambda p: p.name):
			lines.append(f"  {name} tlv decode {part.name}: {part.source}")
		# Which decoded part a `known` key matches (0023). Matching the other
		# one finds an item, and not the one asked for.
		if grammar.identity is not None:
			lines.append(f"  {name} tlv identity: {grammar.identity}")
		if grammar.length_type is not None:
			lines.append(f"  {name} tlv length: {grammar.length_type}")
		if grammar.selector is not None:
			lines.append(f"  {name} tlv select: {grammar.selector}")
		for rule in sorted(grammar.rules, key=_rule_order):
			label = "default" if rule.label is None else str(rule.label)
			lines.append(f"  {name} tlv size {label}: {_value_rule(rule)}")
		for known in sorted(grammar.known, key=lambda k: k.tag):
			lines.append(f"  {name} tlv known {known.tag}: {_known_tag(known)}")

	# The policies, which decide what a receiver does with what the grammar
	# admits: whether a tag it does not know survives a round trip, and which
	# of two items with one tag it believes.
	if placement.tlv_duplicates is not None:
		lines.append(f"  {name} tlv duplicates: {placement.tlv_duplicates}")
	if placement.tlv_unknown is not None:
		lines.append(f"  {name} tlv unknown: {placement.tlv_unknown}")
	if placement.tlv_ordered:
		lines.append(f"  {name} tlv ordered: yes")
	return lines


def _value_rule(rule: ValueRule) -> str:
	"""One of section 9.5's four ways for a value to say where it ends."""
	parts = [rule.kind]
	if rule.size is not None:
		parts.append(str(rule.size))
	if rule.length_type is not None:
		parts.append(rule.length_type)
	return " ".join(parts)


def _known_tag(known: KnownTag) -> str:
	parts = [known.name]
	if known.wire is not None:
		parts.append(f"wire={known.wire}")
	if known.type_name is not None:
		parts.append(f"type={known.type_name}")
	if known.repeated:
		parts.append("repeated")
	return " ".join(parts)


def _index(placement: Placement) -> list[str]:
	"""How an `indexed` region's offset table is read (section 9.3).

	Two lines, which are the two facts the member line has no room for. The
	other two the table holds are already on it and are deliberately not
	repeated here: a fact stated twice is a change reported twice, and the two
	reports do not agree -- a member fact whose value moved is `backward` by
	`_changed_constraint` and an annotation whose value moved is `breaking`
	below, so one change would print under both headings. The element type is
	the member line's type column, which `_compare_member` compares. The count
	is `sized-by=` where a field gives it; where a literal does, the table's
	own extent is `count * entry`, so the width column carries it as soon as
	the entry width is recorded, which is the first line below.

	What is left is the pair that decides where every element in every message
	is. `entry` is how wide one slot is, so it says both how far the table
	runs and which bytes an offset is read out of. `base` is decision 0024:
	the same two bytes name a different byte of the message measured from the
	region, from the message, or from a member declared before it. Neither
	survived into the layout this file reads -- `sqlite.cells` moving from
	`base = page_type` to `base = region` moved every cell in every page and
	produced a byte-identical signature.
	"""
	table = placement.index_table
	if table is None:
		return []

	name = placement.name
	# In bytes, the unit every width in this file is written in. Always whole:
	# `place_indexed` refuses an offset type that is not a whole-byte scalar,
	# so there is no bit-width form to render here.
	return [
		f"  {name} index entry: {table.entry_bits // BITS_PER_BYTE}",
		f"  {name} index base: {_index_base(table)}",
	]


def _index_base(table: IndexTable) -> str:
	"""What an offset is measured from, with the member named where one is.

	The kind is always written, `member` included, so that a member called
	`region` cannot be read as the region itself.
	"""
	if table.base == "member":
		return f"member {table.base_member}"
	return table.base


def _coverage(struct: ResolvedStruct) -> list[str]:
	"""Which bytes each tag authenticates.

	The half of the contract that has nothing to do with layout and is the
	most expensive to get wrong. A field leaving a covered region parses
	identically on both sides and simply stops verifying -- and if it left
	because somebody widened a region boundary by accident, the new version
	authenticates less than the old one and nothing about the structure says
	so.
	"""
	lines = []
	for placement in struct.entries:
		held = placement.placement

		# A `coded` region's `covers` belongs here for this function's own
		# reason (14.1a). Widening it makes a peer transform bytes the old
		# version left alone, and the structure is identical either way --
		# which is the shape of change this signature exists to catch.
		if held.kind == "coded" and held.coded_covers:
			over = " ".join(sorted(held.coded_covers))
			lines.append(f"  {held.name} covers: {held.name} {over}")
			continue

		if held.kind not in ("tag", "checksum"):
			continue

		covered = sorted(
			entry.placement.path for entry in struct.entries
			if held.name in entry.placement.covered_by)
		names = " ".join(path.rpartition(".")[2] for path in covered) or "nothing"
		# The prefix is part of what a peer has to sum (14.2a), and it is
		# invisible in the structure: every member, offset and size is
		# identical whether or not a pseudo-header is covered, so a change
		# here is exactly the kind this signature exists to catch.
		if held.tag_prefix is not None:
			names = f"{held.tag_prefix}(prefix) {names}"
		lines.append(f"  {held.name} covers: {names}")
	return lines


# ---------------------------------------------------------------------------
# Comparing two signatures
#
# Textual, and deliberately: the signature is the contract, so two signatures
# that differ are two contracts, and what matters is which of the differences a
# deployed peer would survive. Re-deriving the comparison from the AST would
# mean two descriptions of the contract that could disagree.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
	"""One difference, and who it hurts."""

	#: "breaking", "backward", "forward", "coverage", "api"
	kind: str
	subject: str
	detail: str

	@property
	def is_break(self) -> bool:
		return self.kind in ("breaking", "coverage")


@dataclass(frozen=True)
class Verdict:
	findings: list[Finding]

	@property
	def breaking(self) -> bool:
		return any(finding.is_break for finding in self.findings)


#: How each kind reads, and what a reviewer should take from it.
KINDS = {
	"breaking": ("BREAKING",
	             "a deployed peer misreads these bytes"),
	"coverage": ("COVERAGE",
	             "what a tag authenticates changed; peers agree on the bytes "
	             "and disagree on the tag, and if the region shrank the new "
	             "build authenticates less than the old one did"),
	"backward": ("backward-compatible",
	             "a new receiver still reads what an old sender produces; an "
	             "old receiver may refuse what a new sender produces"),
	"forward":  ("forward-compatible",
	             "an old receiver still reads what a new sender produces"),
	"api":      ("api-only",
	             "the bytes are unchanged; calling code has to be edited"),
}


def compare(before: str, after: str) -> Verdict:
	"""Classify what changed between two committed signatures.

	Positional, because situ is: a member's identity is where it sits, so a
	renamed field at the same offset with the same width is an API change and
	a wire non-event, and a field of a different width at the same offset is
	a break whatever it is called.
	"""
	old_structs = _parse_signature(before)
	new_structs = _parse_signature(after)
	findings: list[Finding] = []

	findings.extend(_compare_globals(before, after))

	for name in sorted(set(old_structs) | set(new_structs)):
		old = old_structs.get(name)
		new = new_structs.get(name)

		if old is None:
			findings.append(Finding("forward", name,
			                        "new struct; no old sender emits one"))
			continue
		if new is None:
			findings.append(Finding("breaking", name,
			                        "struct removed; an old peer still sends it"))
			continue
		findings.extend(_compare_struct(name, old, new))

	return Verdict(findings)


def _compare_globals(before: str, after: str) -> list[Finding]:
	"""Byte order, bit order, target, and every enum's admitted set.

	Byte order first because it is the change that rewrites every message
	while leaving the structure identical -- and so the one a structural diff
	is guaranteed to miss. `situc diff` reported "No capability change" for it.
	"""
	findings = []

	for directive in ("endian", "bit_order", "target"):
		was = _directive_of(before, directive)
		now = _directive_of(after, directive)
		if was != now and was is not None and now is not None:
			findings.append(Finding(
				"breaking", directive,
				f"{was} -> {now}: every multi-byte field in every message is "
				f"read differently, and nothing about the structure says so"))

	findings.extend(_compare_varints(before, after))
	findings.extend(_compare_enums(before, after))
	return findings


def _compare_varints(before: str, after: str) -> list[Finding]:
	"""A varint type respelled, which is a byte order under another name.

	Only a changed declaration is reported: a type gained or lost changes the
	type column of every member that used it, and `_compare_member` says so
	once per member rather than once here.
	"""
	old = _parse_varints(before)
	new = _parse_varints(after)

	return [Finding(
		"breaking", f"varint {name}",
		f"{old[name]} -> {new[name]}; every value of this type is read "
		"differently, and no offset, width or name moves")
		for name in sorted(set(old) & set(new)) if old[name] != new[name]]


def _parse_varints(text: str) -> dict[str, str]:
	varints = {}
	for line in text.splitlines():
		if line.startswith("varint "):
			name, _, spelling = line[len("varint "):].partition(" : ")
			varints[name] = spelling
	return varints


def _directive_of(text: str, name: str) -> str | None:
	for line in text.splitlines():
		if line.startswith(f"{name} "):
			return line.split(" ", 1)[1]
	return None


def _compare_enums(before: str, after: str) -> list[Finding]:
	"""Adding a member is free under `pass` and a break under `error`.

	The two spellings look identical in a schema and could not differ more in
	a deployment: under `error` an old receiver rejects the new value, so the
	new sender cannot talk to it at all.
	"""
	old = _parse_enums(before)
	new = _parse_enums(after)
	findings = []

	for name in sorted(set(old) & set(new)):
		(old_policy, old_values) = old[name]
		(new_policy, new_values) = new[name]

		if old_policy != new_policy:
			findings.append(Finding(
				"breaking" if new_policy == "error" else "forward",
				f"enum {name}",
				f"unknown values: {old_policy} -> {new_policy}"))

		gained = sorted(set(new_values) - set(old_values))
		lost   = sorted(set(old_values) - set(new_values))

		if gained:
			findings.append(Finding(
				"breaking" if old_policy == "error" else "backward",
				f"enum {name}",
				f"gains {', '.join(gained)}; an old receiver "
				+ ("refuses them, because it was written to reject unknown "
				   "values" if old_policy == "error" else "passes them through")))
		if lost:
			findings.append(Finding(
				"breaking", f"enum {name}",
				f"drops {', '.join(lost)}; an old sender still emits them"))

	return findings


def _parse_signature(text: str) -> dict[str, list[str]]:
	"""Struct name to its member and coverage lines, verbatim."""
	structs: dict[str, list[str]] = {}
	current: list[str] | None = None

	for line in text.splitlines():
		if line.startswith("struct "):
			name = line.split()[1]
			current = structs.setdefault(name, [line])
		elif line.startswith("  ") and current is not None:
			current.append(line.strip())
		elif not line.startswith("  "):
			current = None
	return structs


def _parse_enums(text: str) -> dict[str, tuple[str, list[str]]]:
	enums: dict[str, tuple[str, list[str]]] = {}
	pending: str | None = None

	for line in text.splitlines():
		if line.startswith("enum "):
			parts   = line.split()
			pending = parts[1]
			policy  = parts[-1].partition("=")[2]
			enums[pending] = (policy, [])
		elif pending is not None and line.startswith("  "):
			enums[pending] = (enums[pending][0], line.split())
			pending = None
	return enums


def _is_annotation(line: str) -> bool:
	"""Whether a line under a struct describes something other than a member.

	`": "` is the marker, and it is safe because a member line cannot contain
	one: its positions and widths spell a colon without a space after it
	(`@0x0004:3`, `until:0d0a`), and its facts are `key=value` tokens that
	`_squash` strips every space out of.
	"""
	return ": " in line


def _compare_struct(name: str, old: list[str], new: list[str]) -> list[Finding]:
	old_members = [line for line in old[1:] if not _is_annotation(line)]
	new_members = [line for line in new[1:] if not _is_annotation(line)]
	findings: list[Finding] = _compare_head(name, old[0], new[0])

	reordered = _reordering(name, old_members, new_members)
	if reordered is not None:
		return [*findings, reordered, *_compare_annotations(name, old, new)]

	for index in range(max(len(old_members), len(new_members))):
		was = old_members[index] if index < len(old_members) else None
		now = new_members[index] if index < len(new_members) else None
		findings.extend(_compare_member(name, index, was, now))

	findings.extend(_compare_annotations(name, old, new))
	return findings


def _compare_head(name: str, old: str, new: str) -> list[Finding]:
	"""The struct line, which carries `version=` and nothing else compared here.

	The extent is deliberately not: it is the sum of the member lines, and the
	member comparison names which one moved. Which member carries the version
	is not derivable from any of them.
	"""
	was = next((tok for tok in old.split() if tok.startswith("version=")), None)
	now = next((tok for tok in new.split() if tok.startswith("version=")), None)

	if was == now:
		return []

	# Gained or lost is not a break on its own, and saying so would be the
	# double count this file's headings cannot afford. A `[since]` member with
	# no version field is refused outright (19.4), so a struct that gained one
	# had no optional bytes for it to gate yet, and one that lost it has none
	# left -- and the members that went are reported as members.
	if was is None or now is None:
		return [Finding(
			"api", name,
			f"{was or 'no version field'} -> {now or 'no version field'}; no "
			"bytes move, and no member's presence depended on it either side")]

	return [Finding(
		"breaking", name,
		f"{was} -> {now}; an old receiver reads the version out of the other "
		"field and disagrees about which of the optional bytes are there")]


def _reordering(struct: str, old: list[str], new: list[str]) -> Finding | None:
	"""Two members of the same width trading places.

	Situ says position carries identity (section 4), and taken literally that
	makes this two renames: the member at offset 0 is a different member now,
	and it is called `beta`. Every byte is where it was, so there is nothing
	on the wire to distinguish the two.

	Which is exactly why it needs saying. The names did not change -- the same
	set is present before and after -- so what moved is the meaning attached
	to each position, and an old receiver reads `beta` where the sender put
	`alpha`. Reported as a rename it would have looked cosmetic, which is the
	most dangerous thing this file could do.
	"""
	old_names = [_name_of(line) for line in old]
	new_names = [_name_of(line) for line in new]

	if old_names == new_names or sorted(old_names) != sorted(new_names):
		return None		# not a permutation of the same members

	moved = [name for was, name in zip(old_names, new_names) if was != name]
	return Finding(
		"breaking", struct,
		f"{', '.join(sorted(moved))} changed places; every byte is where it "
		"was, so an old receiver reads each of them under the other's name")


def _compare_member(struct: str, index: int, was: str | None,
		now: str | None) -> list[Finding]:
	if was == now:
		return []

	where = f"{struct}[{index}]"

	if was is None:
		assert now is not None, "both cannot be absent; the loop bounds ensure it"

		# An appended member behind a `[since]` is the one case where
		# appending is provably safe rather than probably: the version field
		# tells an old receiver that these bytes are not for it, and its own
		# schema said as much before this edit existed. That is the whole
		# purpose of section 19.4, so the checker has to know it -- otherwise
		# the construct that makes extension safe still reports as a risk, and
		# a reviewer learns to wave the category through.
		since = next((fact for fact in _facts_of(now)
		              if fact.startswith("since=")), None)
		if since is not None:
			return [Finding("forward", where,
			                f"appended {_name_of(now)} at `{since}`; an old "
			                "receiver reads the version field and knows these "
			                "bytes are not its own")]

		# Without one, an old receiver sized its buffer from the old contract,
		# so it either ignores the extra bytes or rejects the message as
		# overlong -- and which is not situ's to know.
		return [Finding("forward", where,
		                f"appended {_name_of(now)}; an old receiver was not "
		                "written to expect these bytes and may reject the "
		                "message as overlong")]
	if now is None:
		return [Finding("breaking", where,
		                f"removed {_name_of(was)}; an old sender still emits "
		                "those bytes and everything after them shifts")]

	old_bits = was.split()
	new_bits = now.split()

	# Position, width and type are the contract. A rename at the same three is
	# an API change, which is the whole reason this file records names at all
	# -- and the type has to be one of the three. Compared on `[:2]` it was
	# not, so `u16 a` -> `i16 a` matched the rename arm and reported
	# "api-only ... renamed a -> a": the same bytes read as a different number,
	# announced in the half of the output that says nothing on the wire moved.
	if old_bits[:3] == new_bits[:3] and _facts_of(was) == _facts_of(now):
		return [Finding("api", where,
		                f"renamed {_name_of(was)} -> {_name_of(now)}; "
		                "the bytes are identical")]

	if old_bits[0] != new_bits[0]:
		return [Finding("breaking", where,
		                f"{_name_of(was)} moves {old_bits[0]} -> {new_bits[0]}; "
		                "an old receiver reads whatever now sits there")]
	if old_bits[1] != new_bits[1]:
		return [Finding("breaking", where,
		                f"{_name_of(was)} changes width "
		                f"{old_bits[1]} -> {new_bits[1]}; every later field "
		                "shifts for an old receiver")]

	findings: list[Finding] = []
	# A type change at one position and width is an interpretation change, in
	# the sense `INTERPRETATION` means: the bytes are where they were and are
	# a different value. Reported alongside the facts rather than instead of
	# them, because unlike a move or a widening it shifts nothing after it, so
	# the rest of the line is still worth reading.
	if old_bits[2] != new_bits[2]:
		findings.append(Finding(
			"breaking", where,
			f"{_name_of(now)} is {old_bits[2]} -> {new_bits[2]}; the same "
			"bytes now mean something else"))

	return findings + _compare_facts(where, _name_of(was),
	                                 _facts_of(was), _facts_of(now))


#: Facts that say what the bytes *are*, rather than which of them are allowed.
#:
#: The distinction decides everything. A changed `max` narrows the set of
#: messages both sides agree is valid, so old and new still read the same
#: number and one of them may refuse it. A changed byte order means the same
#: bytes are a different number, which no amount of agreement about ranges
#: helps with -- so these are breaking, and the constraint facts are not.
INTERPRETATION = (
	"big", "little", "native", "msb_first", "lsb_first",
	"radix=", "sized-by=", "varint=", "codec=", "encoding=",
	"nonce=", "key=", "pad-to=",
	"endian-from=", "quote=", "escape=", "trim", "fold-case",
	"nul_terminated",
	# Where the bytes are, where the run stops, and what the checksum field
	# is worth while the sum runs over it. None of the three narrows a set of
	# permitted values: each of them puts a different value in front of the
	# receiver, or a different number of bytes.
	"at=", "while=", "self_as=",
)


def _is_interpretation(fact: str) -> bool:
	return any(fact == key or fact.startswith(key) for key in INTERPRETATION)


def _compare_facts(where: str, name: str, was: set[str],
		now: set[str]) -> list[Finding]:
	"""A constraint added is a demand; one removed is a promise withdrawn.

	Which of those breaks depends on the direction, and conflating them is
	how "compatible" stops meaning anything. Tightening keeps old *senders*
	working and may make a new receiver reject them; loosening keeps new
	senders working and may make an old receiver reject them.

	Neither applies to a fact that changes what the bytes mean rather than
	which of them are permitted. Reporting a byte-order flip as a relaxed
	constraint said the same break twice in the reassuring half of the
	output.

	And neither applies to a constraint whose *value* moved, which is one
	finding and not two. Decomposed into a gain and a loss, `[must_eq = 1]` ->
	`[must_eq = 2]` printed "0 breaking, 2 compatible" under two headings that
	each asserted the direction the other denied -- and for `must_eq` both
	were wrong, there being no message the two sides both accept.
	"""
	findings = []
	moved    = {fact for fact in (now - was) | (was - now)
	            if _is_interpretation(fact)}

	if moved:
		return [Finding(
			"breaking", where,
			f"{name}: {' '.join(sorted(was & set(moved)) or ['nothing'])} -> "
			f"{' '.join(sorted(now & moved) or ['nothing'])}; the same bytes "
			"now mean something else")]

	# Paired by key, so that a fact stated before and after is one finding
	# about the value that moved rather than two about a token that came and
	# a token that went.
	dropped = {_key_of(fact): fact for fact in was - now}
	added   = {_key_of(fact): fact for fact in now - was}

	for key in sorted(set(dropped) & set(added)):
		findings.append(_changed_constraint(where, name, key,
		                                    dropped[key], added[key]))

	for key in sorted(set(added) - set(dropped)):
		findings.append(Finding(
			"backward", where,
			f"{name} gains `{added[key]}`; a new receiver may refuse a message "
			"an old sender legitimately produces"))
	for key in sorted(set(dropped) - set(added)):
		findings.append(Finding(
			"forward", where,
			f"{name} drops `{dropped[key]}`; an old receiver may refuse a "
			"message a new sender legitimately produces"))

	return findings


def _key_of(fact: str) -> str:
	"""The fact without its value. A flag is its own key."""
	return fact.partition("=")[0]


#: Constraints a changed value makes irreconcilable rather than merely
#: narrower or wider.
#:
#: `must_eq` states the one value the field may hold, so two builds that name
#: different ones share no message at all. `since` states the version the
#: bytes appear at, so moving it makes the same message a different length to
#: a peer at any version between the two.
IRRECONCILABLE = ("must_eq", "since")

#: Bounds, and which way each one tightens. `min` admits fewer values as it
#: rises; every other bound here admits fewer as it falls.
TIGHTENS_UP = ("min",)
TIGHTENS_DOWN = ("max", "cap", "while-max")


def _changed_constraint(where: str, name: str, key: str, was: str,
		now: str) -> Finding:
	"""One constraint whose value moved, classified by which way it moved.

	Where the direction can be read off the numbers it is, because that is the
	whole content of the backward/forward distinction. Where it cannot -- a
	bound written as an expression, a key with no ordering -- the finding says
	only what is known, and says it under `backward`, which is the heading that
	warns rather than the one that reassures.
	"""
	if key in IRRECONCILABLE:
		return Finding(
			"breaking", where,
			f"{name}: `{was}` -> `{now}`; no message satisfies both, so the "
			"two builds share nothing a peer can send")

	tighter = _tightened(key, was, now)
	if tighter is True:
		return Finding(
			"backward", where,
			f"{name}: `{was}` -> `{now}`, a tightening; a new receiver may "
			"refuse a message an old sender legitimately produces")
	if tighter is False:
		return Finding(
			"forward", where,
			f"{name}: `{was}` -> `{now}`, a loosening; an old receiver may "
			"refuse a message a new sender legitimately produces")

	return Finding(
		"backward", where,
		f"{name}: `{was}` -> `{now}`; the two builds admit different messages, "
		"and which of them refuses the other's is not read off the values")


def _tightened(key: str, was: str, now: str) -> bool | None:
	"""Whether the bound narrowed, or None where the values do not say.

	`[max = n * 2]` is a bound whose value is an expression, and two spellings
	of one are not ordered by anything this file can see.
	"""
	if key not in TIGHTENS_UP + TIGHTENS_DOWN:
		return None
	try:
		old_value = int(was.partition("=")[2], 0)
		new_value = int(now.partition("=")[2], 0)
	except ValueError:
		return None

	if old_value == new_value:
		return None
	return (new_value > old_value) if key in TIGHTENS_UP else (
		new_value < old_value)


def _name_of(line: str) -> str:
	parts = line.split()
	return parts[3] if len(parts) > 3 else "?"


def _facts_of(line: str) -> set[str]:
	return set(line.split()[4:])


def _compare_annotations(name: str, old: list[str],
		new: list[str]) -> list[Finding]:
	"""The lines that state a contract a member line has no room for.

	One dictionary keyed by everything left of the colon, so that an entry
	changed, gained or lost is named as itself: `params case 2`, `fields tlv
	size 1`, `checksum covers`. Which is the point of giving each entry a line
	-- folded into one, a variant with twenty-one arms reports that something
	about the arms differs.
	"""
	old_ann  = _annotations(old)
	new_ann  = _annotations(new)
	findings = []

	for key in sorted(set(old_ann) & set(new_ann)):
		if old_ann[key] != new_ann[key]:
			findings.append(_annotation_changed(name, key, old_ann[key],
			                                    new_ann[key]))
	for key in sorted(set(old_ann) - set(new_ann)):
		findings.append(_annotation_lost(name, key, old_ann[key]))
	for key in sorted(set(new_ann) - set(old_ann)):
		findings.append(_annotation_gained(name, key, new_ann[key]))
	return findings


def _annotations(lines: list[str]) -> dict[str, str]:
	return {line.split(": ", 1)[0]: line.split(": ", 1)[1]
	        for line in lines if _is_annotation(line)}


def _what(key: str) -> str:
	"""Which construct an annotation belongs to: the word after the subject."""
	parts = key.split()
	return parts[1] if len(parts) > 1 else ""


def _annotation_changed(name: str, key: str, was: str, now: str) -> Finding:
	if _what(key) == "covers":
		# Its own category because it is neither a parse break nor a cost:
		# both sides read the same values and the tag simply fails. A region
		# that shrank is worse than that -- the new build authenticates less
		# than the old one did, and `situc diff` called that an improvement.
		lost   = sorted(set(was.split()) - set(now.split()))
		detail = f"`{key}` {was} -> {now}"
		if lost:
			detail += f"; it no longer authenticates {', '.join(lost)}"
		return Finding("coverage", name, detail)

	if _what(key) == "switch":
		return Finding(
			"breaking", name,
			f"`{key}` {was} -> {now}; the arm is chosen by a different field, "
			"so the same discriminant selects a different layout")

	if _what(key) == "index":
		# The INTERPRETATION shape, and the reason this is breaking rather
		# than a narrowing either way: the table is exactly where it was and
		# holds the same numbers, and the bytes those numbers name are
		# somewhere else. An offset base that moves (0024) and an entry width
		# that changes are the same finding in that respect -- neither admits
		# or refuses a message, both hand the receiver different elements.
		return Finding(
			"breaking", name,
			f"`{key}` {was} -> {now}; the table is where it was and every "
			"element it reaches is somewhere else")

	return Finding(
		"breaking", name,
		f"`{key}` {was} -> {now}; the bytes are where they were and are read "
		"as something else")


def _annotation_lost(name: str, key: str, was: str) -> Finding:
	if _what(key) == "covers":
		# Coverage that is simply gone. A `coded` region losing its `covers`
		# changes no member and no offset (14.1a).
		return Finding("coverage", name,
		               f"`{key}` is gone; what it authenticated ({was}) is now "
		               "unauthenticated")

	return Finding(
		"breaking", name,
		f"`{key}` is gone; it said {was}, and an old sender still emits bytes "
		"this build has nothing to read them with")


def _annotation_gained(name: str, key: str, now: str) -> Finding:
	if _what(key) == "covers":
		# Absent while only tags reached this function, because a new tag
		# brings a new field and the member comparison already reports one. A
		# `coded` region gaining a `covers` clause changes no member and no
		# offset, so without this the two signatures compared equal while the
		# peers disagreed about which bytes to transform.
		return Finding("coverage", name,
		               f"`{key}` now {now}, which nothing covered before")

	# An arm, a tag or a value rule the old build had no case for. A new
	# sender may use it; an old receiver refuses or preserves it, on the
	# policy it was built with -- which is the backward direction exactly.
	return Finding(
		"backward", name,
		f"`{key}` now {now}, which nothing said before; a new sender may emit "
		"bytes an old receiver has no rule for")


def render_verdict(verdict: Verdict) -> str:
	"""Grouped by who it hurts, worst first, because that is the reading order."""
	if not verdict.findings:
		return "The wire contract is unchanged.\n"

	lines = []
	for kind in ("breaking", "coverage", "backward", "forward", "api"):
		found = [f for f in verdict.findings if f.kind == kind]
		if not found:
			continue
		heading, gloss = KINDS[kind]
		lines.extend(["", f"{heading}: {gloss}"])
		lines.extend(f"  {f.subject}: {f.detail}" for f in found)

	breaks = sum(1 for f in verdict.findings if f.is_break)
	lines.extend(["", f"{breaks} breaking, "
	              f"{len(verdict.findings) - breaks} compatible.", ""])
	return "\n".join(lines)
