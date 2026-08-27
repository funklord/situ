"""The suggestion catalog and its cost model (project.md section 18.2).

Section 18 calls the advisor the differentiator, and the reason is the cost
column rather than the suggestions: "move your variable-length fields to the
end" is folklore anyone can repeat, and "moving `opts` after `recs` costs 0
bytes and returns 4 members to absolute addressing" is advice. Every row here
therefore carries what it costs and what it buys, both computed from the
resolved schema rather than asserted.

Typical and worst case are reported separately because they diverge and because
an embedded designer sizes a static buffer off the worst one (18.2). Where a
suggestion is free -- reordering usually is -- that is a fact worth printing,
not a reason to leave the column blank.

Like the propagation table this is data: a row is a trigger and a costing, and
`suggest` is the only interpreter. Adding a suggestion means adding a row and a
test.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from situc import ast
from situc.capability import Axis
from situc.diagnostics import Span
from situc.layout import BITS_PER_BYTE, Placement
from situc.propagate import Resolved
from situc.traverse import own_entries
from situc.resolve import ResolvedSchema, ResolvedStruct


@dataclass(frozen=True)
class Cost:
	"""What a change costs on the wire, in bytes.

	Negative is a saving. `unknown` is for a change whose cost depends on a
	number the schema does not state -- pinning an unbounded region cannot be
	priced until somebody picks the bound -- and is reported as such rather
	than as zero, which would be a lie in the cheapest possible direction.
	"""

	typical: int = 0
	worst: int   = 0
	unknown: bool = False
	basis: str   = ""

	def render(self) -> str:
		"""The number, and what it is a number *of*.

		Every rule in the catalogue supplies a `basis` and none of them was
		ever printed, so eight different changes all read "cost: nothing" or
		"cost: N bytes" with no way to tell what was being counted. The basis
		is the half that keeps the number honest: "nothing" for a reordering
		means no bytes move, and says nothing about the peers already
		speaking the old order.
		"""
		if self.unknown:
			body = "depends on the bound chosen"
		elif self.typical == self.worst:
			body = _bytes(self.typical)
		else:
			body = (f"{_bytes(self.typical)} typical, "
			        f"{_bytes(self.worst)} worst case")

		return f"cost: {body}" if not self.basis else f"cost: {body} ({self.basis})"


def _bytes(count: int) -> str:
	if count == 0:
		return "nothing"
	if count < 0:
		return f"{_count(-count, 'byte')} saved"
	return _count(count, "byte")


def _count(many: int, noun: str) -> str:
	"""`1 byte`, `2 bytes`. The advisor is read by a person deciding whether to
	change a schema, and "1 bytes saved" is the register of a program that was
	not read after it was written."""
	return f"{many} {noun}" if many == 1 else f"{many} {noun}s"


@dataclass(frozen=True)
class Suggestion:
	rule: str
	subject: str
	span: Span
	summary: str
	detail: str
	cost: Cost
	yields: str
	# Lower sorts first. The catalog's own order, which section 18.2 gives by
	# yield: the reordering row is called the highest-yield single change.
	rank: int = 0
	# How much the change recovers, for ranking within a rule.
	weight: int = 0

	def render(self) -> list[str]:
		return [
			f"{self.subject}: {self.summary}",
			f"    {self.detail}",
			f"    {self.cost.render()}",
			f"    yields: {self.yields}",
		]


@dataclass(frozen=True)
class Rule:
	name: str
	rank: int
	find: Callable[[ResolvedSchema], list[Suggestion]]


def suggest(resolved: ResolvedSchema) -> list[Suggestion]:
	"""Run the catalog and rank what it finds.

	Ranked by the catalog's order first and by how much each instance recovers
	second, so the highest-yield change in the highest-yield class is the one a
	reader sees at the top.
	"""
	found: list[Suggestion] = []
	for rule in CATALOG:
		found.extend(rule.find(resolved))

	return sorted(found, key=lambda item: (item.rank, -item.weight, item.subject))


def render(suggestions: list[Suggestion]) -> str:
	if not suggestions:
		return ("No suggestions: every construct in this schema is already at "
		        "the strongest form the advisor knows how to reach.\n")

	lines = [f"{len(suggestions)} suggestion(s), highest yield first.", ""]
	for index, suggestion in enumerate(suggestions):
		if index:
			lines.append("")
		lines.extend(suggestion.render())

	return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Shared reading of the resolved schema
# ---------------------------------------------------------------------------


def _members(struct: ResolvedStruct) -> list[Resolved]:
	"""A struct's own members, in layout order.

	The same partition the C backend uses: nested paths belong to their own
	struct, an element entry describes a whole array at once, and an
	`authenticated` region names bytes its members already account for.
	"""
	return own_entries(struct)


def _is_dynamic_size(placement: Placement) -> bool:
	return placement.size_max_bits != placement.size_bits


def _worst_bytes(placement: Placement) -> int | None:
	if placement.size_max_bits is None:
		return None
	return placement.size_max_bits // BITS_PER_BYTE


# ---------------------------------------------------------------------------
# The catalog
# ---------------------------------------------------------------------------


def _find_tail_reordering(resolved: ResolvedSchema) -> list[Suggestion]:
	"""A variable-length member with fixed members behind it.

	The highest-yield single change in the catalog, and the cheapest: moving a
	member changes no byte count at all, and everything that was behind it
	returns to absolute addressing. What it does cost is wire compatibility,
	which is why that is said rather than left implied.
	"""
	found = []

	for struct in resolved.structs.values():
		members = _members(struct)

		for index, entry in enumerate(members):
			if not _is_dynamic_size(entry.placement):
				continue

			# A tag is not a candidate for moving forward: it authenticates
			# what precedes it, so it has to follow. Suggesting otherwise
			# would be advice that cannot be taken.
			#
			# Nor is a versioned member. `[since]` is append-only (19.4), so
			# moving this member past one would be refused by the compiler --
			# and if it were not, it would move bytes for every deployed peer
			# that speaks the earlier version. The advisor said "cost:
			# nothing" for exactly that.
			# And this member cannot be moved past one either, whatever else
			# follows it: a versioned member is append-only, so it is already
			# at the end and nothing may go after it.
			if any(later.placement.since is not None
			       for later in members[index + 1:]):
				continue

			behind, destination = _reordering_gain(members, index)
			if not behind:
				continue

			listed = ", ".join(f"`{later.placement.name}`" for later in behind[:4])
			if len(behind) > 4:
				listed += f", and {len(behind) - 4} more"

			found.append(Suggestion(
				rule    = "move-dynamic-to-tail",
				subject = entry.placement.path,
				span    = entry.placement.span,
				summary = (f"move this variable-length member {destination}"),
				detail  = (f"its extent is not fixed, so "
				           f"{_count(len(behind), 'member')} behind it are "
				           f"Dynamic: {listed}"),
				cost    = Cost(basis="reordering moves no bytes, and every "
				               "deployed peer reads the old order"),
				yields  = (f"{_count(len(behind), 'member')} return to "
				           "AbsoluteStatic, and their accessors to base + K"),
				rank    = 0,
				weight  = len(behind),
			))

	return found


def _reordering_gain(members: list[Resolved],
		index: int) -> tuple[list[Resolved], str]:
	"""Which members actually gain from moving this one back, and how far.

	Both halves were wrong, and in the same direction: the rule counted
	*every* dynamic member behind it and told the author to move it to the
	end.

	It cannot always go to the end. A `[remaining]` member has to be last
	(8.5) and a tag has to follow what it authenticates, so the destination is
	before whichever of those comes first -- and taken literally, the old
	advice produced a schema the compiler refuses, which is the same defect
	26.36 found for `[since]` and did not generalise.

	And moving it does not make everything behind it static. Only the members
	up to and including the *next* variable-length one gain: whatever follows
	that one is placed after a variable extent either way. `example/message`
	is the case -- the advisor promised two and delivered one.
	"""
	# Two separate questions, and answering them in one pass conflated them:
	# how far this member may go, and which members gain when it does.
	stopper = next((later for later in members[index + 1:]
	                if later.placement.sized_by == "remaining"
	                or later.placement.kind in ("tag", "checksum")), None)

	gainers: list[Resolved] = []
	for later in members[index + 1:]:
		if later is stopper:
			break
		if later.vector.get(Axis.OFFSET).base == "Dynamic":
			gainers.append(later)
		if _is_dynamic_size(later.placement):
			break		# whatever follows this one is dynamic either way

	if stopper is None:
		return gainers, "after the fixed ones"
	return gainers, f"after the fixed ones, before `{stopper.placement.name}`"


def _find_varint_replacement(resolved: ResolvedSchema) -> list[Suggestion]:
	"""A varint whose range is bounded tightly enough to be worth pinning.

	Costed honestly in both directions: across most of a `max = 1500` field's
	range a varint already spends two bytes, so `u16` is free typically and
	saves a byte in the worst case -- while restoring fixed offsets for
	everything behind it.
	"""
	found = []

	for struct in resolved.structs.values():
		for entry in _members(struct):
			placement = entry.placement
			if placement.varint is None:
				continue

			worst = _worst_bytes(placement)
			least = placement.size_bits // BITS_PER_BYTE
			if worst is None:
				continue

			fixed = _fixed_width_for(worst)
			found.append(Suggestion(
				rule    = "varint-to-fixed",
				subject = placement.path,
				span    = placement.span,
				summary = f"replace the varint with a fixed `u{fixed * 8}`",
				detail  = (f"the encoding spends {least} to {worst} bytes, and a "
				           f"u{fixed * 8} spends {fixed} always"),
				cost    = Cost(typical=fixed - worst, worst=fixed - worst,
				               basis="fixed width against the varint's worst case"),
				yields  = ("a fixed extent, so every member behind it keeps its "
				           "static offset"),
				rank    = 1,
				weight  = worst,
			))

	return found


def _fixed_width_for(worst_bytes: int) -> int:
	for width in (1, 2, 4, 8):
		if worst_bytes <= width:
			return width
	return 8


def _find_unbounded_regions(resolved: ResolvedSchema) -> list[Suggestion]:
	"""Nothing bounds this, so nothing can allocate for it."""
	found = []

	for struct in resolved.structs.values():
		for entry in _members(struct):
			placement = entry.placement
			if placement.size_max_bits is not None:
				continue

			# The remedy has to be something the compiler implements, which
			# `[size = N]` is not: it is in the parser's closed vocabulary and
			# read by nothing, so a reader who took this advice got a schema
			# that compiled, changed nothing, and produced the same suggestion
			# again on the next run. 26.36 is the same defect and was fixed
			# there for one construct rather than as a rule.
			#
			# What reaches this branch with no driver is an unbounded scan,
			# and what bounds a scan is `max N` -- the cap in the `until`
			# clause that `example/smtp` has been carrying since it was
			# written.
			driver = placement.sized_by
			if driver == "remaining":
				where = ("bound the enclosing frame, or size this from a length "
				         "field instead of `remaining`")
			elif driver:
				where = f"give `{driver}` a `[max = N]`"
			elif placement.delimiter is not None:
				where = ("cap the scan with `max N` after the `until` clause, "
				         "so a frame that never terminates stops somewhere")
			else:
				where = "give this member a bound the compiler can read"

			found.append(Suggestion(
				rule    = "bound-unbounded",
				subject = placement.path,
				span    = placement.span,
				summary = "put an upper bound on this region",
				detail  = (f"its size is Unbounded, so no caller can size a buffer "
				           f"for the struct that holds it; {where}"),
				cost    = Cost(unknown=True,
				               basis="the bound is the cost, and it is unchosen"),
				yields  = "a worst-case extent, so the struct becomes statically "
				          "allocatable",
				rank    = 2,
				weight  = 1,
			))

	return found


def _find_variant_equalization(resolved: ResolvedSchema) -> list[Suggestion]:
	"""Pad the arms to the largest and the members behind stop moving."""
	found = []

	for struct in resolved.structs.values():
		for entry in _members(struct):
			placement = entry.placement
			sizes     = placement.arm_sizes
			if len({size for _, size in sizes}) <= 1:
				continue

			largest = max(size for _, size in sizes)
			worst   = max(largest - size for _, size in sizes) // BITS_PER_BYTE
			average = (sum(largest - size for _, size in sizes)
			           // (len(sizes) * BITS_PER_BYTE))

			behind, encloses = _behind(struct, placement)

			found.append(Suggestion(
				rule    = "equalize-variant-arms",
				subject = placement.path,
				span    = placement.span,
				summary = "pad every arm to the size of the largest",
				detail  = ("the arms differ, so the extent depends on the "
				           f"discriminant: {_arms(sizes)}"),
				cost    = Cost(typical=average, worst=worst,
				               basis="padding to the largest arm"),
				yields  = _equalized_yield(struct, behind, encloses),
				rank    = 3,
				# What it buys, not what it costs. `weight` orders the
				# catalogue and this rule handed it the padding, so the most
				# expensive equalization in a schema sorted above the
				# cheapest -- and `example/netlink`, whose `default: opaque`
				# arm prices at four gigabytes, sorted above every genuinely
				# useful suggestion in the file.
				weight  = behind + (1 if encloses else 0),
			))

	return found


def _arms(sizes: tuple[tuple[str, int], ...]) -> str:
	return ", ".join(f"`{name}` {size // BITS_PER_BYTE}"
	                 for name, size in sizes) + " bytes"


def _behind(struct: ResolvedStruct,
		placement: Placement) -> tuple[int, bool]:
	"""How many of the struct's own members follow this one, and whether
	pinning this one's extent pins the whole struct's.

	The second is not implied by the first. A variant that is the last member
	has nothing behind it and equalizing it still buys something -- it is what
	makes `example/dnsname`'s `label` a fixed 64 bytes -- but only when every
	other member of the struct is already fixed. Measured rather than promised.
	"""
	members = [entry.placement for entry in own_entries(struct)]
	index   = next((i for i, held in enumerate(members)
	                if held.path == placement.path), None)
	if index is None:
		return 0, False

	others   = members[:index] + members[index + 1:]
	encloses = all(held.size_max_bits == held.size_bits
	               and held.size_bits is not None for held in others)
	return len(members) - index - 1, encloses


def _equalized_yield(struct: ResolvedStruct, behind: int,
		encloses: bool) -> str:
	"""What the padding actually returns, in the order it is worth having."""
	gains = []
	if behind:
		gains.append(f"{_count(behind, 'member')} after the variant keep "
		             "absolute offsets")
	if encloses:
		gains.append(f"`{struct.name}` itself becomes a fixed size")
	if not gains:
		return "a fixed extent for the variant, and nothing else in this struct"

	return "a fixed extent, so " + ", and ".join(gains)


def _find_alignment_holes(resolved: ResolvedSchema) -> list[Suggestion]:
	"""An unaligned member with padding already in the struct.

	Free when it is possible at all: the bytes are already being spent, and
	reordering only decides which member sits on the boundary.
	"""
	found = []

	for struct in resolved.structs.values():
		members = _members(struct)
		padding = sum(entry.placement.size_bits for entry in members
		              if entry.placement.kind == "reserved") // BITS_PER_BYTE
		if not padding:
			continue

		for entry in members:
			placement = entry.placement
			scalar    = placement.scalar
			# Triggered off the weakening the lattice already recorded rather
			# than by recomputing the alignment: `Aligned(1)` is a weakened
			# align axis even though its base is not `Unaligned`, and the rule
			# that noticed is the one that knows.
			misaligned = any(weakening.rule.name == "unaligned-multi-byte-scalar"
			                 for weakening in entry.weakenings)
			if scalar is None or scalar.is_bit_packed or not misaligned:
				continue
			if placement.kind != "field" or placement.offset_bits is None:
				continue

			# ...and the padding has to be enough to reach the boundary. The
			# rule fired on "some padding exists somewhere", which for a
			# `u32` one byte into a struct with one reserved byte suggests a
			# reordering that lands it at two -- still unaligned, and there
			# is nothing else to move. Invariant 64: this is advice checked
			# by taking it.
			width = scalar.bits // BITS_PER_BYTE
			at    = placement.offset_bits // BITS_PER_BYTE
			if (width - at % width) % width > padding:
				continue

			# What alignment buys depends on whether the value is the
			# memory. A `ValueConverted` field -- a big-endian scalar read on
			# a little-endian host, say -- cannot be pointed at, and every
			# access is a read-swap-write whichever boundary it sits on, so
			# the offset decides much less than it appears to. Saying so is
			# the same fix as the recomputation price above: the suggestion
			# is not wrong, its condition was invisible, and a reader who
			# does not know it pays reserved bytes for a gain the repr column
			# already ruled out (suggestion/fuzznet.md).
			#
			# `repr` alone cannot say it: an unaligned scalar is
			# ValueConverted *because* it is unaligned, so keying on that
			# would attach the caveat to every finding this rule makes,
			# including the little-endian ones where the reorder does buy
			# the access. Measured before the fix -- it fired on both. What
			# distinguishes the case is the declared byte order.
			swapped = (scalar.bits > BITS_PER_BYTE
			           and placement.endian is ast.Endian.BIG)
			bought = ("an aligned access, which faults on some targets and "
			          "is split on others when it is not")
			if swapped:
				bought += (" -- though on a little-endian host this "
				           "big-endian field is read through a swap whatever "
				           "the offset, so the value is never the memory and "
				           "alignment buys much less than it appears to; "
				           "check the `repr` column before spending bytes "
				           "on it")

			found.append(Suggestion(
				rule    = "fill-alignment-holes",
				subject = placement.path,
				span    = placement.span,
				summary = "reorder to put this on its natural boundary",
				detail  = (f"it is a {scalar.bits}-bit scalar at an unaligned "
				           f"offset, and the struct already spends "
				           f"{_bytes(padding)} on reserved padding"),
				cost    = Cost(basis="the padding bytes are already spent, and "
				               "every deployed peer reads the old order"),
				yields  = bought,
				rank    = 4,
				weight  = scalar.bits // BITS_PER_BYTE,
			))

	return found


def _find_scattered_coverage(resolved: ResolvedSchema) -> list[Suggestion]:
	"""Regions under one tag that are not contiguous.

	Section 14 pays for coverage per range. Two ranges under one tag mean the
	algorithm runs twice, and the generated code cannot even hand out a single
	covered span for it.
	"""
	found = []

	for struct in resolved.structs.values():
		# Region placements, which `_members` leaves out: an `authenticated`
		# block names bytes its members already account for, so it is not part
		# of the layout partition -- but it is exactly what coverage names.
		regions = [entry for entry in struct.entries
		           if entry.placement.kind in ("authenticated", "sealed")]

		for entry in _members(struct):
			placement = entry.placement
			if placement.kind not in ("tag", "checksum") or len(placement.tag_covers) < 2:
				continue

			covered = [held for held in regions
			           if held.placement.name in placement.tag_covers]
			if len(covered) < 2 or _is_contiguous(covered):
				continue

			found.append(Suggestion(
				rule    = "group-covered-regions",
				subject = placement.path,
				span    = placement.span,
				summary = "make the regions this covers contiguous",
				detail  = (f"it covers {len(placement.tag_covers)} regions with "
				           "other members between them, so the coverage is not "
				           "one range"),
				cost    = Cost(basis="moving members, not adding them"),
				yields  = "one range to authenticate instead of several, and a "
				          "covered-span accessor the backend can emit",
				rank    = 5,
				weight  = len(placement.tag_covers),
			))

	return found


def _is_contiguous(entries: list[Resolved]) -> bool:
	ordered = sorted(entries, key=lambda entry: entry.placement.offset_bits or 0)
	for earlier, later in zip(ordered, ordered[1:]):
		if (earlier.placement.offset_bits is None
				or later.placement.offset_bits is None):
			return False
		if earlier.placement.offset_bits + earlier.placement.size_bits \
				!= later.placement.offset_bits:
			return False
	return True


def _find_mutable_under_coverage(resolved: ResolvedSchema) -> list[Suggestion]:
	"""Fields that would be freely writable if a tag did not cover them.

	Section 14.2's design pressure, stated as advice: a hop counter under a tag
	costs an authentication per hop, and moving it out costs nothing but the
	ordering.

	One suggestion per tag rather than one per field, because the trigger
	section 18.2 gives is *frequent* mutation and the schema does not say which
	fields those are. What the compiler can supply is the price -- how many
	bytes each write re-authenticates, and which fields would be candidates --
	and leave the choice of which are hot to the person who knows.

	A field inside a sealed region is not a candidate: moving it out means
	taking it out of the seal, which is a different decision entirely.
	"""
	found = []

	for struct in resolved.structs.values():
		for tag in _members(struct):
			if tag.placement.kind not in ("tag", "checksum"):
				continue

			candidates = [entry for entry in struct.entries
			              if tag.placement.name in entry.placement.covered_by
			              and entry.placement.kind == "field"
			              and entry.placement.scalar is not None
			              and entry.placement.sealed_by is None
			              and entry.vector.get(Axis.MUTATE).base == "InPlaceFixed"]
			if not candidates:
				continue

			extent = _covered_bytes(struct, tag.placement)
			listed = ", ".join(f"`{entry.placement.name}`" for entry in candidates[:5])
			if len(candidates) > 5:
				listed += f", and {len(candidates) - 5} more"

			found.append(Suggestion(
				rule    = "uncover-mutable-field",
				subject = tag.placement.path,
				span    = tag.placement.span,
				summary = "move the fields you rewrite often out of this coverage",
				# The cost is per *write*, which assumes the frame is
				# mutated after it is built. A frame assembled once and sent
				# writes every covered field before the tag exists and pays
				# one recomputation between them, not one each -- so this
				# suggestion is conditional, and the condition was invisible
				# until a consumer read it as unconditional and said so
				# (suggestion/fuzznet.md). A ranked, costed suggestion whose
				# cost model does not match the usage is the shape of a gate
				# that cannot model what it checks: the reader who knows
				# enough ignores it, and the reader who does not obeys it.
				detail  = (f"{len(candidates)} covered field(s) are writable in "
				           f"place -- {listed} -- and each write costs a "
				           f"recomputation over {extent} bytes, where the "
				           f"frame is rewritten after it is built. A frame "
				           f"built once and sent pays one recomputation "
				           f"between them, and this suggestion is worth "
				           f"little to it"),
				cost    = Cost(basis="moving a member, not adding one"),
				yields  = ("writes that invalidate no tag, which is why real "
				           "protocols keep routing and hop fields outside "
				           "coverage"),
				rank    = 6,
				weight  = extent * len(candidates),
			))

	return found


def _covered_bytes(struct: ResolvedStruct, tag: Placement) -> int:
	"""Worst-case extent a tag authenticates, which is what a write recomputes."""
	total = 0
	for entry in struct.entries:
		placement = entry.placement
		if placement.name in tag.tag_covers and placement.kind in ("authenticated",
		                                                           "sealed"):
			total += (placement.size_max_bits or placement.size_bits) // BITS_PER_BYTE
	return total


def _find_tlv_to_positional(resolved: ResolvedSchema) -> list[Suggestion]:
	"""A TLV region: O(n) lookup, and no addressing at all."""
	found = []

	for struct in resolved.structs.values():
		for entry in _members(struct):
			placement = entry.placement
			if placement.kind != "tlv":
				continue

			found.append(Suggestion(
				rule    = "tlv-to-positional",
				subject = placement.path,
				span    = placement.span,
				summary = "lift the tags you always send into positional fields",
				detail  = ("every item is found by walking from the start, so "
				           "lookup is O(n) and nothing inside has an address"),
				cost    = Cost(basis="the bytes of the fields lifted out, which "
				               "the region was spending on tags and lengths "
				               "anyway"),
				yields  = "O(1) access and static offsets for the tags that are "
				          "always present; the rest can stay in the region",
				rank    = 7,
				weight  = 1,
			))

	return found


CATALOG: tuple[Rule, ...] = (
	Rule("move-dynamic-to-tail",	0, _find_tail_reordering),
	Rule("varint-to-fixed",		1, _find_varint_replacement),
	Rule("bound-unbounded",		2, _find_unbounded_regions),
	Rule("equalize-variant-arms",	3, _find_variant_equalization),
	Rule("fill-alignment-holes",	4, _find_alignment_holes),
	Rule("group-covered-regions",	5, _find_scattered_coverage),
	Rule("uncover-mutable-field",	6, _find_mutable_under_coverage),
	Rule("tlv-to-positional",	7, _find_tlv_to_positional),
)


def to_dict(suggestion: Suggestion) -> dict[str, object]:
	"""Machine-readable form, so CI can gate on the catalog."""
	return {
		"rule":    suggestion.rule,
		"subject": suggestion.subject,
		"summary": suggestion.summary,
		"detail":  suggestion.detail,
		"yields":  suggestion.yields,
		"cost": {
			"typical": None if suggestion.cost.unknown else suggestion.cost.typical,
			"worst":   None if suggestion.cost.unknown else suggestion.cost.worst,
			"basis":   suggestion.cost.basis,
		},
		"line": suggestion.span.line,
	}
