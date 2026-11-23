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
import re
from pathlib import PurePath

from situc import ast, chars
from situc.types import pinned_shown
from situc.layout import (
	Arm, BITS_PER_BYTE, IndexTable, KnownTag, Placement, ValueRule)
from situc.resolve import ResolvedSchema, ResolvedStruct
from situc.invariant import paths_in
from situc.diagnostics import SituError
from situc.expr import Env, evaluate
from situc.traverse import holds_of, own_members
from situc.unparse import expr_to_source, kernel_to_source
from situc.timespec import spec_of

#: Bumped from 0, which 0041 kept because new facts on existing lines are
#: what the fact list is for and a comparator ignores tokens it does not know.
#: v1 adds new *kinds* of line -- the varint block, and the `name what: ...`
#: lines a variant and a tlv region state their contract on -- and a v0
#: comparator reading one of those counts it as a member, which slides every
#: member after it by one position and reports the slide as a break. A reader
#: that cannot tell the two apart has to be told, so the number moves.
#:
#: **v2 renames rather than adds.** 26.520 qualified a tag and its covered
#: members by path in the `covers:` lines, so every token on those lines
#: changed and nothing about what is covered did. fuzznet's consuming tree
#: read the result as *no longer authenticates capability, chunks, ...* --
#: the misreport this number exists to prevent, by the rule above, and
#: 26.520 did not move it.
#:
#: **Until 26.521 nothing read this field.** It was rendered into every
#: signature's first line and `compare` never looked at it: written and
#: read by nothing, which is a vacuous pass wearing a format's clothes.
#: Bumping it while that was true would have been a second inert value.
#:
#: **v3 adds a kind of line again**, the kernel block, for v1's reason.
FORMAT_VERSION = 3


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
	lines.extend(_kernels(schema, resolved))
	lines.extend(_enums(schema, resolved))

	for name in sorted(resolved.structs):
		lines.append("")
		lines.extend(_struct(resolved.structs[name], resolved.layout.env))
		lines.extend(_timed_lets(schema, name, resolved.layout.env))
		lines.extend(_refusals(schema, name))

	lines.extend(_registries(schema))

	return "\n".join(lines) + "\n"


#: A registry entry's attributes that say nothing a peer relies on: how a
#: client labels a value is not which values it may send.
PRESENTATIONAL = ("unit",)


def _spelled(text: str) -> str:
	"""A string with no space, quote or backslash in it, so a fact stays one
	whitespace-separated token: `\\xNN` for anything outside the printable
	ASCII the class syntax would also write bare."""
	return "".join(one if 0x21 <= ord(one) <= 0x7E and one not in '"\\'
	               else f"\\x{ord(one):02X}" for one in text)


def _registries(schema: ast.Schema) -> list[str]:
	"""Each registry's keyspace, one line per key (0070, 26.1106).

	A registry is a contract between peers as a struct is -- which keys
	exist, what each cell is a message of, its bounds, the value a peer
	assumes where the cell is absent, which way it is safe to move -- and
	none of it was in this file, so `wire --check` passed a key removed or
	a bound narrowed. The summary and a unit are how a client shows the
	cell, not what may be sent, and are left out so that rewording one is
	not a contract change.
	"""
	lines: list[str] = []
	for decl in sorted(schema.registries(), key=lambda one: one.name):
		lines += ["", f"registry {decl.name}"]
		for entry in sorted(decl.entries, key=lambda one: one.key):
			facts = [f'key="{_spelled(entry.key)}"', f"cell={entry.type_name}"]
			if entry.family:
				facts.append("family")
			for attr in sorted(entry.attrs, key=lambda one: one.name):
				if attr.name in PRESENTATIONAL:
					continue
				value = attr.value
				if isinstance(value, ast.StringLiteral):
					facts.append(f'{attr.name}="{_spelled(value.value)}"')
				elif isinstance(value, ast.IntLiteral):
					facts.append(f"{attr.name}={value.value}")
				elif value is None:
					facts.append(attr.name)
				else:
					facts.append(f"{attr.name}="
					              f"{_spelled(expr_to_source(value))}")
			lines.append("  " + " ".join(facts))
	return lines


def _timed_lets(schema: ast.Schema, struct: str, env: Env) -> list[str]:
	"""A named value that says what time it holds (26.1165): the time is
	what a peer reads it as, so it is contract as a member's is. A value
	saying nothing about time adds no line, so 26.1056's values leave
	every existing signature as it was."""
	decl = next((one for one in schema.structs() if one.name == struct), None)
	lines: list[str] = []
	for let in decl.lets if decl is not None else ():
		time = spec_of(let.attrs, env)
		if time is not None:
			lines.append(f"  let {let.name} {time.render()}")
	return lines


def _refusals(schema: ast.Schema, struct: str) -> list[str]:
	"""The `when`s that make a message illegal, and only those (0051).

	A `refuse` changes which messages are legal, so two peers that disagree
	about one disagree about the bytes -- which is 0048's rule for what a
	wire signature is for. A `warn` or a `note` changes nothing about
	legality, and naming one here would churn the signature on an editorial
	change to a sentence.

	The NAME and the predicate, not the text. The text is a default
	rendering that a consumer may replace; the name is the contract, and
	the predicate is what a peer has to agree with.
	"""
	held = sorted((when for when in schema.whens()
	               if when.severity is ast.Severity.REFUSE
	               and any(path.partition(".")[0] == struct
	                       for path in paths_in(when.expr))),
	              key=lambda one: one.name)

	return [f"  {struct} refuses: {one.name} when {expr_to_source(one.expr)}"
	        for one in held]


def _directives(schema: ast.Schema) -> list[str]:
	"""The schema-wide facts, which are wire facts before they are anything.

	Byte order first, because it is the change that alters every byte in every
	message while leaving the structure identical -- the one a structural diff
	is guaranteed to miss.
	"""
	found = []
	for decl in schema.decls:
		# This file's, not an imported one's. A signature that listed both
		# said the schema was big AND little endian, and both `buffer` and
		# `file` -- a committed contract naming two answers to one
		# question, which is no contract at all.
		if decl.span.source.path != schema.root:
			continue
		if isinstance(decl, ast.EndianDirective):
			found.append(f"endian {decl.endian.value}")
		elif isinstance(decl, ast.BitOrderDirective):
			found.append(f"bit_order {decl.bit_order.value}")
		elif isinstance(decl, ast.TargetDirective):
			found.append(f"target {decl.kind.value}")

	return ["", *sorted(found)] if found else []


def _kernels(schema: ast.Schema, resolved: ResolvedSchema) -> list[str]:
	"""What each codec the message reaches computes.

	`varint`'s reason: the parameters ARE the bytes. `init = 0xFFFF` becoming
	`init = 0` makes every checksum a peer sends fail, and the member line said
	nothing at all, so `wire --check` stayed green over a change no two
	implementations could survive (26.891). Only what a member names: a schema
	importing `std/kernels.situ` is not bound by forty codecs it never calls.
	"""
	used = {name for struct in resolved.structs.values()
	        for entry in struct.entries
	        for name in (entry.placement.codec, entry.placement.tag_codec)}
	lines: list[str] = []
	for decl in schema.codecs():
		if decl.kernel is not None and decl.name in used:
			lines.extend(["", f"kernel {decl.name} : "
			              f"{kernel_to_source(decl.kernel)}"])
	return lines


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
		# Where the number starts, which moves every value read.
		if decl.prefix is not None:
			facts.append(f"prefix={decl.prefix}")
		# Whether one number has one spelling. Without it a receiver must
		# accept a padded encoding of a value it also sees unpadded, which is
		# a fact about the bytes and not about the values they carry.
		if decl.minimal:
			facts.append("minimal")
		if decl.transform is not None \
				and decl.encoding is not ast.VarintEncoding.SLEB128:
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


def _struct(struct: ResolvedStruct, env: Env) -> list[str]:
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
	lines.extend(f"  {'parameter'.ljust(9)} {'-'.ljust(9)} "
	             f"{f'u8[{held.size_bytes}]'.ljust(10)} {held.name}"
	             for held in layout.material)
	lines.extend(f"  {_member(placement, env)}"
	             for placement in own_members(struct))
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


def _member(placement: Placement, env: Env) -> str:
	"""One member's contract, as one line so a diff points at one thing."""
	# A parameter is an argument the caller supplies rather than bytes
	# (0050), and it is named here for 0048's reason: two peers that
	# disagree about an argument disagree about the bytes, so a contract
	# that does not mention it is a contract that cannot be checked.
	#
	# Named rather than placed. The signature said `@0x0000 1 u8 n` for a
	# member occupying nothing -- the same offset as the member after it --
	# which is the wire contract making a false claim about where the bytes
	# are, and this file exists to be the one description that does not.
	#
	# `[stream]` is part of the contract and not a note beside it: it is
	# what says the argument may decide POSITION, so a peer reading this
	# learns whether the layout below moves with the argument or only its
	# meaning does.
	if placement.parameter:
		stream = " [stream]" if any(one.name == "stream"
		                            for one in placement.attrs) else ""
		return (f"{'parameter'.ljust(9)} {'-'.ljust(9)} "
		        f"{placement.type_name.ljust(10)} {placement.name}{stream}")

	parts = [
		_position(placement).ljust(9),
		_width(placement).ljust(9),
		placement.type_name.ljust(10),
		placement.name,
	]
	# What a parent hands the child (0068) is part of the contract: the
	# child's extent and rules follow it, so `note(mode = 3)` becoming
	# `note(mode = 4)` changes what a peer must send even though no row of
	# the child moves.
	facts = [*(f"args {name}={source.replace(' ', '')}"
	           for name, source in placement.arguments),
	         *_constraints(placement, env)]
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
	if placement.delimiters:
		# Every alternative, `|`-separated as the schema writes them. The
		# wire signature is a committed contract, so a member that ends at
		# three bytes and shows one would be a diff nobody reviewed.
		# `until:` or `before:`, because the two frame the same bytes
		# differently and a reader of the signature has to be able to tell:
		# one puts the delimiter inside the member and the other leaves it
		# for the next.
		word = "until" if placement.delimiter_consumed else "before"
		spelled = "|".join(one.hex() for one in placement.delimiters)
		return f"{word}:{spelled}" + ("|end" if placement.delimiter_or_end
		                              else "")
	if placement.size_max_bits != placement.size_bits:
		hi = ("" if placement.size_max_bits is None
		      else str(placement.size_max_bits // BITS_PER_BYTE))
		return f"{placement.size_bits // BITS_PER_BYTE}..{hi}"
	if placement.size_bits % BITS_PER_BYTE:
		return f"{placement.size_bits}bit"
	return str(placement.size_bits // BITS_PER_BYTE)


def _constraints(placement: Placement, env: Env) -> list[str]:
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
		facts.append(f"sized-by={_sized_by(placement.sized_by, env)}")
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
		             f"{_valued_source(placement.size_ast, placement.size_shown or placement.size_expr, env)}")
	# `at hdr.pixel_offset`: where the bytes *are*. `_position` renders `~` for
	# this member exactly as it does for one the data merely displaced, and the
	# two are different promises -- a displaced member follows the line above,
	# and this one goes wherever a field says, however far that is from
	# anything. The line above is not the reason, so it has to be stated.
	if placement.located is not None:
		# `at` names where the bytes ARE, so a const in it moves the member
		# and must not reach the contract as a name. Squashed too: this was
		# the one fact rendered with the author's spaces, inside a line
		# whose facts are space-delimited.
		facts.append(f"at={_valued_source(placement.located_ast, placement.located, env)}")
	# Where a run stops, which is the boundary between this member and the
	# next. Nothing else ends one, so the condition is the width.
	if placement.repeat_while is not None:
		facts.append(f"while="
		             f"{_valued_source(placement.repeat_ast, placement.repeat_shown or placement.repeat_while, env, fields=True)}")
	if placement.repeat_or_end:
		facts.append("while-or-end")
	if placement.repeat_cap is not None:
		facts.append(f"while-max={placement.repeat_cap}")
	# The alignment a pad promises (0043): a peer that pads to a different
	# multiple disagrees about where the next field starts, so it is contract.
	if placement.pad_to is not None:
		facts.append(f"pad-to={placement.pad_to}")
	# And the bounds one promises (0045), for the same reason one rung along:
	# a peer padding to different bounds disagrees about what lengths are
	# legal, which is as wire-visible as disagreeing about where the next
	# field starts.
	if placement.pad_bounds is not None:
		facts.append(f"pad-random={placement.pad_bounds[0]}"
		             f"..{placement.pad_bounds[1]}")
	if placement.radix is not None:
		facts.append(f"radix={placement.radix}")
	# 26.1165: what time the count is. Two consumers reading `expires_at`
	# in different units, or from different epochs, disagreed only as a
	# horizon error; stated, the difference is a line here.
	time = spec_of(placement.attrs, env)
	if time is not None:
		facts.append(time.render())
	if placement.scaled:
		# `scaled` and `decimal` both read base ten and do not read the
		# same bytes: `12.5` is a number under one and malformed under the
		# other, and what a reader gets back is a pair rather than an
		# integer (0056). `radix=10` alone said neither, so two members
		# that parse differently had one signature.
		facts.append("scaled")
	if placement.radix_minimal:
		facts.append("minimal")
	# 26.1166: each spelling rule a reader holds the digits to, beyond what
	# `minimal` already says; a peer spelling `+7` where this refuses it
	# disagrees about the bytes.
	rules = placement.radix_rules
	if rules & 1 and not placement.radix_minimal:
		facts.append("leading-zeros=refused")
	if rules & 4:
		facts.append("hex-case=upper")
	elif rules & 2 and not placement.radix_minimal:
		facts.append("hex-case=lower")
	if placement.radix_signs & 1:
		facts.append("plus-sign")
	if placement.radix_signs & 2:
		facts.append("negative-zero")
	if placement.radix_padded:
		# Where the digits end is part of what the bytes mean: `644` and a
		# NUL is a number under `[padded]` and malformed without it.
		facts.append("padded")
	if placement.trimmed:
		# The bytes, not the flag, which is what `skip=` below already does
		# and for the same reason: `trim` alone cannot tell a reader whether
		# a trailing newline is part of a value. And it belongs HERE rather
		# than on a file-level line, because `whitespace` is declared per
		# file and an imported struct trims its own file's set -- a
		# signature listing only the root's could not see the imported set
		# change at all.
		facts.append("trim=" + "".join(f"{byte:02x}"
		                               for byte in placement.trim_set))
	if placement.shares_byte:
		# The number after it starts in this member's byte (26.922), so a peer
		# that missed this places everything one byte late.
		facts.append("shares-byte")
	if placement.peek:
		# The sharpest interpretation fact there is: a peeked member is read
		# and not spent (0057), so everything after it -- above all the arm
		# a variant selects -- begins where this member began rather than
		# after it. A peer that missed this places every arm one member
		# late, which is a wrong layout and not a narrower one. It showed
		# in `example/json`, whose signature named the discriminant and
		# said nothing about the seven arms starting at it.
		facts.append("peek")
	if placement.skip:
		# A committed fact about the bytes, not a convenience: a member
		# that may be preceded by whitespace frames differently from one
		# that may not, and a peer reading this has to be able to tell.
		facts.append("skip=" + "".join(f"{byte:02x}"
		                               for byte in placement.skip))
	if placement.case_insensitive:
		facts.append("fold-case")
	if placement.delimiter_quote is not None:
		facts.append(f"quote={placement.delimiter_quote:#04x}")
	if placement.delimiter_escape is not None:
		facts.append(f"escape={placement.delimiter_escape:#04x}")
	if placement.delimiter_cap is not None:
		facts.append(f"cap={placement.delimiter_cap}")
	if placement.tail_bytes:
		facts.append(f"leaves={placement.tail_bytes}")
	if placement.tail_source is not None:
		facts.append(f"leaves_more={placement.tail_source}")
	if placement.from_end is not None:
		facts.append(f"from_end={placement.from_end}")
	if (held := holds_of(placement)) is not None:
		facts.append(f"holds={held}")
	if placement.since is not None:
		facts.append(f"since={placement.since}")
	if placement.varint is not None:
		facts.append(f"varint={placement.varint}")
	if placement.codec is not None:
		facts.append(f"codec={placement.codec}")
	if placement.tag_codec is not None:
		facts.append(f"codec={placement.tag_codec}")
	# Which field seeds the nonce and which selects the key (0040) is what
	# the region's bytes *mean* -- a receiver that disagrees about either
	# cannot interoperate -- and both are enforced, which is this file's
	# admission test (0041).
	if placement.sealed_nonce is not None:
		facts.append(f"nonce={placement.sealed_nonce}")
	if placement.sealed_key is not None:
		facts.append(f"key={placement.sealed_key}")

	facts.extend(_attribute_facts(placement, env))
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
#: `preserve` and `unknown` are the other half of one axis, and were missing
#: while `must_be_zero` and `must_be_one` were here (26.553). All four decide
#: whether a message with non-zero reserved bytes is VALID, which is the most
#: receiver-observable thing an attribute can do: measured, `[must_be_one]`
#: changes a comparison from `!= 0u` to `!= 0xFFu` -- four lines -- and
#: `[preserve]` or `[unknown]` deletes the validation block entirely, so the
#: default rejects such a message and these two accept it.
#:
#: Two peers a schema revision apart therefore computed an IDENTICAL signature
#: and disagreed about which messages are legal, which is the one thing this
#: file exists to prevent.
WIRE_ATTRS = (
	"must_eq", "min", "max", "must_be_zero", "must_be_one",
	"preserve", "unknown",
	"encoding", "nul_terminated", "self_as",
	# A run its keys must ascend through refuses what an unordered one
	# takes, which is a receiver's whole question (26.985).
	"ascending",
	# The bytes a run may hold (26.1099), in one canonical spelling so two
	# ways of typing a class are one fact.
	"chars",
)


#: The wire attributes whose value is a NUMBER the compiler resolves, as
#: against one that is a spelling checked by name. `[encoding = utf8]` is
#: the second kind: `utf8` is matched against `TEXT_ENCODINGS` and is not
#: an integer, so a schema declaring `const ascii = 7;` would otherwise
#: publish `encoding=7` -- which no peer could act on and which names a
#: different encoding from the one enforced (26.474).
VALUE_ATTRS = ("must_eq", "min", "max", "self_as")


def _valued(expr: ast.Expr, env: Env, *, fields: bool) -> ast.Expr:
	"""The expression with every const and literal reduced to its value.

	0041 asks the signature to record what is enforced, and both renderings
	the solver carries are SPELLINGS. That fails in two directions at once,
	and the tree had one of each:

	  - silent on a real change -- `align_up(HEADER_BYTES + ..., 4)` names
	    the const, so `HEADER_BYTES` going from 110 to 200 moves the padding
	    run in every cpio entry and the signature does not move;
	  - loud on no change -- `expr_to_source` prints `IntLiteral.text`, so
	    respelling `0x33` as `51` reports BREAKING over bytes that are
	    identical.

	26.474 fixed the first for a BARE const, in `_sized_by`, and this is the
	same defect one code path along: that path takes a plain string and this
	one an expression, and only the string was taught to resolve.

	A character literal reduces to its `code` for the same reason -- it is
	what the schema's encodings agree the byte is worth, and it is the byte
	a peer sees. `Access` and `Remaining` are left whole: the first is a
	field path whose base is a struct rather than a const, and rewriting
	inside one would turn a member name into a number.
	"""
	if isinstance(expr, ast.IntLiteral):
		return ast.IntLiteral(expr.span, expr.value, str(expr.value))
	if isinstance(expr, ast.CharLiteral):
		return ast.IntLiteral(expr.span, expr.code, str(expr.code))
	if isinstance(expr, ast.NameRef):
		# `fields`: whether a BARE name here is a field rather than a const,
		# which is a property of the call site and not of the name. In a
		# `while` predicate every bare name is a field of the element struct
		# -- `check_repeats` refuses anything else -- so a const sharing a
		# field's name must not be substituted. It is legal to write one,
		# and doing so turned `while (kind == 0x33)` into `while=153==51`:
		# a predicate comparing two constants, published as the contract,
		# with the field reference destroyed. Zero collisions in the corpus
		# today, which is why nothing caught it.
		#
		# A size expression is the other way, and the COMPILER decides it
		# rather than this file: `const n = 99` beside a field `n` emits
		# `SITU_S_A_COUNT 100u`, so the const wins and `sized-by=99+1` is
		# the honest line -- `n+1` would suggest a field sizes the run.
		held = None if fields else env.consts.get(expr.name)
		return (expr if held is None
		        else ast.IntLiteral(expr.span, held, str(held)))
	if isinstance(expr, ast.Binary):
		return ast.Binary(expr.span, expr.op,
		                  _valued(expr.left, env, fields=fields),
		                  _valued(expr.right, env, fields=fields))
	if isinstance(expr, ast.Unary):
		return ast.Unary(expr.span, expr.op,
		                 _valued(expr.operand, env, fields=fields))
	if isinstance(expr, ast.Call):
		return ast.Call(expr.span, expr.name,
		                tuple(_valued(one, env, fields=fields)
		                      for one in expr.args))
	if isinstance(expr, ast.Index):
		# Only the subscript is arithmetic; the base is a path.
		return ast.Index(expr.span, expr.base,
		                 None if expr.index is None
		                 else _valued(expr.index, env, fields=fields))
	if isinstance(expr, ast.Access) and isinstance(expr.base, ast.NameRef):
		# `Kind.alpha` is a VALUE and `header.namesize` is a field, and both
		# are an `Access` over a `NameRef`. `_sized_by` already tells them
		# apart -- consts, then a dotted enum member, then a field path --
		# and leaving this branch out made `_valued` disagree with it inside
		# one artifact: `u8 a[Kind.alpha]` resolved to a number through the
		# string path while `u8 a[Kind.alpha + n]` published the name. That
		# is this entry's own defect one code path along, which is the thing
		# it is about.
		arms = env.enums.get(expr.base.name)
		held = None if arms is None else arms.get(expr.name)
		if held is not None:
			return ast.IntLiteral(expr.span, held, str(held))
	return expr


def _valued_source(tree: ast.Expr | None, shown: str | None,
		env: Env, *, fields: bool = False) -> str:
	"""`tree` resolved to values, or the spelling where no tree travelled.

	**The fallback is unreachable today, and that is worth saying rather
	than implying it guards something.** Eleven sites construct a
	`Placement`; two set `size_expr`, one `repeat_while` and one `located`,
	and all four set the matching tree beside it. `dataclasses.replace`
	copies what it is not given, so the five `replace` sites keep both.

	What would make it fire is a NEW construction site that sets a string
	and not a tree -- and then this would quietly publish the spelling
	again, which is the whole defect. It is a fallback rather than an
	assertion because a rendering path is the wrong place to abort a user's
	build; the guard against it is that the two are set together, in four
	places, named here so the next reader can check.
	"""
	from situc.unparse import expr_to_source, kernel_to_source

	if tree is None:
		return _squash(shown or "")
	return _squash(expr_to_source(_valued(tree, env, fields=fields),
	                              explicit=False))


def _sized_by(name: str, env: Env) -> str:
	"""What sizes this run: a field a peer can read, or a number.

	`sized_by` is a plain string rather than an expression, so `evaluate`
	is unavailable and the lookup mirrors its order by hand -- consts
	first, then a dotted enum member. A name in neither is a FIELD path,
	and a field's name is the enforceable fact: a peer locates it in the
	byte stream, where it cannot locate a constant that exists only in
	one schema's source.

	The width column is not a substitute for resolving a const here. A
	member carrying `[size = N]` has its width frozen by the pin while
	the meaningful length moves with the constant, so `const NAME = 8`
	becoming `16` changed the generated `_COUNT` macro and left the
	signature byte-identical (26.474).

	`remaining` is guarded FIRST and deliberately: `const remaining = 4;`
	is legal and coexists with the `[remaining]` keyword, which wins the
	layout -- measured, the member is genuinely unbounded while
	`env.consts` holds a 4. Resolving it would publish a length the
	format does not have. The keyword is the only way this string can
	arrive, `sizing_field` returning it literally for `ast.Remaining`.
	"""
	if name == "remaining":
		return name
	if name in env.consts:
		return str(env.consts[name])
	held, dot, member = name.rpartition(".")
	if dot and held in env.enums:
		found = env.enums[held].get(member)
		if found is not None:
			return str(found)
	return name


def _attribute_facts(placement: Placement, env: Env) -> list[str]:
	from situc.unparse import expr_to_source, kernel_to_source

	facts = []
	for attr in placement.attrs:
		if attr.name not in WIRE_ATTRS:
			continue
		if attr.value is None:
			facts.append(attr.name)
			continue
		if attr.name == "chars":
			held = chars.of((attr,))
			assert held is not None
			facts.append(f'chars="{chars.canonical(held)}"')
			continue

		# The VALUE, not the author's spelling of it. This file is read by
		# a receiver that is already deployed and cannot be recompiled, so
		# `must_eq=PROTOCOL_VERSION` promises it nothing: the name lives in
		# one schema's source and the byte is what arrives. The generated
		# code has always flattened it -- the C reads `!= 2` -- so the
		# signature was recording something other than what is enforced,
		# which 0041 calls the defect rather than a lesser form of the
		# truth.
		#
		# `evaluate` is the discriminator as well as the resolver: it
		# reaches `env.consts` and `env.enums` and RAISES for a field
		# reference, which is exactly the case whose spelling must stay --
		# a field is a pointer into the byte stream, and a peer resolves it
		# at parse time. Same call and same environment as `pack.py`, so
		# the two cannot disagree about a value.
		shown = None
		if attr.name in VALUE_ATTRS:
			try:
				shown = evaluate(attr.value, env)
			except SituError:
				shown = None
		facts.append(f"{attr.name}="
		             f"{shown if shown is not None else expr_to_source(attr.value)}")
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
		# A carried value, with its arithmetic: `previous + delta` and
		# `previous + delta + 1` number every item differently, and the
		# name alone is the same either way.
		for part in sorted(grammar.tag_running, key=lambda p: p.name):
			lines.append(f"  {name} tlv running {part.name}: {part.source}")
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
	"""One of section 9.5's five ways for a value to say where it ends.

	The arithmetic of a computed size is part of the signature, not a
	detail under it: `length` and `length + 13` are different wire formats
	and the kind word is the same for both, so a signature printing only
	`computed` would call a changed extent unchanged -- which is what this
	file exists to refuse.
	"""
	parts = [rule.kind]
	if rule.size is not None:
		parts.append(str(rule.size))
	if rule.length_type is not None:
		parts.append(rule.length_type)
	if rule.source is not None:
		parts.append(rule.source)
	return " ".join(parts)


def _known_tag(known: KnownTag) -> str:
	parts = [known.name]
	if known.wire is not None:
		parts.append(f"wire={known.wire}")
	if known.type_name is not None:
		parts.append(f"type={known.type_name}")
	if known.repeated:
		parts.append("repeated")
	if known.form is not None:
		parts.append(f"form={known.form}")
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
	def local(path: str) -> str:
		"""A name in this struct's terms: the path with the struct dropped.

		The LEAF was used here, for both the tag and the members it covers,
		and a nested member brings its own leaf along -- so a struct holding
		two self-signed members renders two lines that both read `signature
		covers:` and both list a `body`, with nothing saying which is which.

		fuzznet met it from the consuming end: their provisioning card has
		three such lines and the wrong coverage of 26.519 sat in their
		committed contract unread, because two identical-looking lines each
		listing both structs' fields read as a description rather than as
		two tags making the same claim.

		It is not only unreadable. `_annotations` keys the comparison on the
		text before `": "`, so two lines with one name are ONE key and the
		later overwrites the earlier: measured, a tag losing coverage inside
		a parent produced no finding for that parent, and was caught only
		because the same struct is also rendered standalone. A tag that
		exists only within a parent has no such second chance (26.520).
		"""
		return path[len(struct.name) + 1:]

	lines = []
	for placement in struct.entries:
		held = placement.placement

		# A `coded` region's `covers` belongs here for this function's own
		# reason (14.1a). Widening it makes a peer transform bytes the old
		# version left alone, and the structure is identical either way --
		# which is the shape of change this signature exists to catch.
		if held.kind == "coded" and held.coded_covers:
			over = " ".join(sorted(held.coded_covers))
			lines.append(f"  {local(held.path)} covers: {local(held.path)}"
			             f" {over}")
			continue

		if held.kind not in ("tag", "checksum"):
			continue

		covered = sorted(
			entry.placement.path for entry in struct.entries
			if local(held.path) in entry.placement.covered_by)
		names = " ".join(local(path) for path in covered) or "nothing"
		# The prefix is part of what a peer has to sum (14.2a), and it is
		# invisible in the structure: every member, offset and size is
		# identical whether or not a pseudo-header is covered, so a change
		# here is exactly the kind this signature exists to catch.
		if held.tag_prefix is not None:
			names = f"{held.tag_prefix}(prefix) {names}"
		lines.append(f"  {local(held.path)} covers: {names}")
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
	"format":   ("FORMAT",
	             "the two signatures were rendered by different versions of "
	             "situc, so a line may differ in how it is WRITTEN rather "
	             "than in what it says; read what follows knowing that"),
}


def _format_version(signature: str) -> int:
	"""The `v` on the first line, or 0 for a signature written before it.

	0 is not a guess: the field arrived at v1 and 0041 kept the files that
	predate it readable, so a signature with no version line is a v0 one
	and saying so is more useful than refusing to read it.
	"""
	for line in signature.splitlines():
		found = re.match(r"# situ wire signature v(\d+)\s*$", line)
		if found:
			return int(found.group(1))
		if not line.startswith("#"):
			break		# past the header; there is no version line
	return 0


def compare(before: str, after: str) -> Verdict:
	"""Classify what changed between two committed signatures, with each
	side's enum arms in reach of the constraint comparison (26.1095)."""
	global _ARMS, _ENUM_LINES, _STRUCT_LINES
	_ARMS = (_enum_arms(before), _enum_arms(after))
	_ENUM_LINES = (_enum_lines(before), _enum_lines(after))
	_STRUCT_LINES = (_parse_signature(before), _parse_signature(after))
	try:
		return _compare(before, after)
	finally:
		_ARMS = ({}, {})
		_ENUM_LINES = ({}, {})
		_STRUCT_LINES = ({}, {})


#: Each side's structs as `_parse_signature` reads them, so a member whose
#: type changed can ask what the two types are (26.1129).
_STRUCT_LINES: tuple[dict[str, list[str]], dict[str, list[str]]] = ({}, {})


def _retyped_struct(old: str, new: str) -> str | None:
	"""How a member's struct type changed where its bytes stay (26.1129).

	`same`: one layout under two names. `refined`: the old type was its
	bytes and nothing else -- one `u8` run as wide as the struct -- and the
	new one reads those bytes as members, which is what fuzznet's roster
	bundle did with its 179-byte hop: a tightening, as a gained `holds` is.
	`coarsened` the reverse. None where neither holds.
	"""
	was = _STRUCT_LINES[0].get(old)
	now = _STRUCT_LINES[1].get(new)
	if was is None or now is None:
		return None

	def size(lines: list[str]) -> str | None:
		held = next((word.partition("=")[2] for word in lines[0].split()
		             if word.startswith("size=")), None)
		return held if held is not None and held.isdigit() else None

	def opaque(lines: list[str]) -> bool:
		members = [line.split() for line in lines[1:]
		           if not _is_annotation(line)]
		return (len(members) == 1 and members[0][2] == "u8"
		        and members[0][1] == size(lines)
		        and len(lines) == 2)

	if lines_without_name(was) == lines_without_name(now):
		return "same"
	if size(was) is None or size(was) != size(now):
		return None
	if opaque(was) and not opaque(now):
		return "refined"
	if opaque(now) and not opaque(was):
		return "coarsened"
	return None


def lines_without_name(lines: list[str]) -> list[str]:
	"""A struct's signature lines with its own name taken out."""
	head = lines[0].split()
	return [" ".join(head[2:]), *lines[1:]]


#: Each side's enums as their signature lines with the name taken out, so
#: two enums can be asked whether they differ in anything but it (26.1129).
_ENUM_LINES: tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]] \
	= ({}, {})


def _enum_lines(text: str) -> dict[str, tuple[str, ...]]:
	found: dict[str, list[str]] = {}
	current: list[str] | None = None
	for line in text.splitlines():
		if line.startswith("enum "):
			parts   = line.split()
			current = found.setdefault(parts[1], [" ".join(parts[2:])])
			continue
		if current is not None and line.startswith("  "):
			current.append(line.strip())
			continue
		current = None
	return {name: tuple(lines) for name, lines in found.items()}


def _renamed_enum(old: str, new: str) -> bool:
	"""Whether `new` is `old` under another name: the same width, policy,
	arms and values. fuzznet renamed `fzn_revocation_object` to clear a
	macro collision, 129 and 133 unchanged, and was told the same bytes now
	meant something else (26.1129)."""
	was = _ENUM_LINES[0].get(old)
	return was is not None and was == _ENUM_LINES[1].get(new)


def _compare(before: str, after: str) -> Verdict:
	"""Classify what changed between two committed signatures.

	Positional, because situ is: a member's identity is where it sits, so a
	renamed field at the same offset with the same width is an API change and
	a wire non-event, and a field of a different width at the same offset is
	a break whatever it is called.
	"""
	old_parsed  = _parse_signature(before)
	new_parsed  = _parse_signature(after)
	old_structs = _through_holds(old_parsed, new_parsed)
	new_structs = _through_holds(new_parsed, old_parsed)
	findings: list[Finding] = []

	# The version first, because it is context for everything under it: a
	# renderer that renames every token on a line makes the line-by-line
	# comparison unable to tell a rename from a narrowing, and this is the
	# field that says so (26.521).
	#
	# It does NOT suppress the findings below, which was the other design
	# and is the worse trade. A false break costs a reader an investigation
	# and their trust in the gate; a suppressed one ships. So the reader is
	# told and the comparison still speaks -- which is what this field's own
	# comment asks for and no more.
	was, now = _format_version(before), _format_version(after)
	if was != now:
		findings.append(Finding(
			"format", "signature",
			f"rendered by format v{was}, compared against v{now}"))

	findings.extend(_compare_globals(before, after))

	for name in sorted(set(old_structs) | set(new_structs)):
		old = old_structs.get(name)
		new = new_structs.get(name)

		if old is None:
			findings.append(Finding("forward", name,
			                        "new struct; no old sender emits one"))
			continue
		if new is None:
			# Read only inside other structs, its bytes are judged where
			# those members are; reported here too, it was a second BREAKING
			# for one renamed hop (26.1129).
			inside = sorted({parent for parent, lines in old_parsed.items()
			                 if parent != name and any(
			                     len(line.split()) > 2
			                     and line.split()[2] == name
			                     for line in lines[1:])})
			if inside:
				findings.append(Finding(
					"api", name,
					f"struct removed; it was read only inside "
					f"{', '.join(inside)}, and what an old peer sends there "
					"is judged at those members"))
				continue
			findings.append(Finding("breaking", name,
			                        "struct removed; an old peer still sends it"))
			continue
		moved = _into_a_length_arm(name, old, new, new_structs)
		if moved is not None:
			new, finding = moved
			findings.append(finding)
		findings.extend(_compare_struct(name, old, new))

	findings.extend(_compare_registries(before, after))
	return Verdict(findings)


def _into_a_length_arm(name: str, old: list[str], new: list[str],
		structs: dict[str, list[str]]
		) -> tuple[list[str], Finding] | None:
	"""Members that moved into a `switch (remaining)` arm (26.1125), or None.

	fuzznet's setting ended `u8 rank; u8 signature[64]`; its new contract
	ends in `variant tail switch (remaining)` whose `case 65` arm holds
	exactly those two, and its default arm the signature alone. Read
	member by member it was BREAKING -- "removed rank ... everything after
	them shifts" -- while a ranked setting's bytes are identical (inbox
	20261121T0700Z). So where the old struct's trailing members are one
	case arm's members at the variant's offset, the comparison reads that
	arm in the variant's place, and the finding is the one thing that did
	change: the struct now admits shapes the old one did not, which a new
	receiver reads and an old one refuses.
	"""
	members = [line for line in new[1:] if not _is_annotation(line)]
	if not members or members[-1].split()[2:3] != ["variant"]:
		return None
	variant = _name_of(members[-1])
	if f"{variant} switch: remaining" not in new:
		return None
	where = members[-1].split()[0]
	if not where.startswith("@0x"):
		return None
	base = int(where[1:], 16)
	before = [line for line in old[1:] if not _is_annotation(line)]
	tail = before[len(members) - 1:]
	if not tail or [_name_of(line) for line in before[:len(members) - 1]] \
			!= [_name_of(line) for line in members[:-1]]:
		return None
	for line in new:
		head, _, arm = line.partition(": ")
		if not head.startswith(f"{variant} case ") or head.endswith("default"):
			continue
		words = arm.split()
		if len(words) != 2 or words[1] not in structs:
			continue
		rebased = []
		for member in structs[words[1]][1:]:
			if _is_annotation(member):
				continue
			at, _, rest = member.partition(" ")
			if not at.startswith("@0x"):
				break
			rebased.append(f"@0x{base + int(at[1:], 16):04x} {rest}")
		else:
			if rebased == tail:
				others = sorted({line.partition(": ")[2].split()[0]
				                 for line in new
				                 if line.startswith(f"{variant} case ")
				                 and line.partition(": ")[2].split()[0]
				                 != words[0]} - {"error"})
				# The variant's own annotations go with it: the struct it
				# is compared against never had them.
				return ([new[0], *members[:-1], *tail,
				         *[line for line in new[1:] if _is_annotation(line)
				           and not line.startswith(f"{variant} ")]],
				        Finding(
					"backward", name,
					f"{', '.join(_name_of(line) for line in tail)} moved into "
					f"`{variant}`'s `case {head.split()[-1]}` arm `{words[0]}` at "
					f"the same bytes; the frame's length now also admits "
					f"{', '.join(others) or 'no other arm'}, which an old "
					f"receiver was not written for"))
	return None


#: Each side's enums as `{enum: {arm: value}}`, for the comparison under way;
#: set by `compare` for its own duration rather than passed through the four
#: layers between it and `_compare_facts`.
_ARMS: tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]] = ({}, {})


def _enum_arms(text: str) -> dict[str, dict[str, int]]:
	"""The `enum NAME ...` blocks of a signature: each arm's value."""
	found: dict[str, dict[str, int]] = {}
	current: dict[str, int] | None = None
	for line in text.splitlines():
		if line.startswith("enum "):
			current = found.setdefault(line.split()[1], {})
			continue
		if current is not None and line.startswith("  ") and "=" in line:
			for token in line.split():
				arm, _, value = token.partition("=")
				try:
					current[arm] = int(value, 0)
				except ValueError:
					pass
			continue
		current = None
	return found


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
	findings.extend(_compare_kernels(before, after))
	findings.extend(_compare_enums(before, after))
	return findings


def _compare_kernels(before: str, after: str) -> list[Finding]:
	"""A codec recomputed: every digest a peer sends stops verifying (26.891)."""
	old = _parse_kernels(before)
	new = _parse_kernels(after)
	return [Finding("breaking", f"kernel {name}",
	                f"now computes {new[name]}, was {old[name]}")
	        for name in sorted(old.keys() & new.keys())
	        if old[name] != new[name]]


def _parse_kernels(text: str) -> dict[str, str]:
	found: dict[str, str] = {}
	for line in text.splitlines():
		if line.startswith("kernel "):
			name, _, rest = line[len("kernel "):].partition(" : ")
			found[name] = rest
	return found


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


def _through_holds(structs: dict[str, list[str]],
		other: dict[str, list[str]] | None = None,
		depth: int = 0) -> dict[str, list[str]]:
	"""Each struct with a run that `holds = T` replaced by T's own members,
	at the run's offset (26.898, 26.1118).

	A run holding a struct is its members on the wire: replacing fourteen
	fields with a run that holds a struct of exactly those fields moves no
	byte, and the line-by-line comparison read it as fourteen fields
	removed -- fuzznet's contract was then rewritten on its vectors' word,
	the check unable to say so. So both sides are compared through the
	run. The run's own line goes, since the bytes it names are the held
	struct's; what the run says about its length stays in the head's size
	range, which is where a widened bound shows. A held struct holding
	another is expanded in turn, to a bounded depth.

	Only where the run's bytes ARE the struct's: a run whose extent no
	other field decides (a `[remaining]` tail), or one exactly the
	struct's size. A run sized by a field -- `u8 signer[signer_len]
	[holds = device]` -- keeps its line: its length is the field's, and
	splicing the struct's members in its place lined them up against the
	fields after the run, so fuzznet's `sig` read as narrowed 64 -> 32
	where no byte had moved (inbox 20261120T2010Z). Its `holds` is then a
	reading of bytes whose extent is unchanged, which the per-line check
	reports as api-only.

	And only where the OTHER side does not hold the same run as plain
	bytes. fuzznet's roster bundle gained `[holds = fzn_roster]` on the
	`record` it already had, and expanded on one side alone the run read as
	removed and the held struct's ten members as inserted -- fourteen
	BREAKING findings over a run whose bytes had not moved (26.1129).
	Compared line to line, the run gains a `holds`, which is a tightening."""
	if depth > 8:
		return structs
	other = other or {}

	def plain_there(struct: str, run: str) -> bool:
		"""Whether the other side has `run` in `struct`, holding nothing."""
		return any(_name_of(line) == run and not any(
		               word.startswith("holds=") for word in line.split())
		           for line in other.get(struct, [])[1:]
		           if not _is_annotation(line))
	changed = False
	out: dict[str, list[str]] = {}
	for name, lines in structs.items():
		expanded = [lines[0]]
		for line in lines[1:]:
			held = next((word.partition("=")[2] for word in line.split()
			             if word.startswith("holds=")), None)
			inner = structs.get(held or "")
			if inner is None or held == name or not _run_is_struct(
					line, inner[0]) or plain_there(name, _name_of(line)):
				expanded.append(line)
				continue
			changed = True
			# The run's own bound, kept as a line of its own: the bytes are
			# the held struct's, and how many of them the run admits is the
			# one thing the run says -- `max` dropped or raised is a wider
			# message, which an old receiver refuses (26.1118).
			words = line.split()
			expanded.append(f"{_name_of(line)} holds: {held} {words[1]}")
			where = line.split()[0]
			base = int(where[1:], 16) if where.startswith("@0x") else None
			for member in inner[1:]:
				if _is_annotation(member):
					continue
				at, _, rest = member.partition(" ")
				if base is not None and at.startswith("@0x"):
					at = f"@0x{base + int(at[1:], 16):04x}"
				else:
					at = "~"
				expanded.append(f"{at} {rest}")
		out[name] = expanded
	return _through_holds(out, other, depth + 1) if changed else out


def _run_is_struct(line: str, header: str) -> bool:
	"""Whether a run's bytes are exactly a held struct's: its extent is
	not another field's to decide, or it is the struct's size."""
	size = next((word.partition("=")[2] for word in header.split()
	             if word.startswith("size=")), None)
	sizer = next((word.partition("=")[2] for word in line.split()
	              if word.startswith("sized-by=")), "remaining")
	return sizer == "remaining" or line.split()[1] == size


def _parse_registries(text: str) -> dict[str, dict[str, set[str]]]:
	"""Registry name to its keys, each to the facts its line states."""
	found: dict[str, dict[str, set[str]]] = {}
	current: dict[str, set[str]] | None = None
	for line in text.splitlines():
		if line.startswith("registry "):
			current = found.setdefault(line.split()[1], {})
		elif line.startswith("  ") and current is not None:
			facts = line.split()
			current[facts[0].partition("=")[2]] = set(facts[1:])
		elif not line.startswith("  "):
			current = None
	return found


def _compare_registries(before: str, after: str) -> list[Finding]:
	"""A registry's keyspace, as a peer reads it (26.1106).

	A key removed is a cell an old peer still writes and a new one does not
	know: breaking. A key added is one no old peer writes: forward. A cell
	type changed reads the same bytes as another message: breaking. Every
	other fact -- a bound, a size, a default, `safe`, a policy word -- is a
	constraint, compared as a member's are, so a narrowed bound is the
	tightening it is.
	"""
	was, now = _parse_registries(before), _parse_registries(after)
	findings: list[Finding] = []
	for name in sorted(set(was) | set(now)):
		where = f"registry {name}"
		if name not in now:
			findings.append(Finding("breaking", where,
			                        "registry removed; an old peer still "
			                        "writes its cells"))
			continue
		if name not in was:
			findings.append(Finding("forward", where,
			                        "new registry; no old peer writes it"))
			continue
		old_keys, new_keys = was[name], now[name]
		for key in sorted(set(old_keys) | set(new_keys)):
			if key not in new_keys:
				findings.append(Finding(
					"breaking", where,
					f"{key} removed; an old peer still writes that cell"))
				continue
			if key not in old_keys:
				findings.append(Finding(
					"forward", where,
					f"{key} is new; no old peer writes it"))
				continue
			cell_was = {one for one in old_keys[key] if one.startswith("cell=")}
			cell_now = {one for one in new_keys[key] if one.startswith("cell=")}
			if cell_was != cell_now:
				findings.append(Finding(
					"breaking", where,
					f"{key}: {' '.join(sorted(cell_was))} -> "
					f"{' '.join(sorted(cell_now))}; the same bytes are now a "
					"message of another type"))
				continue
			findings.extend(_compare_facts(where, key,
			                               old_keys[key] - cell_was,
			                               new_keys[key] - cell_now))
	return findings


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

	findings.extend(_compare_aligned(name, old_members, new_members))
	findings.extend(_compare_annotations(name, old, new))
	return findings


def _compare_aligned(struct: str, old: list[str], new: list[str]
		) -> list[Finding]:
	"""Members compared by NAME, the two lists aligned where names agree.

	Paired by index, one member inserted shifted every comparison after it:
	openmlx4 wrapped fields in `authenticated` regions and split one 28-bit
	field into a checksum byte and twenty bits, and every later field of
	`fs2_header` was reported as moved and breaking -- none had moved a bit
	(26.1035). So the lists are aligned on their names, a member present in
	both is compared with itself, and what is left is removed, inserted or
	appended. A run replaced over exactly the bits it held is one finding.
	Names are unique within a struct; where they are not, position pairs.
	"""
	old_names = [_name_of(line) for line in old]
	new_names = [_name_of(line) for line in new]
	if len(set(old_names)) != len(old_names) \
			or len(set(new_names)) != len(new_names):
		return [finding
		        for index in range(max(len(old), len(new)))
		        for finding in _compare_member(
		            struct, index,
		            old[index] if index < len(old) else None,
		            new[index] if index < len(new) else None)]

	from difflib import SequenceMatcher

	findings: list[Finding] = []
	matcher = SequenceMatcher(None, old_names, new_names, autojunk = False)
	for tag, i1, i2, j1, j2 in matcher.get_opcodes():
		if tag == "equal":
			for offset in range(i2 - i1):
				findings.extend(_compare_member(
				    struct, j1 + offset, old[i1 + offset], new[j1 + offset],
				    last = (i1 + offset == len(old) - 1
				            and j1 + offset == len(new) - 1)))
			continue
		if tag == "replace" and i2 - i1 == j2 - j1:
			# Renames in place, or a member swapped for another at its
			# position: the positional comparison says which, and a rename
			# at the same bytes is the API change it always was.
			for offset in range(i2 - i1):
				findings.extend(_compare_member(struct, j1 + offset,
				                                old[i1 + offset],
				                                new[j1 + offset]))
			continue
		if tag == "replace" and _same_bits(old[i1:i2], new[j1:j2]):
			findings.append(Finding(
				"breaking", f"{struct}[{j1}]",
				f"{', '.join(old_names[i1:i2])} replaced by "
				f"{', '.join(new_names[j1:j2])} over the same bits; an old "
				"receiver reads them as what they replaced"))
			continue
		for index in range(i1, i2):
			findings.extend(_compare_member(struct, index, old[index], None))
		for index in range(j1, j2):
			if i2 >= len(old):
				# Past every old member: appended, which `_compare_member`
				# already knows how to judge, `[since]` included.
				findings.extend(_compare_member(struct, index, None,
				                                new[index]))
				continue
			findings.append(Finding(
				"breaking", f"{struct}[{index}]",
				f"inserted {new_names[index]} at "
				f"{new[index].split()[0]}; an old receiver does not expect "
				"these bytes"))
	return findings


def _same_bits(old: list[str], new: list[str]) -> bool:
	"""Whether two runs of member lines start together and span as much."""
	def span(lines: list[str]) -> tuple[str, int] | None:
		total = 0
		for line in lines:
			width = line.split()[1]
			if width.endswith("bit") and width[:-3].isdigit():
				total += int(width[:-3])
			elif width.isdigit():
				total += int(width) * 8
			else:
				return None		# a range or a computed width
		return lines[0].split()[0], total
	held = span(old)
	return held is not None and held == span(new)


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


def _byte_range(width: str) -> tuple[int, int | None] | None:
	"""A width as (fewest, most) bytes, most None for no bound; None for a
	width in bits or one a delimiter ends."""
	low, dots, high = width.partition("..")
	if not low.isdigit() or (dots and high and not high.isdigit()):
		return None
	if not dots:
		return int(low), int(low)
	return int(low), int(high) if high else None


def _within(inner: tuple[int, int | None],
		outer: tuple[int, int | None]) -> bool:
	return inner[0] >= outer[0] and (outer[1] is None or (
		inner[1] is not None and inner[1] <= outer[1]))


def _last_width(where: str, name: str, was: str, now: str
		) -> Finding | None:
	"""A struct's LAST member changing width moves no byte (26.1129).

	"every later field shifts" was said of members with none after them:
	fuzznet's trailing `content[remaining]` gaining `max 470` and `hops`
	behind a count gaining `[min = 1, max = 8]` were each BREAKING. What
	changed is which lengths are accepted, so it is judged as a `max` is: a
	narrower range is a demand a new receiver makes of an old sender, a
	wider one a promise an old receiver never made. A range that does both
	stays BREAKING, as does a width with no range to compare.
	"""
	old_range, new_range = _byte_range(was), _byte_range(now)
	if old_range is None or new_range is None:
		return None
	if _within(new_range, old_range):
		return Finding("backward", where,
		               f"{name} narrows {was} -> {now}; nothing follows it, "
		               "so no byte moves, and a new receiver may refuse a "
		               "length an old sender legitimately produces")
	if _within(old_range, new_range):
		return Finding("forward", where,
		               f"{name} widens {was} -> {now}; nothing follows it, "
		               "so no byte moves, and an old receiver may refuse a "
		               "length a new sender legitimately produces")
	return None


def _compare_member(struct: str, index: int, was: str | None,
		now: str | None, last: bool = False) -> list[Finding]:
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
	findings: list[Finding] = []
	if old_bits[1] != new_bits[1]:
		judged = (_last_width(where, _name_of(was), old_bits[1], new_bits[1])
		          if last else None)
		if judged is None:
			return [Finding("breaking", where,
			                f"{_name_of(was)} changes width "
			                f"{old_bits[1]} -> {new_bits[1]}; every later "
			                "field shifts for an old receiver")]
		findings.append(judged)
	# A type change at one position and width is an interpretation change, in
	# the sense `INTERPRETATION` means: the bytes are where they were and are
	# a different value. Reported alongside the facts rather than instead of
	# them, because unlike a move or a widening it shifts nothing after it, so
	# the rest of the line is still worth reading.
	retyped = (_retyped_struct(old_bits[2], new_bits[2])
	           if old_bits[2] != new_bits[2] else None)
	if old_bits[2] != new_bits[2] and (retyped == "same" or _renamed_enum(
			old_bits[2], new_bits[2])):
		findings.append(Finding(
			"api", where,
			f"{_name_of(now)} is {old_bits[2]} -> {new_bits[2]}, one "
			"layout under two names; the bytes are identical"))
	elif retyped == "refined":
		findings.append(Finding(
			"backward", where,
			f"{_name_of(now)} reads its bytes as {new_bits[2]}, where "
			f"{old_bits[2]} held them unread; no byte moves, and a new "
			"receiver may refuse a message an old sender legitimately "
			"produces"))
	elif retyped == "coarsened":
		findings.append(Finding(
			"forward", where,
			f"{_name_of(now)} holds its bytes unread as {new_bits[2]}, "
			f"where {old_bits[2]} read them; no byte moves, and an old "
			"receiver may refuse a message a new sender legitimately "
			"produces"))
	elif old_bits[2] != new_bits[2]:
		findings.append(Finding(
			"breaking", where,
			f"{_name_of(now)} is {old_bits[2]} -> {new_bits[2]}; the same "
			"bytes now mean something else"))

	return findings + _compare_facts(where, _name_of(was),
	                                 *_order_shown(old_bits[1], new_bits[1],
	                                               _facts_of(was),
	                                               _facts_of(now)),
	                                 (old_bits[2], new_bits[2]))


#: The byte orders `_constraints` names, and only where a member's FEWEST
#: bytes are more than one.
BYTE_ORDERS = ("big", "little", "native")


def _order_shown(old_width: str, new_width: str, was: set[str],
		now: set[str]) -> tuple[set[str], set[str]]:
	"""Both fact sets, the byte order left out where only the width hid it.

	`_constraints` names a byte order where the member's lower bound is
	past a byte, so `hops` behind a count gaining `[min = 1]` went from `0..`
	to `179..` and gained `big` -- "nothing -> big; the same bytes now mean
	something else" of bytes read exactly as before (26.1129). Where one side
	could not have shown it, its absence there says nothing.
	"""
	def shown(width: str) -> bool:
		held = _byte_range(width)
		return held is None or held[0] > 1
	if shown(old_width) == shown(new_width):
		return was, now
	return ({fact for fact in was if fact not in BYTE_ORDERS},
	        {fact for fact in now if fact not in BYTE_ORDERS})


#: Facts that say what the bytes *are*, rather than which of them are allowed.
#:
#: `holds=` is not one. The run's bytes are read as they were; the held
#: struct decides which of them `validate` accepts, so a gained `holds` is
#: a tightening and a dropped one a loosening -- as a `max` is. It sat here
#: until fuzznet (inbox 20261121T0400Z) asked what a peer misreads, and the
#: answer was nothing (26.898, 26.1118).
#:
#: The distinction decides everything. A changed `max` narrows the set of
#: messages both sides agree is valid, so old and new still read the same
#: number and one of them may refuse it. A changed byte order means the same
#: bytes are a different number, which no amount of agreement about ranges
#: helps with -- so these are breaking, and the constraint facts are not.
INTERPRETATION = (
	"big", "little", "native", "msb_first", "lsb_first",
	"radix=", "scaled", "peek", "sized-by=", "varint=", "codec=", "encoding=",
	"nonce=", "key=", "pad-to=",
	"endian-from=", "quote=", "escape=", "trim=", "fold-case",
	"nul_terminated",
	# Where the bytes are, where the run stops, and what the checksum field
	# is worth while the sum runs over it. None of the three narrows a set of
	# permitted values: each of them puts a different value in front of the
	# receiver, or a different number of bytes.
	"at=", "while=", "self_as=",
)


#: What time a member holds (26.1165): compared on its own, above.
TIMED = ("time=", "duration=")

#: Facts that admit a spelling rather than refuse one (26.1166), so that
#: gaining one widens what a receiver accepts.
ADMITS = frozenset({"plus-sign", "negative-zero"})


def _is_interpretation(fact: str) -> bool:
	return any(fact == key or fact.startswith(key) for key in INTERPRETATION)


def _compare_facts(where: str, name: str, was: set[str],
		now: set[str], types: tuple[str, str] = ("", "")) -> list[Finding]:
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
	# By value, not spelling: a contract rendered before the signature
	# reduced literals (format v1) says `must_eq=0x4D544657` where today's
	# says `must_eq=1297368663`, and the two are one constraint -- openmlx4's
	# FS2 magics reported four BREAKING findings over an unchanged schema
	# (26.1010).
	# And an ARM by its value: `must_eq=data` in a contract rendered before
	# a bare arm was read as the arm (26.1094) is `must_eq=4` today, one
	# constraint -- fuzznet's unchanged `spool/message` reported four
	# breaking findings (26.1095). Each side's own enum table, which the
	# signature carries.
	# But not an arm a scalar SIBLING shadows: by 26.1094's own rule the
	# bound then reads the sibling, as the four backends build it, and
	# taking the name for the arm called fuzznet's `type [must_eq = base]`
	# -- `base` a `u64` beside it -- the same contract as `must_eq = 8`
	# (inbox 20261122T0921Z, 26.1150).
	old_arms, new_arms = _ARMS
	struct = where.partition("[")[0]
	old_own = {arm: value for arm, value in old_arms.get(types[0], {}).items()
	           if arm not in _scalar_siblings(0, struct)}
	new_own = {arm: value for arm, value in new_arms.get(types[1], {}).items()
	           if arm not in _scalar_siblings(1, struct)}
	was = {_by_value(fact, old_own) for fact in was}
	now = {_by_value(fact, new_own) for fact in now}
	findings = []

	# What time a member holds (26.1165). A changed unit, epoch or kind
	# keeps every byte where it was and changes what every value means: an
	# old receiver reads microseconds as an instant 31,000 years ahead.
	# This called fuzznet's `s` -> `us` backward-compatible, as a narrowing
	# (its inbox 20261123T1801Z). Stating a time where none was, or
	# dropping one, moves nothing a peer reads.
	timed_was = {fact for fact in was if fact.startswith(TIMED)}
	timed_now = {fact for fact in now if fact.startswith(TIMED)}
	if timed_was and timed_now and timed_was != timed_now:
		return [Finding(
			"breaking", where,
			f"{name}: {' '.join(sorted(timed_was))} -> "
			f"{' '.join(sorted(timed_now))}; the same bytes now count "
			"another time")]
	if timed_was != timed_now:
		findings.append(Finding(
			"api", where,
			f"{name}: {' '.join(sorted(timed_was)) or 'no time'} -> "
			f"{' '.join(sorted(timed_now)) or 'no time'}; what the value "
			"means is stated or unstated, and no byte or value moves"))
	was -= timed_was
	now -= timed_now

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

	# A fact that ADMITS a spelling (26.1166) runs the other way: gaining
	# `plus-sign` is a loosening, which every other fact here is not.
	for key in sorted(set(added) - set(dropped)):
		findings.append(Finding(
			"forward", where,
			f"{name} gains `{added[key]}`; an old receiver may refuse a "
			"message a new sender legitimately produces")
			if key in ADMITS else Finding(
			"backward", where,
			f"{name} gains `{added[key]}`; a new receiver may refuse a message "
			"an old sender legitimately produces"))
	for key in sorted(set(dropped) - set(added)):
		findings.append(Finding(
			"backward", where,
			f"{name} drops `{dropped[key]}`; a new receiver may refuse a "
			"message an old sender legitimately produces")
			if key in ADMITS else Finding(
			"forward", where,
			f"{name} drops `{dropped[key]}`; an old receiver may refuse a "
			"message a new sender legitimately produces"))

	return findings


def _scalar_siblings(side: int, struct: str) -> set[str]:
	"""The members of `struct` on one side a bound may read as a value --
	those whose type is neither a run nor a struct -- by name."""
	lines = _STRUCT_LINES[side].get(struct, [])
	names: set[str] = set()
	for line in lines[1:]:
		words = line.split()
		if _is_annotation(line) or len(words) < 4 \
				or not words[0].startswith("@"):
			continue
		kind = words[2]
		if "[" in kind or kind in _STRUCT_LINES[side]:
			continue
		names.add(words[3])
	return names


def _by_value(fact: str, arms: dict[str, int] | None = None) -> str:
	"""`key=<integer literal>` with the literal as its decimal value, and
	`key=<arm>` of the member's enum as the arm's; any other fact as it
	stands."""
	key, equals, value = fact.partition("=")
	if not equals:
		return fact
	if arms and value in arms:
		return f"{key}={arms[value]}"
	try:
		return f"{key}={int(value, 0)}"
	except ValueError:
		return fact


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
	if key == "chars":
		# A class is a set, and a narrower one refuses bytes the wider took
		# (26.1099). Neither inside the other is both at once, which the
		# caller reports under the heading that warns.
		old_set, new_set = _class_of(was), _class_of(now)
		if old_set == new_set:
			return None
		if new_set < old_set:
			return True
		if new_set > old_set:
			return False
		return None
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


def _class_of(fact: str) -> frozenset[int]:
	"""A `chars="..."` fact's bytes, read back from its canonical spelling."""
	text = fact.partition("=")[2].strip('"')
	return chars.parse(text.encode("ascii").decode("unicode_escape"),
	                   None)					# type: ignore[arg-type]


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
	if _what(key) == "holds":
		return _holds_changed(name, key, was, now)
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

	# A known tag's `form` (26.1167) decides only which values are
	# accepted, never what one reads as: `form = run` added refuses a value
	# that is no whole number of its type and reads every other exactly as
	# before. openmlx4 adopting it was reported BREAKING by the line below.
	# Added it narrows, removed it widens; `run` for `number` is neither,
	# each accepting values the other refuses.
	if " tlv known " in f" {key}":
		def without_form(text: str) -> list[str]:
			return [word for word in text.split()
			        if not word.startswith("form=")]
		def form_of(text: str) -> str | None:
			return next((word[len("form="):] for word in text.split()
			             if word.startswith("form=")), None)
		if without_form(was) == without_form(now):
			before, after = form_of(was), form_of(now)
			if before is None and after is not None:
				return Finding(
					"backward", name,
					f"`{key}` gains form={after}; every value it accepted "
					"before reads the same, and a new receiver may refuse "
					"one an old sender legitimately produces")
			if before is not None and after is None:
				return Finding(
					"forward", name,
					f"`{key}` drops form={before}; every value reads the "
					"same, and an old receiver may refuse one a new sender "
					"legitimately produces")

	return Finding(
		"breaking", name,
		f"`{key}` {was} -> {now}; the bytes are where they were and are read "
		"as something else")


def _holds_bound(text: str) -> int | None:
	"""The upper bound of a `holds` annotation's `lo..hi`, None unbounded."""
	_, _, hi = text.split()[-1].partition("..")
	return int(hi) if hi.isdigit() else None


def _holds_changed(name: str, key: str, was: str, now: str) -> Finding:
	old, new = _holds_bound(was), _holds_bound(now)
	wider = new is None if old is not None else False
	wider = wider or (old is not None and new is not None and new > old)
	if wider:
		return Finding(
			"backward", name,
			f"`{key}` {was} -> {now}; a new sender may emit a longer run than "
			"an old receiver admits")
	return Finding(
		"forward", name,
		f"`{key}` {was} -> {now}; a new receiver refuses a run an old sender "
		"may still emit at its old length")


def _annotation_lost(name: str, key: str, was: str) -> Finding:
	if _what(key) == "covers":
		# Coverage that is simply gone. A `coded` region losing its `covers`
		# changes no member and no offset (14.1a).
		return Finding("coverage", name,
		               f"`{key}` is gone; what it authenticated ({was}) is now "
		               "unauthenticated")
	if _what(key) == "holds":
		return Finding("api", name,
		               f"`{key}` is gone ({was}): the same members are now "
		               "the struct's own, and no byte moves")

	return Finding(
		"breaking", name,
		f"`{key}` is gone; it said {was}, and an old sender still emits bytes "
		"this build has nothing to read them with")


def _annotation_gained(name: str, key: str, now: str) -> Finding:
	if _what(key) == "covers" and "." in key.split()[0]:
		# A tag reached through a member, gained because the member now
		# reads its bytes as a struct with one (26.1129): the member's own
		# finding says so, and the tag is one more thing a new receiver
		# checks of bytes that have not moved.
		return Finding("backward", name,
		               f"`{key}` now {now}, through a member that reads it "
		               "now; a new receiver verifies what an old one passed "
		               "unread")
	if _what(key) == "covers":
		# Absent while only tags reached this function, because a new tag
		# brings a new field and the member comparison already reports one. A
		# `coded` region gaining a `covers` clause changes no member and no
		# offset, so without this the two signatures compared equal while the
		# peers disagreed about which bytes to transform.
		return Finding("coverage", name,
		               f"`{key}` now {now}, which nothing covered before")
	if _what(key) == "holds":
		return Finding("api", name,
		               f"`{key}` now {now}: these members are read through a "
		               "run that holds them, and no byte moves")

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
	for kind in ("format", "breaking", "coverage", "backward",
	             "forward", "api"):
		found = [f for f in verdict.findings if f.kind == kind]
		if not found:
			continue
		heading, gloss = KINDS[kind]
		lines.extend(["", f"{heading}: {gloss}"])
		lines.extend(f"  {f.subject}: {f.detail}" for f in found)

	# The format notice is not a change to the contract, so it is not
	# counted as one: "1 breaking, 40 compatible" where one of the forty is
	# the notice would be the tally describing itself.
	counted = [f for f in verdict.findings if f.kind != "format"]
	breaks  = sum(1 for f in counted if f.is_break)
	lines.extend(["", f"{breaks} breaking, "
	              f"{len(counted) - breaks} compatible.", ""])
	return "\n".join(lines)
