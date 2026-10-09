# Append-only message building: the write half of rung 2 (26.610).
#
# `--owned` already encodes: `_encode` lays a filled struct back into a
# buffer. It is restricted to a struct whose every member has a fixed-size C
# field, so what it cannot write is exactly what 0032 row 2 names -- "build
# or resize a message whose extent is not fixed". hull asked for that half
# and found nothing emitting it in any backend (suggestion/hull.md, 11).
#
# The restriction here is append-only, and it is a property of the LAYOUT
# rather than a flag: `traverse.append_only_refusals` reads it out of the
# schema, because 0032's closing invariant is that no rung invents a fact
# the schema did not state. A caller cannot ask for an append-only build of
# a format that is not one.
#
# Two writers of one member is how a pair comes to be self-consistent and
# wrong together, which `owned.py` paid for in BCD. So this file emits no
# representation conversion at all: it writes through the runtime's own
# `situ_put_*`, and REFUSES every member whose value differs from its bits
# by more than byte order -- BCD, a radix, a sub-byte scalar, a varint. The
# conversion then has exactly one implementation per direction, and it is
# not this one.
#
# And the bytes are checked by the view rather than by a second opinion:
# every build ends by validating what it wrote through the generated
# `_validate`, for the reason `owned.py` states -- two checks of one schema
# is how they come to disagree.

from __future__ import annotations

from collections.abc import Mapping

from situc import ast, traverse
from situc.codegen.c.names import bare_name, ident, macro
from situc.codegen.c.owned import _ctype, storage_width
from situc.layout import Arm, BITS_PER_BYTE, Placement
from situc.resolve import ResolvedSchema, ResolvedStruct
from situc.types import ScalarKind
from situc import __version__

WORD_WIDTHS = (8, 16, 32, 64)

#: The writer's own locals. Prefixed because a member name is a schema's to
#: choose and these are not: `test/schema/image.situ` has a member called
#: `cap`, which redefined the capacity parameter and was then unused, so one
#: collision produced two compile errors. A member that collides even with
#: these is refused by name rather than mangled -- a parameter list is an
#: interface, and renaming somebody's member in it is the kind of invention
#: 0032 asks a rung not to make.
OUT, CAP, WROTE = "situ_out", "situ_cap", "situ_wrote"
AT, MSG, VIEW, ERR = "situ_at", "situ_msg", "situ_view", "situ_err"
DIGITS, SCRATCH, SCAN = "situ_digits", "situ_scratch", "situ_scan"
NEED, COUNT = "situ_need", "situ_count"
FIXTURES = frozenset({OUT, CAP, WROTE, AT, MSG, VIEW, ERR, DIGITS, SCRATCH,
                      SCAN, NEED, COUNT, "situ_fixed"})

# What a member is to the writer. `literal` and `size` take no parameter:
# both are facts the schema states, and asking the caller for either would
# invite a message that disagrees with its own schema.
LITERAL = "literal"	# pinned bytes, written from the schema
SIZE    = "size"	# the length of a run that follows it
SCALAR  = "scalar"	# one whole-byte binary scalar, from the caller
RUN     = "run"		# a byte run of fixed length, from the caller
SPAN    = "span"	# a byte run whose length the caller supplies
ENDED   = "ended"	# a byte run the caller measures, then its delimiter
NESTED  = "nested"	# a whole inner struct the caller built
REPEAT  = "repeat"	# a run of inner structs the caller built
PEEKED  = "peeked"	# a member that reads bytes belonging to what follows
ARMTAG  = "armtag"	# a discriminant the chosen arm decides
BITS    = "bits"	# one field of a byte several members share

#: The roles a caller supplies a value for, which is one list because three
#: of them had drifted: the signature named five, the uniquifier three and
#: the "nothing to build" check three, so a struct of nested members was
#: reported as entirely literal.
CALLER_SUPPLIED = frozenset({SCALAR, RUN, SPAN, ENDED, NESTED, REPEAT,
                             BITS})

#: And of those, the ones the caller measures, which emit a `_len` beside
#: the pointer. A fixed run does not: the schema counted it.
MEASURED = frozenset({SPAN, ENDED, NESTED, REPEAT})
ZERO    = "zero"	# reserved bytes, which no caller names


class Part:
	"""One member, and how the writer reaches it."""

	def __init__(self, role: str, placement: Placement, local: str,
			bytes_: int = 0, literal: bytes = b"",
			sizes: Placement | None = None, radix: int = 0,
			minimal: bool = False, delimiter: bytes = b"",
			digits: int = 0, inner: str = "", value: int = 0,
			others: tuple[int, ...] = (), bit_at: int = 0,
			opens: bool = False, closes: bool = False) -> None:
		self.role      = role
		self.placement = placement
		self.local     = local
		self.bytes     = bytes_
		self.literal   = literal
		self.sizes     = sizes		# the run this member's value measures
		self.radix     = radix		# 0 where the value is written as bits
		self.minimal   = minimal	# no leading zeros, so the width varies
		self.delimiter = delimiter	# what ends a minimal run of digits
		self.digits    = digits		# the most digits the schema allows
		self.inner     = inner		# the struct a nested member holds
		self.value     = value		# what an `armtag` writes
		self.others    = others		# the values other arms claim
		self.bit_at    = bit_at		# where in a packed group this sits
		self.opens     = opens		# first of a packed group
		self.closes    = closes		# last of one


def _local(struct: ResolvedStruct, placement: Placement) -> str:
	"""The parameter name for a member.

	A parameter list is the third place a member name is emitted on its own,
	after the owned struct's field and every C++ accessor -- so it is
	`bare_name`'s third caller rather than a new rule, and it mangles for
	0025's reason: the schema keeps its name and the emitter moves. The
	first draft used the path verbatim and `<reserved0>` reached four
	signatures; the second used `c_name` and a member called `short` reached
	one.

	Mangling costs a caller nothing here, which is worth saying because it
	is not true everywhere: C has no named arguments, so a parameter name is
	documentation rather than call syntax. Refusing the struct would cost
	them the builder.
	"""
	return bare_name(traverse.local_name(struct, placement))


def _whole_bytes(placement: Placement) -> int | None:
	"""How many whole bytes this member occupies, or None if it is not
	byte-aligned or not of a fixed size."""
	if placement.size_bits is None or placement.size_bits <= 0:
		return None
	if placement.size_bits % BITS_PER_BYTE:
		return None
	return placement.size_bits // BITS_PER_BYTE


def ends_before_a_byte_it_does_not_own(struct: "ResolvedStruct") -> bool:
	"""Whether this struct's last member is a `before` run.

	Such a struct has a `_required` and it can never answer the length a
	caller passes: with the delimiter absent it reports TRUNCATED, and with
	it present the extent stops short of it, because a `before` run does
	not consume. So the bytes cannot be handed over as a self-measuring
	blob -- the parent owns the byte that ends them.

	Found by building one: `situ_sexpr_build_as_symbol` was accepted at
	plan time and refused every input at run time, which is the
	honest-but-useless state a refusal by name is for.
	"""
	members = traverse.own_members(struct)
	if not members:
		return False
	last = members[-1]
	return bool(last.delimiters) and not last.delimiter_consumed


def frames_itself(header: str, name: str) -> bool:
	"""Whether the emitted header declares `<name>_required`.

	A nested struct is written as bytes the caller built, and the writer
	has to know they are exactly ONE of them -- a length three bytes too
	long shifts every member after it, and whether the result then fails
	`_validate` depends on what those three bytes happen to say. The
	struct's own `_required` answers it exactly: situ emits one for every
	struct whose extent its own bytes determine.

	**Read from the artifact rather than predicted.** Whether `_required`
	exists is decided inside `emit.py` by three of its methods, and the
	header says plainly what they decided -- so this asks the file instead
	of deriving the answer a second way, which is how two derivations come
	to disagree. `names.py` scans the runtime header for its function
	names on the same argument.

	Matching the WHOLE declaration is what makes it safe: a struct taking
	arguments (0050) has them in its `_required` signature too, and this
	writer has no way to pass them, so the exact-match refuses it without
	a second rule to maintain.
	"""
	return (f"situ_err_t {name}_required(const uint8_t *data, "
	        "uint32_t have, uint32_t *need)") in header


def _digit_bytes(radix: int) -> frozenset[int]:
	"""The bytes `situ_format_uint` can emit at this radix.

	Upper case only, which is that function's own choice and argued where it
	lives: a lower-case hex digit is a second spelling of one number and is
	refused on the way in rather than tolerated on the way out.
	"""
	alphabet = "0123456789ABCDEF"[:radix]
	return frozenset(ord(digit) for digit in alphabet)


def _radix_refusal(placement: Placement) -> str | None:
	"""Why this member's digits cannot be written, or None.

	The runtime writes digits already -- `situ_format_uint`, which is
	`situ_parse_uint` backwards -- at a FIXED width, so the leading zeros
	are mandatory and one value is one byte sequence. What it cannot do is
	`[minimal]`, where the width follows the value; that needs the digit
	count, which is three lines on top of it rather than a second
	implementation of the conversion.
	"""
	scalar = placement.scalar
	assert scalar is not None
	if scalar.signed:
		return "is a signed number written in digits, and the runtime " \
		       "formats unsigned ones"
	if placement.radix_minimal:
		# Zero delimiters is not checked here because the parser refuses it
		# outright -- *`decimal n` has no end* -- so a branch for it would
		# be unreachable by construction rather than merely unexercised.
		if len(placement.delimiters) > 1:
			# The schema names several bytes that may end the digits, so
			# the writer would be choosing one, which is the invention
			# 0032 forbids.
			return f"is minimal and ends at any of " \
			       f"{len(placement.delimiters)} delimiters, so which byte " \
			       "to write after it is not stated"
		if not placement.delimiter_consumed:
			return "is minimal and does not consume its delimiter, so the " \
			       "byte that ends it belongs to the member after it"
		ending = placement.delimiters[0]
		if len(ending) != 1:
			return "ends at a multi-byte delimiter"
		if ending[0] in _digit_bytes(placement.radix or 10):
			# Nothing else keeps a `[minimal]` field unambiguous: the digits
			# stop at the first byte that is not one, so a delimiter that IS
			# one cannot be found.
			return f"ends at `{chr(ending[0])}`, which is a digit at radix " \
			       f"{placement.radix}"
		if placement.size_max_bits is None:
			return "is minimal with no `max`, so nothing bounds its digits"
	elif placement.array_count is None:
		return "is written in digits with neither a width nor `[minimal]`"
	return None


def _delimited_refusal(placement: Placement) -> str | None:
	"""Why this run cannot be ended by writing its delimiter, or None.

	The delimiter is what says where the run stops, so writing one means
	guaranteeing it does not occur among the bytes. For a run of DIGITS
	that is a property of the radix and `_radix_refusal` settles it
	statically; for arbitrary bytes nothing can be known in advance, so the
	writer scans what the caller gave it and refuses rather than altering
	it. Scanning is the honest half of the trade: escaping would hand back
	a different message from the one that was asked for.

	Measured over the 45 corpus schemas when this was written: of 31
	delimited members in structs with no builder, 23 are a single consumed
	delimiter with no escape, quote or trim -- so the shapes refused below
	are the tail rather than the body.
	"""
	# `before` first, and the order is the whole diagnosis. Such a run
	# writes NO terminator -- the delimiter belongs to whatever follows --
	# so the number of delimiters it lists does not bear on it at all, and
	# asked the other way round a `before` run with several was told "which
	# byte to write after it is not stated" when no byte is its to write.
	#
	# Reported by hull 2026-10-09, who read this function rather than its
	# output: their `symbol` lists eight. `sexpr.symbol.name` is the corpus
	# instance, and the two tests over it and `http.header_field.value` are
	# what separate these two checks.
	if not placement.delimiter_consumed:
		return "is a `before` run, so nothing it writes ends it: whether " \
		       "the bytes after it begin with a delimiter is a property of " \
		       "what is written next, which composes it rather than being " \
		       "part of it"
	if len(placement.delimiters) > 1:
		return f"ends at any of {len(placement.delimiters)} delimiters, so " \
		       "which byte to write after it is not stated"
	if len(placement.delimiters[0]) != 1:
		return "ends at a multi-byte delimiter"
	if placement.delimiter_escape:
		return "escapes its delimiter, and escaping would write bytes " \
		       "other than the ones it was given"
	if placement.delimiter_quote:
		return "is quoted, so where it ends depends on a quote this " \
		       "writer does not place"
	if placement.trim_set:
		return "is trimmed, so the bytes it holds are not the bytes it " \
		       "was given"
	return None


def _scalar_refusal(placement: Placement) -> str | None:
	"""Why this scalar's value is not its bytes in the runtime's own order.

	Each of these has a conversion somewhere else in the tree, and a second
	copy here is the fault `owned.py` records against its own BCD: the two
	were self-consistent and wrong together, so a round trip returned
	SITU_OK about a date the bytes did not spell.
	"""
	scalar = placement.scalar
	assert scalar is not None
	if scalar.kind is ScalarKind.BCD or scalar.is_bcd:
		return "is packed BCD, whose value is not its bytes"
	if scalar.kind is ScalarKind.FLOAT:
		return "is floating point"
	if scalar.kind in (ScalarKind.SFIXED, ScalarKind.UFIXED):
		return "is fixed point, whose value is scaled"
	if placement.radix is not None:
		return _radix_refusal(placement)
	if placement.scaled:
		return "is scaled, so its value is not its bits"
	if placement.varint:
		return "is a varint, whose width depends on its value"
	if scalar.bits % BITS_PER_BYTE or scalar.bits not in WORD_WIDTHS:
		return f"is {scalar.bits} bits wide, so it shares a byte with its " \
		       "neighbours and cannot be appended on its own"
	if placement.offset_bits is not None \
			and placement.offset_bits % BITS_PER_BYTE:
		return "does not start on a byte boundary"
	return None


def _member_refusal(placement: Placement) -> str | None:
	"""Why this member is beyond the first writer, by name.

	Everything here is a real construct that a later increment may reach.
	Refusing by name is the point: 0032 asks a rung to be absent rather
	than defaulted where the schema states something it cannot hold to.
	"""
	if placement.kind == "checksum" or placement.tag_covers:
		return "is a tag or checksum, which this writer does not compute"
	if placement.kind == "variant":
		return "is a variant, so which arm to write is the caller's choice " \
		       "and not yet expressible"
	if placement.kind not in ("field", "reserved", "preamble"):
		return f"is a {placement.kind}"
	if placement.codec is not None:
		return "passes through a codec"
	if placement.sealed_by or placement.sealed_nonce or placement.sealed_key:
		return "is sealed, and sealing is rung 3's"
	if placement.index_table is not None:
		return "is an index table"
	if placement.delimiters and placement.radix is None:
		return _delimited_refusal(placement)
	if placement.repeat_while is not None:
		return "repeats while a condition holds"
	if placement.pad_to is not None:
		return "pads to an alignment"
	if placement.pad_bounds is not None:
		return "is a length-hiding pad, so how long to make it is a " \
		       "decision rather than a fact the schema states"
	if placement.parameter:
		return "comes from a struct argument"
	if placement.remaining_cap:
		return "takes whatever is left, so its length is the frame's"
	if placement.size_expr is not None:
		return f"is sized by the expression `{placement.size_expr}`, which " \
		       "this writer cannot invert"
	return None


def _value_part(role: str, placement: Placement, local: str,
		sizes: Placement | None = None) -> Part:
	"""One member whose value the writer puts down, in bits or in digits."""
	scalar = placement.scalar
	assert scalar is not None
	if placement.radix is None:
		return Part(role, placement, local,
		            scalar.bits // BITS_PER_BYTE, sizes=sizes)

	# A radix member's WIDTH is its digits, not its value's width: `decimal
	# u32` is a u32 worth of value in up to ten bytes of wire. A minimal one
	# has no fixed width at all, so it carries none.
	assert placement.size_max_bits is not None or not placement.radix_minimal
	most = ((placement.size_max_bits or 0) // BITS_PER_BYTE
	        if placement.radix_minimal else placement.array_count or 0)
	return Part(role, placement, local,
	            0 if placement.radix_minimal else most,
	            sizes=sizes, radix=placement.radix,
	            minimal=placement.radix_minimal,
	            delimiter=(placement.delimiters[0]
	                       if placement.radix_minimal else b""),
	            digits=most)


def packed(placement: Placement) -> bool:
	"""Whether this member shares a byte rather than owning whole ones.

	Either because it is not a whole number of bytes wide, or because it
	does not start on a byte boundary. `owned.py` splits its writer on the
	same question and this is the same answer, which is why the ORDER
	comes from `traverse.bit_extractor` here too: two derivations of which
	way the bits run is how a little-endian `u24` came to be read one way
	and written the other (26.285).
	"""
	if placement.scalar is None or placement.size_bits is None:
		return False
	if placement.offset_bits is None:
		return False
	return bool(placement.size_bits % BITS_PER_BYTE
	            or placement.offset_bits % BITS_PER_BYTE)


def _packed_groups(members: list[Placement]
		) -> dict[int, tuple[int, int, int]]:
	"""For each packed member, where it sits in its group and how big that
	group is: `(bit within the group, group bytes, index of the first)`.

	A group is a maximal run of consecutive packed members. It has to start
	on a byte boundary and span whole bytes, because the writer zeroes
	those bytes and then sets bits inside them -- a group that straddled a
	boundary would need the byte before it back, which append-only does not
	have.
	"""
	found: dict[int, tuple[int, int, int]] = {}
	index = 0
	while index < len(members):
		if not packed(members[index]):
			index += 1
			continue
		last = index
		while last + 1 < len(members) and packed(members[last + 1]):
			last += 1
		first_bit = members[index].offset_bits or 0
		end_bit = ((members[last].offset_bits or 0)
		           + (members[last].size_bits or 0))
		if first_bit % BITS_PER_BYTE == 0 \
				and (end_bit - first_bit) % BITS_PER_BYTE == 0:
			span = (end_bit - first_bit) // BITS_PER_BYTE
			for at in range(index, last + 1):
				found[at] = ((members[at].offset_bits or 0) - first_bit,
				             span, index)
		index = last + 1
	return found


def _arm_placement(struct: ResolvedStruct, path: str) -> Placement | None:
	"""The placement an arm selects, which is an entry under the variant."""
	for entry in struct.entries:
		if entry.placement.path == path:
			return entry.placement
	return None


def hands_over(header: str, name: str, prefix: str,
		structs: Mapping[str, ResolvedStruct] | None) -> bool:
	"""Whether a `name` can be passed as bytes the writer verifies.

	Two conditions, and the second needs the schema rather than the
	header: it must have a `_required`, and it must not end `before` a
	byte it does not own.
	"""
	if not frames_itself(header, ident(prefix, name)):
		return False
	inner = structs.get(name) if structs is not None else None
	return inner is None or not ends_before_a_byte_it_does_not_own(inner)


def _arm_refusal(placement: Placement, header: str, prefix: str,
		structs: Mapping[str, ResolvedStruct] | None) -> str | None:
	"""Why this arm's content cannot be written.

	An arm holds one member, so what can be written is what can be written
	anywhere: a whole-byte scalar, or a struct whose own bytes state its
	extent. The second is `frames_itself`'s question again, asked of the
	arm rather than of a nested field.
	"""
	if placement.type_name and hands_over(header, placement.type_name,
	                                      prefix, structs):
		return None
	if placement.scalar is not None and placement.array_count is None \
			and placement.type_name not in (structs or {}):
		return _scalar_refusal(placement)
	if placement.type_name:
		return f"holds a `{placement.type_name}`, which cannot be handed " \
		       "over as bytes: either its own bytes do not state its " \
		       "extent, or it ends `before` a byte it does not own, so " \
		       "the writer cannot tell one from a longer blob"
	return "holds something this writer cannot place"


def _arm_part(struct: ResolvedStruct, placement: Placement, header: str,
		prefix: str,
		structs: Mapping[str, ResolvedStruct] | None) -> Part:
	"""The arm's content: bytes the caller built, or one scalar."""
	# The arm's own name through `bare_name`, not a fragment chopped out of
	# the local: splitting `body_write_register` on underscores gave
	# `register`, which is a C keyword, and modbus would not compile. Fourth
	# instance today of one class -- a name that reaches a parameter has to
	# go through `bare_name`, and every instance came from COMPUTING a
	# fragment instead of asking for the name.
	local = bare_name(placement.name or "body")
	if placement.type_name and hands_over(header, placement.type_name,
	                                      prefix, structs):
		return Part(NESTED, placement, local,
		            inner=ident(prefix, placement.type_name))
	return _value_part(SCALAR, placement, local)


def arms(struct: ResolvedStruct) -> list[Arm]:
	"""The cases of this struct's variant, or nothing where it has none."""
	variant = next((held for held in traverse.own_members(struct)
	                if held.kind == "variant"), None)
	return list(variant.arm_cases or ()) if variant is not None else []


def arm_name(case: Arm) -> str:
	"""What to call the builder for this arm.

	The arm's own member name, which is what the schema called it, so a
	reader of the header and a reader of the schema see one word. A default
	arm selecting nothing has no member to name it after.
	"""
	member = case.member
	if member:
		return str(member).rsplit(".", 1)[-1]
	return "default"


def plan(struct: ResolvedStruct, header: str = "",
		prefix: str = "situ", arm: Arm | None = None,
		structs: Mapping[str, ResolvedStruct] | None = None
		) -> tuple[list[Part], str | None]:
	"""What the writer does for each member, or the first reason it cannot.

	The order is the schema's, which is what makes the pass forward-only.

	`arm` is which case of a variant this plan writes. A variant needs one
	function per arm rather than a parameter saying which, because the
	discriminant is then **decided by the function the caller chose** and
	cannot disagree with the body -- the same argument as computing a size
	field rather than asking for it.
	"""
	# A register is a bus transaction rather than bytes in a buffer and gets
	# an entirely different API (15.1), so there is no view to validate
	# through and nothing to append into. A struct that is not a whole
	# number of bytes has no accessors at all. Both are read from the layout
	# where `emit.py` reads them, rather than from the target directive: one
	# witness, and it is the one that decided what was emitted.
	if struct.layout.register is not None:
		return [], "is a register, which is a bus transaction rather than " \
		           "bytes in a buffer"
	if not struct.layout.is_byte_sized:
		return [], f"is {struct.layout.size_bits} bits, not a whole number " \
		           "of bytes, so no accessors exist to read it back"

	refusals = traverse.append_only_refusals(struct)
	if refusals:
		return [], refusals[0]

	members = traverse.own_members(struct)
	if not members:
		return [], "has no members to write"

	# A size field is the writer's to compute, so it must be known before
	# the loop reaches it -- and `sized_by` names it from the run, which is
	# the member that comes later.
	by_name = {held.name: held for held in members}
	measures: dict[str, Placement] = {}
	for held in members:
		if not held.sized_by or held.sized_by not in by_name:
			# `sized_by` also carries `remaining`, which names no member:
			# the run takes the rest of the frame, and since the builder
			# decides the frame, the length the caller gives IS the rest.
			continue
		if held.sized_by in measures:
			# Two runs measured by one field would make the writer compute
			# one value for two lengths, and the last one to be planned
			# would win silently.
			return [], f"`{held.sized_by}` is the stated size of both " \
			           f"`{measures[held.sized_by].name}` and " \
			           f"`{held.name}`, so one value cannot say both"
		measures[held.sized_by] = held

	# Which member the variant switches on, and what the chosen arm says it
	# must hold. A `peek`ed discriminant is not written at all: a peek does
	# not consume, so those bytes belong to the arm that follows and
	# writing them here would write them twice.
	variant = next((held for held in members if held.kind == "variant"), None)
	chooses = variant.discriminant if variant is not None else None
	claimed = tuple(sorted({case.value for case in (variant.arm_cases or ())
	                        if case.value is not None})
	                ) if variant is not None else ()

	groups = _packed_groups(members)

	parts: list[Part] = []
	for index, held in enumerate(members):
		local = _local(struct, held)

		if packed(held):
			if index not in groups:
				return [], f"`{traverse.local_name(struct, held)}` packs " \
				           "into a group that does not start on a byte " \
				           "boundary or does not span whole bytes"
			bit_at, span, first = groups[index]
			if held.kind not in ("field", "reserved"):
				return [], f"`{traverse.local_name(struct, held)}` is a " \
				           f"packed {held.kind}"
			assert held.scalar is not None
			if held.scalar.is_bcd or held.radix is not None \
					or held.scaled:
				return [], f"`{traverse.local_name(struct, held)}` is " \
				           "packed and its value is not its bits"
			parts.append(Part(BITS, held, local, span,
			                  bit_at=bit_at, opens=index == first,
			                  closes=(index + 1 not in groups
			                          or groups[index + 1][2] != first)))
			continue

		if held.peek:
			# Reads bytes that belong to what comes after it, so it writes
			# nothing and asks for nothing. The arm carries them -- and
			# therefore carries the check that they say what this arm is:
			# see where the arm part is built.
			if (held.name == chooses and arm is not None
					and held.size_bits != BITS_PER_BYTE):
				return [], f"`{traverse.local_name(struct, held)}` is a " \
				           f"peeked {held.size_bits}-bit discriminant, and " \
				           "this writer checks an arm's first BYTE against " \
				           "the case it was called for"
			parts.append(Part(PEEKED, held, local))
			continue

		if held.kind == "variant":
			if arm is None:
				return [], f"`{traverse.local_name(struct, held)}` is a " \
				           "variant, so which arm to write is the caller's " \
				           "choice and is said by calling one of the " \
				           "per-arm builders"
			if arm.member is None:
				# An arm that selects nothing, which is how a schema spells
				# an absent body. Zero bytes, so there is nothing to emit.
				continue
			inner = _arm_placement(struct, str(arm.member))
			if inner is None:
				return [], f"`{arm.member}` is not a member of this struct"
			why = _arm_refusal(inner, header, prefix, structs)
			if why:
				return [], f"`{arm.member}` {why}"
			part = _arm_part(struct, inner, header, prefix, structs)
			# Where the discriminant is peeked, nothing was written for it
			# and `_validate` cannot help: bytes beginning with another
			# arm's kind are a VALID message of that other arm, so a
			# caller who asked for this one would be told SITU_OK about
			# something else. The arm's first byte is therefore checked
			# against the case this builder is for -- or, for the default
			# arm, against every case it is not.
			disc = next((other for other in members
			             if other.name == chooses), None)
			if disc is not None and disc.peek:
				value = arm.value
				part.value  = -1 if value is None else int(value)
				part.others = claimed
			parts.append(part)
			continue

		if chooses is not None and held.name == chooses:
			if arm is None:
				return [], "is the discriminant of a variant"
			if arm.value is None:
				# The default arm names no value, so the caller supplies one
				# -- and it must not be a value a named arm claims, or the
				# message reads as that arm instead.
				why = _scalar_refusal(held)
				if why:
					return [], f"`{traverse.local_name(struct, held)}` {why}"
				parts.append(_value_part(SCALAR, held, local))
				parts[-1].others = claimed
				continue
			why = _scalar_refusal(held)
			if why:
				return [], f"`{traverse.local_name(struct, held)}` {why}"
			part = _value_part(ARMTAG, held, local)
			part.value = int(arm.value)
			parts.append(part)
			continue
		runs  = traverse.pinned_runs(held)
		size  = _whole_bytes(held)

		if runs and len(runs) == 1 and size is not None \
				and len(runs[0]) == size:
			parts.append(Part(LITERAL, held, local, size, runs[0]))
			continue

		if held.kind in ("reserved", "preamble"):
			# Reserved bytes are a constraint rather than a value (8.8) and
			# have no accessor, so there is nothing for a caller to pass.
			# Zero is not a choice either: the content policy is
			# `must_be_zero` or nothing, so zero is the one value that is
			# required where it is stated and refused nowhere.
			if size is None:
				return [], f"`{traverse.local_name(struct, held)}` is " \
				           "reserved and has no stated length"
			parts.append(Part(ZERO, held, local, size))
			continue

		why = _member_refusal(held)
		if why:
			return [], f"`{traverse.local_name(struct, held)}` {why}"

		if held.name in measures:
			if held.scalar is None:
				return [], f"`{local}` sizes " \
				           f"`{measures[held.name].name}` and is not a scalar"
			why = _scalar_refusal(held)
			if why:
				return [], f"`{traverse.local_name(struct, held)}` {why}"
			run = measures[held.name]
			part = _value_part(SIZE, held, local, sizes=run)
			if not run.element_bits and run.type_name:
				# ble's `num` and edges' `count` are documented element
				# counts -- "a LITERAL count of elements that have no
				# single size" -- so a struct run's size field is not the
				# byte length a span's is, and writing that would be a
				# message nothing can read.
				part.inner = ident(prefix, run.type_name)
			parts.append(part)
			continue

		# A delimiter decides a member's extent, so a delimited member is a
		# RUN -- except where its digits make it a value, which is the radix
		# case. `u8 chars[] until " "` carries a u8 scalar and no count,
		# which is exactly the shape this branch would take for a
		# single-byte write; letting it reported four more buildable
		# structs with no delimited part among them, which is what a count
		# says when the thing it counted went somewhere else.
		#
		# Keying on `delimiters` alone is sound because the parser refuses
		# the other reading -- *`length` is a single value, so a delimiter
		# has nothing to bound* -- so there is no delimited scalar for this
		# to mistake a run for.
		ends_at_byte = bool(held.delimiters) and held.radix is None
		if held.scalar is not None and not traverse.data_sized(held) \
				and not ends_at_byte \
				and (held.array_count is None or held.radix is not None):
			why = _scalar_refusal(held)
			if why:
				return [], f"`{traverse.local_name(struct, held)}` {why}"
			parts.append(_value_part(SCALAR, held, local))
			continue

		# A byte run: fixed where the schema counted it, caller-measured
		# where another member holds the count. Anything wider than a byte
		# per element would need the element's byte order applied per
		# element, which is a loop this increment does not write.
		element = held.element_bits or 0
		if element != BITS_PER_BYTE:
			# A single nested struct is not a run of them, and calling it
			# one was a wrong diagnosis for 8 of the 16 members refused
			# here -- `mqtt_string topic;` and `value held;` are fields.
			# Same class as the `before` misdiagnosis in 26.612: the
			# verdict was right and the reason was about something else.
			repeats = bool(held.array_count is not None or held.sized_by
			               or held.size_expr or held.delimiters
			               or held.repeat_while is not None
			               or held.remaining_cap)
			if held.type_name and hands_over(header, held.type_name,
			                                 prefix, structs):
				if not repeats:
					parts.append(Part(NESTED, held, local,
					                  inner=ident(prefix, held.type_name)))
					continue
				# Every struct run reduces to one walk: a fixed count, a
				# count field, the rest of the frame, or a delimiter.
				if (held.size_expr or held.repeat_while is not None
						or len(held.delimiters) > 1
						or (held.delimiters
						    and (len(held.delimiters[0]) != 1
						         or not held.delimiter_consumed))):
					return [], f"`{traverse.local_name(struct, held)}` is " \
					           f"a run of `{held.type_name}` whose end " \
					           "this writer cannot state"
				parts.append(Part(REPEAT, held, local,
				                  inner=ident(prefix, held.type_name),
				                  delimiter=(held.delimiters[0]
				                             if held.delimiters else b""),
				                  digits=held.array_count or 0))
				continue
			if not repeats and held.type_name:
				return [], f"`{traverse.local_name(struct, held)}` is a " \
				           f"nested `{held.type_name}`, whose extent its " \
				           "own bytes do not determine, so the writer " \
				           "cannot tell one from a longer blob"
			spelt = (f"a run of {element}-bit elements" if element
			         else f"a run of `{held.type_name}`")
			return [], f"`{traverse.local_name(struct, held)}` is {spelt}, " \
			           "not of bytes"
		if held.array_count is not None:
			parts.append(Part(RUN, held, local, held.array_count))
			continue
		if held.sized_by:
			parts.append(Part(SPAN, held, local))
			continue
		if held.delimiters:
			# `_delimited_refusal` has already settled the shape; what is
			# left is the length, which the caller gives and `[max]` bounds.
			parts.append(Part(ENDED, held, local,
			                  delimiter=held.delimiters[0],
			                  digits=held.delimiter_cap or 0))
			continue
		return [], f"`{traverse.local_name(struct, held)}` has no stated " \
		           "length, so nothing says when the writer should stop"

	# One more pass for the same reason, over names that are legal C and
	# still cannot stand where they are: the writer's own locals, and each
	# other -- a `value` span emits a `value_len`, which a member of that
	# name would then redefine. `image.situ` has a member called `cap`, and
	# one collision produced two errors, a redefinition and an unused
	# parameter. Declaration order decides, so the earlier member keeps its
	# spelling.
	taken = set(FIXTURES)
	for part in parts:
		local = part.local
		while local in taken or f"{local}_len" in taken:
			local += "_"
		part.local = local
		taken.add(local)
		if part.role in MEASURED:
			taken.add(f"{local}_len")

	if not any(part.role in CALLER_SUPPLIED for part in parts):
		# Every byte is a fact the schema states, so there is nothing for a
		# caller to supply and `_encode` of a constant is not a builder.
		return [], "is entirely literal, so there is nothing to build"
	return parts, None


def buildable(resolved: ResolvedSchema, header: str = "",
		prefix: str = "situ") -> list[ResolvedStruct]:
	found = []
	for struct in resolved.structs.values():
		cases = arms(struct)
		if cases:
			if any(plan(struct, header, prefix, case,
			            resolved.structs)[0] for case in cases):
				found.append(struct)
		elif plan(struct, header, prefix, None, resolved.structs)[0]:
			found.append(struct)
	return found


def refusals(resolved: ResolvedSchema, header: str = "",
		prefix: str = "situ") -> list[tuple[str, str]]:
	"""Every struct with no builder, and why -- by name, on stderr."""
	found = []
	for name, struct in resolved.structs.items():
		cases = arms(struct)
		if not cases:
			parts, why = plan(struct, header, prefix, None,
			                  resolved.structs)
			if not parts and why:
				found.append((name, why))
			continue
		# A variant is reported per arm, because the useful answer is which
		# arms can be written rather than whether the struct can.
		for case in cases:
			parts, why = plan(struct, header, prefix, case,
			                  resolved.structs)
			if not parts and why:
				found.append((f"{name}.{arm_name(case)}", why))
	return found


def _signature(struct: ResolvedStruct, parts: list[Part], prefix: str,
		enums: Mapping[str, object], suffix: str = "") -> list[str]:
	name = ident(prefix, struct.name, "build", suffix)
	args = [f"uint8_t *{OUT}", f"uint32_t {CAP}"]
	for part in parts:
		if part.role == BITS:
			if part.placement.kind == "reserved":
				continue	# zeroed by the group, not a value to pass
			args.append(f"{_ctype(part.placement, prefix, enums)} "
			            f"{part.local}")
		elif part.role == SCALAR:
			args.append(f"{_ctype(part.placement, prefix, enums)} "
			            f"{part.local}")
		elif part.role == RUN:
			args.append(f"const uint8_t *{part.local}")
		elif part.role in MEASURED:
			args.append(f"const uint8_t *{part.local}")
			args.append(f"uint32_t {part.local}_len")
	args.append(f"uint32_t *{WROTE}")

	lines = [f"static inline situ_err_t {name}("]
	for i, arg in enumerate(args):
		lines.append(f"\t\t{arg}" + ("," if i + 1 < len(args) else ")"))
	return lines


def _store(part: Part, value: str) -> list[str]:
	"""Put one whole-byte scalar at the cursor."""
	scalar = part.placement.scalar
	assert scalar is not None
	width = storage_width(scalar.bits)
	if scalar.bits == BITS_PER_BYTE:
		return [f"\t{OUT}[{AT}] = (uint8_t){value};"]
	end = "le" if part.placement.endian is ast.Endian.LITTLE else "be"
	return [f"\tsitu_put_{end}{scalar.bits}({OUT} + {AT},",
	        f"\t              (uint{width}_t){value});"]


def _value(part: Part, value: str, local: str) -> list[str]:
	"""Put one member's value down and advance the cursor.

	Both roles that take a value come through here -- the caller's scalar
	and the size field computed from a run -- because a radix reached by one
	and not the other is how `atom.length`, which is both, would get the
	binary writer.
	"""
	if not part.radix:
		return [*_room(f"{part.bytes}u"),
		        *_store(part, value),
		        f"\t{AT} += {part.bytes}u;"]

	scalar = part.placement.scalar
	assert scalar is not None
	if not part.minimal:
		return [
			f"\t/* {local}: {part.digits} digit(s) at radix {part.radix}, "
			"leading zeros and all. */",
			*_room(f"{part.digits}u"),
			f"\tif (situ_format_uint({OUT} + {AT}, {part.digits}u, "
			f"{part.radix}u,",
			f"\t                     (uint64_t){value}) != 0) {{",
			"\t\treturn SITU_ERR_CONSTRAINT;",
			"\t}",
			f"\t{AT} += {part.digits}u;",
		]

	ending = part.delimiter[0]
	return [
		f"\t/* {local}: minimal digits at radix {part.radix}, then the "
		f"`{chr(ending)}` that ends them. */",
		"\t{",
		f"\t\tuint32_t {DIGITS} = 1u;",
		f"\t\tuint64_t {SCRATCH} = (uint64_t){value};",
		"",
		# The count rather than a second formatter: `situ_format_uint` pads
		# to the width it is given, so the only thing missing for `[minimal]`
		# is the width.
		f"\t\twhile ({SCRATCH} >= {part.radix}u) {{",
		f"\t\t\t{SCRATCH} /= {part.radix}u;",
		f"\t\t\t{DIGITS}++;",
		"\t\t}",
		f"\t\tif ({DIGITS} > {part.digits}u) {{",
		"\t\t\treturn SITU_ERR_CONSTRAINT;",
		"\t\t}",
		f"\t\tif ({CAP} - {AT} < {DIGITS} + 1u) {{",
		"\t\t\treturn SITU_ERR_BOUNDS;",
		"\t\t}",
		f"\t\tif (situ_format_uint({OUT} + {AT}, {DIGITS}, {part.radix}u,",
		f"\t\t                     (uint64_t){value}) != 0) {{",
		"\t\t\treturn SITU_ERR_CONSTRAINT;",
		"\t\t}",
		f"\t\t{AT} += {DIGITS};",
		f"\t\t{OUT}[{AT}] = 0x{ending:02X}u;",
		f"\t\t{AT} += 1u;",
		"\t}",
	]


def _walk(part: Part, local: str, count: bool) -> list[str]:
	"""Step through a run of inner structs the caller built.

	Every shape a struct run takes reduces to this, which is why there is
	one of them rather than four: a fixed count, a count field, a run that
	takes the rest of the frame, and a run ended by a delimiter all need
	the same question answered -- are these bytes a whole number of
	elements -- and the element's own `_required` answers it exactly.

	**What makes the loop terminate is the zero-progress check**, and it is
	not hypothetical: `situ_nothing_required` sets `*need = 0`, so a run of
	a zero-byte struct would spin for ever. A generated loop is somebody
	else's unattended program, so it says what stops it.

	**The delimiter check belongs INSIDE the loop and cannot be a scan.**
	A byte run refuses a delimiter anywhere in it; a struct run must not,
	because a `)` inside a nested element is legitimate and the reader
	walks structurally. What breaks it is a delimiter at an ELEMENT
	BOUNDARY, where the reader would stop early -- so the check is at each
	boundary the walk reaches, which is exactly where the reader looks.
	"""
	ending = part.delimiter[0] if part.delimiter else None
	lines = [
		f"\t/* {local}: a run of `{part.placement.type_name}` the caller",
		"\t * built. Walked with the element's own `_required`, because",
		"\t * only it can say where one ends -- and the walk is what",
		"\t * establishes these bytes are a whole number of them. */",
		# Before the walk, not with the copy further down: the loop reads
		# `x[scan]` and calls `_required(x + scan, ...)`, so a null pointer
		# with a non-zero length would be dereferenced here first.
		f"\tif ({part.local}_len != 0u && {part.local} == NULL) {{",
		"\t\treturn SITU_ERR_BOUNDS;",
		"\t}",
		"\t{",
		f"\t\tuint32_t {SCAN} = 0u;",
		"",
		f"\t\twhile ({SCAN} < {part.local}_len) {{",
		f"\t\t\tuint32_t {NEED} = 0u;",
		"",
	]
	if ending is not None:
		lines += [
			f"\t\t\t/* A `{chr(ending)}` here is where the reader would",
			"\t\t\t * stop, so an element may hold one and may not",
			"\t\t\t * BEGIN with one. */",
			f"\t\t\tif ({part.local}[{SCAN}] == 0x{ending:02X}u) {{",
			"\t\t\t\treturn SITU_ERR_CONSTRAINT;",
			"\t\t\t}",
		]
	lines += [
		f"\t\t\tif ({part.inner}_required({part.local} + {SCAN},",
		f"\t\t\t                      {part.local}_len - {SCAN},",
		f"\t\t\t                      &{NEED}) != SITU_OK) {{",
		"\t\t\t\treturn SITU_ERR_CONSTRAINT;",
		"\t\t\t}",
		f"\t\t\tif ({NEED} == 0u) {{",
		"\t\t\t\t/* What stops the loop. A zero-byte element would",
		"\t\t\t\t * otherwise never advance it. */",
		"\t\t\t\treturn SITU_ERR_CONSTRAINT;",
		"\t\t\t}",
		f"\t\t\t{SCAN} += {NEED};",
		*([f"\t\t\t{COUNT}++;"] if count else []),
		"\t\t}",
		"\t}",
	]
	return lines


def _room(size: str) -> list[str]:
	"""The capacity check before a write of `size` bytes.

	`{CAP} - {AT}` rather than `{AT} + size > {CAP}`: the cursor never
	passes the capacity, so the subtraction cannot wrap, while the addition
	can on a length the caller supplied.
	"""
	return [f"\tif ({CAP} - {AT} < {size}) {{",
	        "\t\treturn SITU_ERR_BOUNDS;",
	        "\t}"]


def _one(struct: ResolvedStruct, prefix: str,
		enums: Mapping[str, object], header: str,
		arm: Arm | None = None,
		structs: Mapping[str, ResolvedStruct] | None = None) -> list[str]:
	parts, _ = plan(struct, header, prefix, arm, structs)
	lines = [
		f"/** Build a {struct.name} into `{OUT}`, one member at a time in",
		" *  schema order, and validate the result before reporting it.",
		*([f" *",
		   f" *  This one writes the `{arm_name(arm)}` arm. Which arm is",
		   " *  said by which builder you call, so the discriminant is",
		   " *  this function's to write and cannot disagree with the",
		   " *  body."] if arm is not None else []),
		" *",
		f" * `*{WROTE}` is set only on success. A size field is computed",
		" * from the run it measures rather than supplied, so a message",
		" * cannot disagree with its own schema.",
		" *",
		f" * SITU_OK             built; the first `*{WROTE}` bytes are a",
		f" *                     complete {struct.name}",
		f" * SITU_ERR_BOUNDS     `{CAP}` is short of what the members need",
		" * SITU_ERR_CONSTRAINT a run is longer than its size field can say,",
		" *                     or the result does not satisfy the schema",
		" */",
		*_signature(struct, parts, prefix, enums, arm_name(arm) if arm
		            else ""),
		"{",
		f"\tsitu_msg_t  {MSG};",
		f"\tsitu_view_t {VIEW};",
		f"\tsitu_err_t  {ERR};",
		f"\tuint32_t    {AT} = 0;",
		# Only where something reads it, or `-Wunused-but-set-variable`
		# refuses the file: a run that takes the rest of the frame is
		# walked without being counted.
		*([f"\tuint32_t    {COUNT} = 0u;"]
		  if any(part.role == REPEAT and (part.digits or part.inner
		                                  in [other.inner for other in parts
		                                      if other.role == SIZE])
		         for part in parts) else []),
		"",
		f"\tif ({OUT} == NULL || {WROTE} == NULL) {{",
		"\t\treturn SITU_ERR_BOUNDS;",
		"\t}",
		"",
	]

	for part in parts:
		held  = part.placement
		local = traverse.local_name(struct, held)
		if part.role == SIZE and part.inner:
			# The count is not known from a length the caller passed: it is
			# what the walk finds, so the walk happens HERE, at the field
			# that states it, rather than at the run further down.
			assert part.sizes is not None and held.scalar is not None
			run  = _local(struct, part.sizes)
			tally = Part(REPEAT, part.sizes, run, inner=part.inner,
			             delimiter=(part.sizes.delimiters[0]
			                        if part.sizes.delimiters else b""))
			lines += [
				*_walk(tally, traverse.local_name(struct, part.sizes), True),
				f"\t/* {local}: how many the walk found. */",
				*_value(part, COUNT, local),
				"",
			]
		elif part.role == SIZE:
			assert part.sizes is not None and held.scalar is not None
			run   = _local(struct, part.sizes)
			limit = (1 << held.scalar.bits) - 1
			# The value's own width, even where it is written in digits: a
			# `decimal u32` holds what a u32 holds, and `[max]` bounds the
			# digits separately in `_value`. Omitted where a `uint32_t`
			# length cannot exceed it, because a check that cannot fire is
			# noise -- `emit.py` declines `length < 0u` for the same
			# reason -- and `> 4294967295u` on a `uint32_t` reads as a
			# fault in the generator.
			lines += [
				f"\t/* {local}: the length of `{run}`, which the schema "
				"states. */",
				*([f"\tif ({run}_len > {limit}u) {{",
				   "\t\treturn SITU_ERR_CONSTRAINT;",
				   "\t}"] if held.scalar.bits < 32 else []),
				*_value(part, f"{run}_len", local),
				"",
			]
		elif part.role == SCALAR:
			lines += [
				# A value a named arm claims would make the message read as
				# that arm instead, so the default builder refuses it. The
				# named builders are where those values are written.
				*([f"\tif ({' || '.join(f'{part.local} == {v}u' for v in part.others)}) {{",
				   "\t\treturn SITU_ERR_CONSTRAINT;",
				   "\t}"] if part.others else []),
				*_value(part, part.local, local),
				"",
			]
		elif part.role == ZERO:
			lines += [
				f"\t/* {local}: {part.bytes} reserved byte(s). Zero is what",
				"\t * a `must_be_zero` policy requires and what no other",
				"\t * policy refuses. */",
				*_room(f"{part.bytes}u"),
				f"\tmemset({OUT} + {AT}, 0, {part.bytes}u);",
				f"\t{AT} += {part.bytes}u;",
				"",
			]
		elif part.role == LITERAL:
			spelt = ", ".join(f"0x{byte:02X}u" for byte in part.literal)
			lines += [
				f"\t/* {local}: {part.bytes} byte(s) the schema spells out. */",
				*_room(f"{part.bytes}u"),
				"\t{",
				f"\t\tstatic const uint8_t situ_fixed[{part.bytes}] = "
				f"{{ {spelt} }};",
				"",
				f"\t\tmemcpy({OUT} + {AT}, situ_fixed, {part.bytes}u);",
				"\t}",
				f"\t{AT} += {part.bytes}u;",
				"",
			]
		elif part.role == RUN:
			lines += [
				f"\tif ({part.local} == NULL) {{",
				"\t\treturn SITU_ERR_BOUNDS;",
				"\t}",
				*_room(f"{part.bytes}u"),
				f"\tmemcpy({OUT} + {AT}, {part.local}, {part.bytes}u);",
				f"\t{AT} += {part.bytes}u;",
				"",
			]
		elif part.role == BITS:
			assert held.scalar is not None
			order = traverse.bit_extractor(held.scalar, held)
			width = held.size_bits or 0
			lines += [
				*([f"\t/* {part.bytes} byte(s) of packed fields: zeroed,",
				   "\t * then each one set in place. The reserved bits",
				   "\t * among them are what the zeroing is for. */",
				   *_room(f"{part.bytes}u"),
				   f"\tmemset({OUT} + {AT}, 0, {part.bytes}u);"]
				  if part.opens else []),
			]
			if held.kind != "reserved":
				# `situ_bits_set_*` MASKS the value, so a field given more
				# than it can hold would be written truncated -- a valid
				# message saying something else, which `_validate` cannot
				# object to. Refused instead.
				lines += [
					f"\t/* {local}: {width} bits at {part.bit_at}. */",
					f"\tif ((uint64_t){part.local} > {(1 << width) - 1}u) {{",
					"\t\treturn SITU_ERR_CONSTRAINT;",
					"\t}",
					f"\tsitu_bits_set_{order}({OUT} + {AT}, "
					f"{part.bit_at}u, {width}u,",
					f"\t                    (uint64_t){part.local});",
				]
			if part.closes:
				lines += [f"\t{AT} += {part.bytes}u;", ""]
		elif part.role == PEEKED:
			lines += [
				f"\t/* {local}: peeked, so it does not consume: the bytes",
				"\t * it reads belong to what follows and are written",
				"\t * there. Nothing to write here. */",
				"",
			]
		elif part.role == ARMTAG:
			lines += [
				f"\t/* {local}: {part.value}, which is this arm's case. The",
				"\t * arm is chosen by which builder was called, so the",
				"\t * discriminant cannot disagree with the body. */",
				*_value(part, f"{part.value}u", local),
				"",
			]
		elif part.role == REPEAT:
			counted = part.digits > 0
			walked  = any(other.role == SIZE and other.inner == part.inner
			              for other in parts)
			lines += [
				# Walked at the count field already, where the answer was
				# needed; walking twice would be two measurements of one
				# thing and they would have to agree.
				*([] if walked else _walk(part, local, counted)),
				*([f"\tif ({COUNT} != {part.digits}u) {{",
				   "\t\treturn SITU_ERR_CONSTRAINT;",
				   "\t}"] if counted else []),
				*_room(f"{part.local}_len"),
				f"\tif ({part.local}_len != 0u) {{",
				f"\t\tif ({part.local} == NULL) {{",
				"\t\t\treturn SITU_ERR_BOUNDS;",
				"\t\t}",
				f"\t\tmemcpy({OUT} + {AT}, {part.local}, "
				f"{part.local}_len);",
				"\t}",
				f"\t{AT} += {part.local}_len;",
				*([] if not part.delimiter else [
					f"\t/* and the `{chr(part.delimiter[0])}` that ends "
					"the run. */",
					*_room("1u"),
					f"\t{OUT}[{AT}] = 0x{part.delimiter[0]:02X}u;",
					f"\t{AT} += 1u;",
				]),
				"",
			]
		elif part.role == NESTED:
			lines += [
				*([] if not part.others and part.value <= 0 else [
					f"\t/* {local}: its first byte is the peeked",
					"\t * discriminant, so it has to say which arm this",
					"\t * is -- bytes beginning with another arm's kind",
					"\t * are a valid message of THAT arm, which",
					"\t * `_validate` would accept. */",
					f"\tif ({part.local}_len == 0u) {{",
					"\t\treturn SITU_ERR_CONSTRAINT;",
					"\t}",
					*([f"\tif ({part.local}[0] != {part.value}u) {{"]
					  if part.value >= 0 else
					  [f"\tif ({' || '.join(f'{part.local}[0] == {v}u' for v in part.others)}) {{"]),
					"\t\treturn SITU_ERR_CONSTRAINT;",
					"\t}",
				]),
				f"\t/* {local}: one whole `{held.type_name}`, which the",
				"\t * caller built. Its extent is its own to state, so the",
				"\t * writer ASKS rather than trusting the length it was",
				"\t * given: three bytes too many shift every member after",
				"\t * it, and whether `_validate` then refuses depends on",
				"\t * what those three bytes happen to say. */",
				f"\tif ({part.local}_len != 0u && {part.local} == NULL) {{",
				"\t\treturn SITU_ERR_BOUNDS;",
				"\t}",
				"\t{",
				f"\t\tuint32_t {NEED} = 0u;",
				"",
				f"\t\tif ({part.inner}_required({part.local}, "
				f"{part.local}_len,",
				f"\t\t                           &{NEED}) != SITU_OK",
				f"\t\t    || {NEED} != {part.local}_len) {{",
				"\t\t\treturn SITU_ERR_CONSTRAINT;",
				"\t\t}",
				"\t}",
				*_room(f"{part.local}_len"),
				f"\tif ({part.local}_len != 0u) {{",
				f"\t\tif ({part.local} == NULL) {{",
				"\t\t\treturn SITU_ERR_BOUNDS;",
				"\t\t}",
				f"\t\tmemcpy({OUT} + {AT}, {part.local}, "
				f"{part.local}_len);",
				"\t}",
				f"\t{AT} += {part.local}_len;",
				"",
			]
		elif part.role == ENDED:
			ending = part.delimiter[0]
			lines += [
				f"\t/* {local}: the bytes, then the "
				f"`{chr(ending) if 32 < ending < 127 else hex(ending)}` "
				"that ends them.",
				"\t * The delimiter is what says where the run stops, so a",
				"\t * run containing it would read back shorter than it was",
				"\t * written. Refused rather than escaped: escaping writes",
				"\t * bytes other than the ones the caller gave. */",
				*([f"\tif ({part.local}_len > {part.digits}u) {{",
				   "\t\treturn SITU_ERR_CONSTRAINT;",
				   "\t}"] if part.digits else []),
				"\t{",
				f"\t\tuint32_t {SCAN};",
				"",
				f"\t\tfor ({SCAN} = 0u; {SCAN} < {part.local}_len; "
				f"{SCAN}++) {{",
				f"\t\t\tif ({part.local}[{SCAN}] == 0x{ending:02X}u) {{",
				"\t\t\t\treturn SITU_ERR_CONSTRAINT;",
				"\t\t\t}",
				"\t\t}",
				"\t}",
				*_room(f"{part.local}_len"),
				f"\tif ({part.local}_len != 0u) {{",
				f"\t\tif ({part.local} == NULL) {{",
				"\t\t\treturn SITU_ERR_BOUNDS;",
				"\t\t}",
				f"\t\tmemcpy({OUT} + {AT}, {part.local}, "
				f"{part.local}_len);",
				"\t}",
				f"\t{AT} += {part.local}_len;",
				# Two checks rather than one against `_len + 1u`, which can
				# wrap on a length the caller supplied.
				*_room("1u"),
				f"\t{OUT}[{AT}] = 0x{ending:02X}u;",
				f"\t{AT} += 1u;",
				"",
			]
		else:
			lines += [
				*_room(f"{part.local}_len"),
				# A zero-length run is a legal message, and `memcpy` with a
				# null source is undefined even for zero bytes.
				f"\tif ({part.local}_len != 0u) {{",
				f"\t\tif ({part.local} == NULL) {{",
				"\t\t\treturn SITU_ERR_BOUNDS;",
				"\t\t}",
				f"\t\tmemcpy({OUT} + {AT}, {part.local}, {part.local}_len);",
				"\t}",
				f"\t{AT} += {part.local}_len;",
				"",
			]

	lines += [
		"\t/* Constraints are the view's to state and this reuses them",
		"\t * rather than restating them: two checks of one schema is how",
		"\t * they come to disagree. */",
		f"\tsitu_msg_init(&{MSG}, {OUT}, {AT});",
		# A frame's view takes the length the caller has and a fixed
		# struct's does not -- `emit.py` keys that on `is_fixed_size` and so
		# does this. The first draft did not, and it was a compile error
		# rather than a quiet one.
		f"\tif ({ident(prefix, struct.name, 'view')}(&{MSG}, 0, "
		+ ("" if struct.layout.is_fixed_size else f"{AT}, ")
		+ f"&{VIEW}) != SITU_OK) {{",
		"\t\treturn SITU_ERR_BOUNDS;",
		"\t}",
		f"\t{ERR} = {ident(prefix, struct.name, 'validate')}({VIEW});",
		f"\tif ({ERR} != SITU_OK) {{",
		f"\t\treturn {ERR};",
		"\t}",
		"",
		f"\t*{WROTE} = {AT};",
		"\treturn SITU_OK;",
		"}",
		"",
	]
	return lines


def generate(schema: ast.Schema, resolved: ResolvedSchema, basename: str,
		prefix: str = "situ", header: str = "") -> dict[str, str]:
	"""The build header, or nothing where no layout qualifies.

	`header` is the ordinary header this compilation emitted, which is the
	artifact that says which structs have a `_required` -- see
	`frames_itself`. Without it a nested member is refused rather than
	guessed at, so a caller who does not pass it gets less rather than
	something wrong.
	"""
	structs = buildable(resolved, header, prefix)
	if not structs:
		return {}

	guard = macro(prefix, basename, "BUILD_H")
	lines = [
		f"/* Generated by situc {__version__} from {basename}.situ -- do not edit.",
		" *",
		" * Append-only building: the write half of rung 2. Each member goes",
		" * down in schema order into backing you supply, nothing moves once",
		" * written, and no view into the buffer exists while it is being",
		" * filled -- which is why this needs rung 2's permission and",
		" * nothing above it.",
		" *",
		" * A layout that cannot be written forward has no builder here, and",
		" * `situc build` names it. Nothing here allocates. A separate",
		" * header -- a rung adds files rather than changing them (0032).",
		" */",
		"",
		f"#ifndef {guard}",
		f"#define {guard}",
		"",
		"#include <string.h>",
		"",
		"#include \"situ.h\"",
		f"#include \"{basename}.h\"",
		"",
		"#ifdef __cplusplus",
		"extern \"C\" {",
		"#endif",
		"",
	]

	enums = dict(resolved.layout.env.enums)
	known = resolved.structs
	for struct in structs:
		cases = arms(struct)
		if not cases:
			lines.extend(_one(struct, prefix, enums, header, None, known))
			continue
		for case in cases:
			if plan(struct, header, prefix, case, known)[0]:
				lines.extend(_one(struct, prefix, enums, header, case,
				                  known))

	lines += ["#ifdef __cplusplus", "}", "#endif", "",
	          f"#endif /* {guard} */"]

	return {f"{basename}_build.h": "\n".join(lines) + "\n"}
