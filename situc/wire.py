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
from situc.layout import BITS_PER_BYTE, Placement
from situc.resolve import ResolvedSchema, ResolvedStruct
from situc.traverse import own_members

FORMAT_VERSION = 0


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
	]

	lines.extend(_directives(schema))
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
		values = resolved.layout.env.enums[decl.name]
		listed = " ".join(f"{member.name}={values[member.name]}"
		                  for member in decl.members)
		lines.extend([
			"",
			f"enum {decl.name} : {decl.backing.name} "
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

	lines = [f"struct {struct.name} {extent}"]
	lines.extend(f"  {_member(placement)}" for placement in own_members(struct))
	lines.extend(_coverage(struct))
	return lines


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


#: Attributes a peer can observe in the bytes. `[secret]` and the register
#: access modes are deliberately absent: they change the generated API and
#: nothing a receiver could detect.
WIRE_ATTRS = (
	"must_eq", "min", "max", "must_be_zero", "must_be_one",
	"encoding", "nul_terminated",
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

	findings.extend(_compare_enums(before, after))
	return findings


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


def _compare_struct(name: str, old: list[str], new: list[str]) -> list[Finding]:
	old_members = [line for line in old[1:] if " covers: " not in line]
	new_members = [line for line in new[1:] if " covers: " not in line]
	findings: list[Finding] = []

	reordered = _reordering(name, old_members, new_members)
	if reordered is not None:
		return [reordered, *_compare_coverage(name, old, new)]

	for index in range(max(len(old_members), len(new_members))):
		was = old_members[index] if index < len(old_members) else None
		now = new_members[index] if index < len(new_members) else None
		findings.extend(_compare_member(name, index, was, now))

	findings.extend(_compare_coverage(name, old, new))
	return findings


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

	# Position and width are the contract. A rename at the same position and
	# width is an API change, which is the whole reason this file records
	# names at all.
	if old_bits[:2] == new_bits[:2] and _facts_of(was) == _facts_of(now):
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

	return _compare_facts(where, _name_of(was), _facts_of(was), _facts_of(now))


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

	for fact in sorted(now - was):
		findings.append(Finding(
			"backward", where,
			f"{name} gains `{fact}`; a new receiver may refuse a message an "
			"old sender legitimately produces"))
	for fact in sorted(was - now):
		findings.append(Finding(
			"forward", where,
			f"{name} drops `{fact}`; an old receiver may refuse a message a "
			"new sender legitimately produces"))

	return findings


def _name_of(line: str) -> str:
	parts = line.split()
	return parts[3] if len(parts) > 3 else "?"


def _facts_of(line: str) -> set[str]:
	return set(line.split()[4:])


def _compare_coverage(name: str, old: list[str], new: list[str]) -> list[Finding]:
	"""What each tag authenticates.

	Its own category because it is neither a parse break nor a cost: both
	sides read the same values and the tag simply fails. A region that shrank
	is worse than that -- the new build authenticates less than the old one
	did, and `situc diff` called exactly that an improvement.
	"""
	old_cover = {line.split(" covers: ")[0]: line.split(" covers: ")[1]
	             for line in old if " covers: " in line}
	new_cover = {line.split(" covers: ")[0]: line.split(" covers: ")[1]
	             for line in new if " covers: " in line}
	findings = []

	for tag in sorted(set(old_cover) & set(new_cover)):
		if old_cover[tag] == new_cover[tag]:
			continue
		was  = set(old_cover[tag].split())
		now  = set(new_cover[tag].split())
		lost = sorted(was - now)
		detail = f"`{tag}` covers {old_cover[tag]} -> {new_cover[tag]}"
		if lost:
			detail += f"; it no longer authenticates {', '.join(lost)}"
		findings.append(Finding("coverage", name, detail))

	for tag in sorted(set(old_cover) - set(new_cover)):
		findings.append(Finding("coverage", name,
		                        f"`{tag}` is gone; what it authenticated is "
		                        "now unauthenticated"))

	# Coverage that was not there before. Absent while only tags reached this
	# function, because a new tag brings a new field and the member comparison
	# already reports one. A `coded` region gaining a `covers` clause changes
	# no member and no offset (14.1a), so without this the two signatures
	# compared equal while the peers disagreed about which bytes to transform.
	for tag in sorted(set(new_cover) - set(old_cover)):
		findings.append(Finding("coverage", name,
		                        f"`{tag}` now covers {new_cover[tag]}, which "
		                        "nothing covered before"))
	return findings


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
