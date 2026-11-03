"""`gen-checks --target cpp`: the capability map held to the C++ accessors.

**A plain `main` counting failures rather than a framework**, which is the
same decision `cpp/vectors.py` records: the C suite uses cmocka and the C++
one has no dependency to spend, so each check is a function that prints
`FAIL` and increments a counter. `main` returns non-zero if any did.

The arithmetic is not here. Which value a member must hold, where its bytes
land, which arm a message carries and what a probe pattern is all live in
`situc/codegen`, shared with `--target c` and `--target python` -- so the
three backends cannot disagree about the question, only about how C++
spells the answer. That sharing is what made this port the statement
vocabulary rather than the logic, and 26.838 and 26.842 are where the
helpers moved.

The kinds and their reasons are `--target python`'s, which carries the full
account; what is written here is only what C++ does differently.
"""

from __future__ import annotations

from situc import __version__, ast
from situc.codegen import (CHECK_KINDS, PROBE_BYTES, arguments,
                           digit_pattern,
                           passes_unknown, placed_bytes, refuse_parameters,
                           required_pattern, required_run, reserved_policy,
                           selected_arms)
from situc.codegen.c.frame import framed_structs
from situc.capability import Axis
from situc.codegen.c.names import c_name
from situc.layout import BITS_PER_BYTE, Placement
from situc.propagate import Resolved
from situc.resolve import ResolvedSchema, ResolvedStruct
from situc.traverse import (enclosing_arm, has_computable_extent,
                            inside_an_unresolved_arm, own_entries)
from situc.types import ScalarKind

OK = "::situ::rt::err::ok"


def _fail(label: str, why: str) -> list[str]:
	"""The one way this suite reports: print and count."""
	return [f'\t\tstd::printf("FAIL {label}: {why}\\n");',
	        "\t\tsitu_failures++;",
	        "\t\treturn;"]


def _acquire(struct: ResolvedStruct, extent: int, label: str) -> list[str]:
	"""A zeroed buffer, a message over it, and a view.

	A variable struct's `at` takes the message length and a fixed one's does
	not -- the C++ backend's own branch, written down here rather than
	re-derived, since the Python vector emitter got exactly this wrong for
	13 of 27 schemas before it was run (26.829).
	"""
	length = "" if struct.layout.is_fixed_size else f", {extent}u"
	return [
		f"\tstd::uint8_t raw[{extent}u] = {{}};",
		f"\t::situ::rt::message msg(raw, {extent}u);",
		f"\t::situ::{struct.name} view;",
		"",
		f"\tif (::situ::{struct.name}::at(msg, 0u{length}, view) != {OK}) {{",
		*_fail(label, "the bytes take no view"),
		"\t}",
	]


def _short_buffer(struct: ResolvedStruct) -> list[str]:
	"""One byte short of the minimum must yield no view.

	Section 12.3 calls this the single check everything after it trusts.
	"""
	minimum = struct.layout.size_bytes
	short   = minimum - 1
	length  = "" if struct.layout.is_fixed_size else f", {short}u"
	name    = c_name(struct.name)

	return [
		f"/* `{struct.name}` needs {minimum} bytes; {short} must refuse. */",
		f"static void check_{name}_refuses_a_short_buffer(void)",
		"{",
		f"\tstd::uint8_t raw[{max(short, 1)}u] = {{}};",
		f"\t::situ::rt::message msg(raw, {short}u);",
		f"\t::situ::{struct.name} view;",
		"",
		f"\tif (::situ::{struct.name}::at(msg, 0u{length}, view) == {OK}) {{",
		*_fail(f"{struct.name}", f"{short} bytes yielded a view"),
		"\t}",
		"}",
		"",
	]


def _decodes(enums: set[str], struct: ResolvedStruct,
		placement: Placement) -> list[str]:
	"""A byte-aligned integer reads the number its bytes mean.

	The expectation is computed here with Python's own `int.from_bytes`
	rather than by the rule the accessor reads with, which is what keeps
	the check independent of the thing it checks (26.832). The whitelist
	and the positive byte-order test are that entry's too.
	"""
	if placement.offset_bits is None or placement.kind != "field":
		return []
	if placement.scalar is None or placement.radix is not None:
		return []
	if placement.size_max_bits != placement.size_bits:
		return []
	if placement.array_count is not None or placement.type_name in enums:
		return []
	if placement.offset_bits % BITS_PER_BYTE or placement.size_bits % BITS_PER_BYTE:
		return []
	if not struct.layout.is_byte_sized or struct.layout.size_bytes == 0:
		return []
	if placement.scalar.kind not in (ScalarKind.UINT, ScalarKind.SINT):
		return []
	if placement.scalar.frac_bits:
		return []

	width = placement.size_bits // BITS_PER_BYTE
	if not 1 <= width <= 8:
		return []
	first  = placement.offset_bits // BITS_PER_BYTE
	extent = struct.layout.size_bytes
	if first + width > extent:
		return []

	if placement.endian is ast.Endian.BIG:
		order = "big"
	elif placement.endian is ast.Endian.LITTLE:
		order = "little"
	else:
		return []

	pattern  = bytes(PROBE_BYTES[:width])
	expected = int.from_bytes(pattern, order,  # type: ignore[arg-type]
	                          signed=placement.scalar.signed)
	label = f"{placement.path}"
	name  = c_name(placement.path)
	return [
		f"/* {placement.path}: {width} byte(s) at {first}, {order} order,",
		f" * signed={placement.scalar.signed}. {expected} is what those",
		" * bytes mean, computed by Python's `int.from_bytes` rather than",
		" * by the rule the accessor reads with. */",
		f"static void check_{name}_decodes_a_known_encoding(void)",
		"{",
		*_acquire(struct, extent, label),
		"",
		*(f"\traw[{first + i}u] = {byte:#04x}u;"
		  for i, byte in enumerate(pattern)),
		"",
		f"\tif (static_cast<std::int64_t>(view.{c_name(placement.name)}())"
		f" != {expected}) {{",
		*_fail(label, "does not decode its own bytes"),
		"\t}",
		"}",
		"",
	]


def _baseline(resolved: ResolvedSchema, struct: ResolvedStruct,
		extent: int, inside: str | None = None) -> list[str] | None:
	"""Statements making a zeroed buffer satisfy every constraint.

	Without it a constraint check is vacuous: a zeroed buffer already
	breaks `must_be_one`, so the check would pass against a validator that
	did nothing (26.838). `None` where some constraint cannot be satisfied.
	"""
	writes: list[str] = []
	carried = selected_arms(resolved, struct, inside)

	for entry in struct.entries:
		placement = entry.placement
		if placement.offset_bits is None or not placement.size_bits:
			continue

		digits = digit_pattern(resolved, struct, placement)
		if digits is not None:
			if placement.offset_bits % BITS_PER_BYTE:
				return None
			at = placement.offset_bits // BITS_PER_BYTE
			if at + len(digits) > extent:
				return None
			writes.extend(f"\traw[{at + i}u] = {byte:#04x}u;"
			              for i, byte in enumerate(digits))
			continue

		run = required_run(placement)
		if run is not None:
			if placement.offset_bits % BITS_PER_BYTE:
				return None
			at = placement.offset_bits // BITS_PER_BYTE
			if at + len(run) > extent:
				return None
			writes.extend(f"\traw[{at + i}u] = {byte:#04x}u;"
			              for i, byte in enumerate(run))
			continue

		if placement.array_count is not None or placement.sized_by is not None:
			continue
		if "[]" in placement.path:
			continue

		held = enclosing_arm(struct, placement)
		if held is not None and held[1].member not in carried:
			continue

		value = required_pattern(resolved, struct, placement)
		if value in (None, 0):
			continue
		assert value is not None

		placed = placed_bytes(
			placement.offset_bits, placement.size_bits, value,
			placement.endian, placement.bit_order,
			bool(placement.scalar and placement.scalar.is_bit_packed))
		if placed is None or max(placed) >= extent:
			return None

		writes.extend(f"\traw[{at}u] = static_cast<std::uint8_t>"
		              f"(raw[{at}u] | {placed[at]:#04x}u);"
		              for at in sorted(placed) if placed[at])

	return writes


def _break_to(placement: Placement, wrong: int) -> list[str] | None:
	"""Statements setting this member's bits to `wrong`, leaving the rest.

	Read-modify-write, because a member narrower than a byte shares one
	with its neighbours: storing would break theirs too and the refusal
	would be true of the wrong field.
	"""
	if placement.offset_bits is None or not placement.size_bits:
		return None

	packed = bool(placement.scalar and placement.scalar.is_bit_packed)
	placed = placed_bytes(placement.offset_bits, placement.size_bits, wrong,
	                      placement.endian, placement.bit_order, packed)
	mask = placed_bytes(placement.offset_bits, placement.size_bits,
	                    (1 << placement.size_bits) - 1,
	                    placement.endian, placement.bit_order, packed)
	if placed is None or mask is None:
		return None

	return [f"\traw[{at}u] = static_cast<std::uint8_t>((raw[{at}u] & "
	        f"{0xFF ^ mask[at]:#04x}u) | {placed[at]:#04x}u);"
	        for at in sorted(mask)]


def _select_arm(struct: ResolvedStruct, placement: Placement,
		extent: int) -> list[str] | None:
	"""Statements putting the discriminant where this member exists."""
	found = enclosing_arm(struct, placement)
	if found is None:
		return []

	variant, arm = found
	if arm.value is None:
		return None

	held = next((entry.placement for entry in struct.entries
	             if entry.placement.name == variant.discriminant), None)
	if held is None or held.offset_bits is None or held.scalar is None:
		return None

	placed = placed_bytes(held.offset_bits, held.size_bits, arm.value,
	                      held.endian, held.bit_order,
	                      held.scalar.is_bit_packed)
	mask = placed_bytes(held.offset_bits, held.size_bits,
	                    (1 << held.size_bits) - 1, held.endian,
	                    held.bit_order, held.scalar.is_bit_packed)
	if placed is None or mask is None or max(placed) >= extent:
		return None

	# Cleared and written rather than OR-ed: the baseline may already have
	# left a value in the discriminant, and OR-ing 0x02 over a 0x01 selects
	# arm three.
	return [f"\t/* Select the arm this member is in:"
	        f" {variant.discriminant} = {arm.value}. */",
	        *(f"\traw[{at}u] = {placed[at]:#04x}u;" if mask[at] == 0xFF
	          else f"\traw[{at}u] = static_cast<std::uint8_t>((raw[{at}u] & "
	               f"{0xFF ^ mask[at]:#04x}u) | {placed[at]:#04x}u);"
	          for at in sorted(mask))]


def _reach_version(struct: ResolvedStruct, extent: int) -> list[str] | None:
	"""Put the version field where every versioned member is present."""
	versioned = [held.placement.since for held in struct.entries
	             if held.placement.since is not None]
	if not versioned:
		return []

	name = next((held.placement.version_field for held in struct.entries
	             if held.placement.version_field is not None), None)
	held = next((held.placement for held in struct.entries
	             if held.placement.name == name), None)
	if held is None or held.offset_bits is None or held.scalar is None:
		return None

	placed = placed_bytes(held.offset_bits, held.size_bits, max(versioned),
	                      held.endian, held.bit_order,
	                      held.scalar.is_bit_packed)
	if placed is None or max(placed) >= extent:
		return None

	return [f"\t/* Version {max(versioned)}, so every versioned member is"
	        " here. */",
	        *(f"\traw[{at}u] = {placed[at]:#04x}u;"
	          for at in sorted(placed))]


def _prelude(resolved: ResolvedSchema, struct: ResolvedStruct,
		placement: Placement, extent: int) -> list[str] | None:
	"""Every constraint satisfied, the version reached, the arm selected."""
	select = _select_arm(struct, placement, extent)
	if select is None:
		return None

	found  = enclosing_arm(struct, placement)
	inside = found[1].member if found is not None else None
	base   = _baseline(resolved, struct, extent, inside)
	if base is None:
		return None

	reach = _reach_version(struct, extent)
	if reach is None:
		return None

	lines: list[str] = []
	if base:
		lines += ["\t/* Every constraint satisfied, so the refusal below is",
		          "\t * about the field this check breaks and nothing"
		          " else. */",
		          *base, ""]
	if reach:
		lines += [*reach, ""]
	if select:
		lines += [*select, ""]
	return lines


def _constraint(resolved: ResolvedSchema, struct: ResolvedStruct,
		placement: Placement, extent: int, suffix: str, note: list[str],
		wrong: int, broke: str) -> list[str] | None:
	"""The shape every constraint check here has.

	Acquire, satisfy everything, `validate` must accept, break one field,
	`validate` must refuse. The three callers differ only in which value
	they write and what they say about it, so the sequence is written once:
	three copies of "accept then refuse" would be three chances to leave
	out the accept, which is the half that gives the refusal meaning.
	"""
	prelude = _prelude(resolved, struct, placement, extent)
	if prelude is None:
		return None
	broken = _break_to(placement, wrong)
	if broken is None:
		return None

	label = placement.path
	name  = c_name(placement.path)
	return [
		*note,
		f"static void check_{name}_{suffix}(void)",
		"{",
		*_acquire(struct, extent, label),
		"",
		*prelude,
		"\t/* The control: without it a refusal below proves nothing, a",
		"\t * buffer the schema already refuses refusing whatever this",
		"\t * wrote into it. */",
		f"\tif (view.validate() != {OK}) {{",
		*_fail(label, "the baseline does not validate"),
		"\t}",
		"",
		f"\t/* {broke} */",
		*broken,
		f"\tif (view.validate() == {OK}) {{",
		*_fail(label, "a value the schema forbids was accepted"),
		"\t}",
		"}",
		"",
	]


def _reserved(resolved: ResolvedSchema, struct: ResolvedStruct,
		placement: Placement, extent: int) -> list[str]:
	"""A reserved field holds what its policy says.

	Section 8.8: reserved bits are malleability control, not pedantry -- a
	receiver that ignores them lets a sender vary bytes the format calls
	fixed, which is a bypass primitive in anything authenticated.
	"""
	policy = reserved_policy(placement)
	if policy is None or placement.offset_bits is None:
		return []
	if not placement.size_bits or "[]" in placement.path:
		return []
	if placement.pinned_runs:
		return []
	if placement.offset_bits + placement.size_bits > extent * BITS_PER_BYTE:
		return []
	if inside_an_unresolved_arm(struct, placement):
		return []

	wrong = 0 if policy == "must_be_one" else (1 << placement.size_bits) - 1
	body  = _constraint(
		resolved, struct, placement, extent, f"{policy}_is_enforced",
		[f"/* {placement.path} is reserved [{policy}]. Section 8.8:",
		 " * reserved bits are malleability control, not pedantry. */"],
		wrong, f"{placement.path} declares {policy}.")
	return body or []


def _must_eq(resolved: ResolvedSchema, struct: ResolvedStruct,
		placement: Placement, extent: int) -> list[str]:
	"""A pinned value, broken by adding one inside its own width."""
	from situc.expr import evaluate

	must = next((attr for attr in placement.attrs
	             if attr.name == "must_eq"), None)
	run = bool(required_run(placement, every=True))
	if must is None or must.value is None:
		return []
	if run and placement.kind != "reserved":
		return []		# a byte run: a number cannot break one
	if placement.offset_bits is None or not placement.size_bits:
		return []
	if inside_an_unresolved_arm(struct, placement):
		return []
	if (placement.offset_bits + placement.size_bits - 1) \
			// BITS_PER_BYTE >= extent:
		return []
	try:
		demanded = int(evaluate(must.value, resolved.layout.env))
	except Exception:	# noqa: BLE001 - unfoldable is unbreakable
		return []

	wrong = (demanded + 1) & ((1 << placement.size_bits) - 1)
	body  = _constraint(
		resolved, struct, placement, extent, "must_eq_is_enforced",
		[f"/* {placement.path} is pinned to {demanded}. A value the",
		 " * schema does not admit has to be refused on parse (8.8). */"],
		wrong, f"{placement.path} is pinned to {demanded}; {wrong} is not.")
	return body or []


def _enum(schema: ast.Schema, resolved: ResolvedSchema,
		struct: ResolvedStruct, placement: Placement,
		extent: int) -> list[str]:
	"""An enum admits its members and, with `default = error`, no more."""
	values = resolved.layout.env.enums.get(placement.type_name or "")
	if not values or "[]" in placement.path:
		return []
	if passes_unknown(schema, placement.type_name):
		return []		# section 8.7's escape hatch: nothing to reject
	if placement.offset_bits is None or not placement.size_bits:
		return []
	if inside_an_unresolved_arm(struct, placement):
		return []
	if (placement.offset_bits + placement.size_bits - 1) \
			// BITS_PER_BYTE >= extent:
		return []

	taken   = set(values.values())
	unknown = next((v for v in range(1 << placement.size_bits)
	                if v not in taken), None)
	if unknown is None:
		return []		# the enum names every value its bits can hold

	body = _constraint(
		resolved, struct, placement, extent,
		"rejects_a_value_no_member_names",
		[f"/* {placement.path} is a `{placement.type_name}`, which rejects",
		 " * unknown values (8.7). A field declared to admit a handful of",
		 " * values that takes all 256 is not the field described. */"],
		unknown, f"{unknown} names no member of `{placement.type_name}`.")
	return body or []


def _occupies(struct: ResolvedStruct, placement: Placement) -> list[str]:
	"""A field occupies exactly the bytes the map claims.

	Each byte of its claim must reach the value and no other byte may,
	which is asked by flipping every byte of the buffer in turn.
	"""
	if placement.offset_bits is None or placement.kind != "field":
		return []
	if placement.scalar is None or placement.size_max_bits != placement.size_bits:
		return []
	if placement.array_count is not None or placement.radix is not None:
		return []
	if not struct.layout.is_byte_sized or struct.layout.size_bytes == 0:
		return []

	extent = struct.layout.size_bytes
	first  = placement.offset_bits // BITS_PER_BYTE
	last   = (placement.offset_bits + placement.size_bits - 1) // BITS_PER_BYTE
	if last >= extent:
		return []

	label  = placement.path
	name   = c_name(placement.path)
	getter = c_name(placement.name)
	return [
		f"/* {placement.path} claims bytes {first}..{last}. Every byte of",
		" * the claim must reach it and no other byte may. */",
		f"static void check_{name}_occupies_its_claimed_bytes(void)",
		"{",
		*_acquire(struct, extent, label),
		f"\tconst std::int64_t base ="
		f" static_cast<std::int64_t>(view.{getter}());",
		"",
		f"\tfor (std::uint32_t i = 0u; i < {extent}u; i++) {{",
		"\t\traw[i] = static_cast<std::uint8_t>(~raw[i]);",
		"",
		"\t\tconst bool moved ="
		f" static_cast<std::int64_t>(view.{getter}()) != base;",
		"",
		f"\t\tif (i >= {first}u && i <= {last}u) {{",
		"\t\t\tif (!moved) {",
		f'\t\t\t\tstd::printf("FAIL {label}: byte %u is claimed and does'
		' not reach it\\n", i);',
		"\t\t\t\tsitu_failures++;",
		"\t\t\t}",
		"\t\t} else if (moved) {",
		f'\t\t\tstd::printf("FAIL {label}: byte %u is not claimed and'
		' reaches it\\n", i);',
		"\t\t\tsitu_failures++;",
		"\t\t}",
		"",
		"\t\traw[i] = static_cast<std::uint8_t>(~raw[i]);",
		"\t}",
		"}",
		"",
	]


def _framing(resolved: ResolvedSchema, basename: str) -> list[str]:
	"""A reader reassembles a stream whatever the arrival boundaries are.

	The pair discriminates: a reader announcing a message on every push
	satisfies "two whole messages arrive" and fails the stream a byte
	short, and one announcing nothing passes the short case and fails the
	whole one (26.843).
	"""
	lines: list[str] = []
	for struct in framed_structs(resolved):
		if arguments(struct):
			continue
		if not (struct.layout.is_byte_sized and struct.layout.is_fixed_size):
			continue
		if struct.layout.size_bytes < 2:
			continue

		extent = struct.layout.size_bytes
		whole  = extent * 2
		name   = c_name(struct.name)
		reader = f"::situ::{struct.name}_reader"
		lines += [
			f"/* Two whole {struct.name} messages pushed one byte at a time,",
			" * which cuts the stream at every boundary it has. Exactly two",
			" * must come out, and the loop is bounded by the ANSWER: a",
			" * reader announcing one every time would spin for ever, and a",
			" * test that hangs is worse than one that fails. */",
			f"static void check_{name}_reader_reassembles_across_every_"
			"boundary(void)",
			"{",
			f"\tstd::uint8_t stream[{whole}u] = {{}};",
			f"\tstd::uint8_t back[{whole}u];",
			f"\t{reader} reader(back, static_cast<std::uint32_t>"
			"(sizeof back));",
			f"\t::situ::{struct.name} view;",
			"\tstd::uint32_t seen = 0u;",
			"",
			"\tfor (std::uint32_t i = 0u; i < "
			f"static_cast<std::uint32_t>(sizeof stream); i++) {{",
			f"\t\tif (reader.push(&stream[i], 1u) != {OK}) {{",
			*_fail(struct.name, "a push was refused"),
			"\t\t}",
			f"\t\twhile (seen <= 2u && reader.next(view) == {OK}) {{",
			"\t\t\tseen++;",
			"\t\t\treader.advance();",
			"\t\t}",
			"\t}",
			"",
			"\tif (seen != 2u) {",
			f'\t\tstd::printf("FAIL {struct.name}: a byte-at-a-time stream'
			' of two messages yielded %u\\n", seen);',
			"\t\tsitu_failures++;",
			"\t}",
			"}",
			"",
			f"/* One byte short of a whole {struct.name}. A reader handing",
			" * this out would be handing out a message that has not",
			" * arrived -- and without this case a reader announcing one on",
			" * every push would still satisfy the check above. */",
			f"static void check_{name}_reader_yields_nothing_from_a_partial_"
			"message(void)",
			"{",
			f"\tstd::uint8_t stream[{extent - 1}u] = {{}};",
			f"\tstd::uint8_t back[{extent}u];",
			f"\t{reader} reader(back, static_cast<std::uint32_t>"
			"(sizeof back));",
			f"\t::situ::{struct.name} view;",
			"",
			"\tif (reader.push(stream, static_cast<std::uint32_t>"
			f"(sizeof stream)) != {OK}) {{",
			*_fail(struct.name, "a push was refused"),
			"\t}",
			f"\tif (reader.next(view) != ::situ::rt::err::truncated) {{",
			*_fail(struct.name, "a partial message was handed out"),
			"\t}",
			"}",
			"",
		]
	return lines


def _round_trip(resolved: ResolvedSchema, enums: set[str],
		struct: ResolvedStruct, entry: Resolved) -> list[str]:
	"""A field the map calls writable in place round-trips and moves nothing.

	**The gate is two axes, not one**, and the second cost the Python
	backend five schemas before it was there. `mutate = InPlaceFixed` says
	writing the field moves no bytes; `auth = Covered` says writing it
	leaves an authentication tag stale, and the backend withholds the plain
	setter for that reason (section 14.2) -- so a field can be writable in
	place and have no setter at all.
	"""
	placement = entry.placement
	if entry.vector.get(Axis.MUTATE).base != "InPlaceFixed":
		return []
	if entry.vector.get(Axis.AUTH).base != "Uncovered":
		return []
	if placement.offset_bits is None or placement.kind != "field":
		return []
	if placement.scalar is None or placement.radix is not None:
		return []
	if placement.size_max_bits != placement.size_bits:
		return []
	if placement.array_count is not None or placement.type_name in enums:
		return []
	if not struct.layout.is_byte_sized or struct.layout.size_bytes == 0:
		return []

	extent = struct.layout.size_bytes
	first  = placement.offset_bits // BITS_PER_BYTE
	last   = (placement.offset_bits + placement.size_bits - 1) // BITS_PER_BYTE
	if last >= extent:
		return []

	# **A driving field's setter takes the message**, because writing it
	# moves where the members after it start and the write has to bump
	# the generation. `set_nla_len(int)` is what the first version
	# emitted for three such schemas and g++ refused all three -- the
	# corpus doing what the corpus is for. `invalidating_members` is the
	# same predicate the emitter branches on, so this cannot drift from
	# the signature it is calling.
	from situc.traverse import invalidating_members, local_name

	moves = local_name(struct, placement) in invalidating_members(
		resolved.structs).get(struct.name, set())
	hand  = "msg, " if moves else ""

	label  = placement.path
	name   = c_name(placement.path)
	getter = c_name(placement.name)
	length = "" if struct.layout.is_fixed_size else f", {extent}u"
	return [
		f"/* The map calls {placement.path} writable in place: writing it",
		" * must read back and must move no byte outside its own claim. */",
		f"static void check_{name}_round_trips_in_place(void)",
		"{",
		*_acquire(struct, extent, label),
		"",
		f"	view.set_{getter}({hand}1);",
		"",
		"	/* Read back through a FRESH view. `InPlaceFixed` says",
		"	 * writing this field moves no bytes; it does not say a",
		"	 * view taken before the write survives it, and a driving",
		"	 * field bumps the message generation. Whether bytes",
		"	 * MOVED is asked of `raw` below, which no generation",
		"	 * affects. */",
		f"	if (::situ::{struct.name}::at(msg, 0u{length}, view)"
		f" != {OK}) {{",
		*_fail(label, "no view after the write"),
		"	}",
		f"	if (static_cast<std::int64_t>(view.{getter}()) != 1) {{",
		*_fail(label, "did not take 1"),
		"	}",
		"",
		f"	for (std::uint32_t i = 0u; i < {extent}u; i++) {{",
		f"		if (i >= {first}u && i <= {last}u) {{",
		"			continue;",
		"		}",
		"		if (raw[i] != 0u) {",
		f'			std::printf("FAIL {label}: writing it moved byte'
		' %u\\n", i);',
		"			situ_failures++;",
		"		}",
		"	}",
		"}",
		"",
	]


def _nested_start(resolved: ResolvedSchema, struct: ResolvedStruct,
		placement: Placement) -> list[str]:
	"""Where a nested struct sits inside its parent.

	Its interior is checked under its own struct, against a buffer where it
	begins at zero, which says nothing about where the parent puts it --
	the hole mutation testing found (26.834).
	"""
	if placement.kind != "field" or placement.type_name is None:
		return []
	nested = resolved.structs.get(placement.type_name)
	if nested is None:
		return []
	if placement.offset_bits is None or placement.array_count is not None:
		return []
	if placement.sized_by is not None or placement.sealed_by is not None:
		return []
	if placement.repeat_while is not None or placement.delimiters:
		return []
	if placement.offset_bits % BITS_PER_BYTE:
		return []
	if not struct.layout.is_byte_sized or struct.layout.size_bytes == 0:
		return []
	if not nested.layout.is_fixed_size \
			and not has_computable_extent(resolved.structs, nested):
		return []

	extent = struct.layout.size_bytes
	offset = placement.offset_bits // BITS_PER_BYTE
	label  = placement.path
	name   = c_name(placement.path)
	return [
		f"/* {placement.path} starts {offset} byte(s) into"
		f" `{struct.name}`. Its own fields are checked under its own",
		" * struct, where it begins at zero; only this says where the",
		" * parent actually puts it. */",
		f"static void check_{name}_starts_where_the_map_says(void)",
		"{",
		*_acquire(struct, extent, label),
		f"\t::situ::{placement.type_name} inner;",
		"",
		f"\tif (view.{c_name(placement.name)}(inner) != {OK}) {{",
		*_fail(label, "no sub-view"),
		"\t}",
		"\t/* The map's offset, measured from the parent rather than",
		"\t * recomputed from it. */",
		"\tif (static_cast<std::uint32_t>(situ_base(inner.raw())"
		f" - situ_base(view.raw())) != {offset}u) {{",
		*_fail(label, "does not start where the map says"),
		"\t}",
		"}",
		"",
	]


def generate(schema: ast.Schema, resolved: ResolvedSchema,
		basename: str, prefix: str = "situ") -> str:
	"""The capability map held to the C++ accessors, as one `main`."""
	refuse_parameters(schema)

	enums  = {decl.name for decl in schema.enums()}
	bodies: list[str] = []
	names:  list[str] = []

	def take(lines: list[str]) -> None:
		if not lines:
			return
		bodies.extend(lines)
		names.extend(one.split("(")[0].split()[-1] for one in lines
		             if one.startswith("static void check_"))

	framed = _framing(resolved, basename)

	for struct in resolved.structs.values():
		if struct.layout.register is not None:
			continue		# a bus transaction, with no view to acquire
		if not struct.layout.is_byte_sized or struct.layout.size_bytes == 0:
			continue
		extent = struct.layout.size_bytes

		take(_short_buffer(struct))
		for entry in own_entries(struct):
			take(_occupies(struct, entry.placement))
			take(_round_trip(resolved, enums, struct, entry))
			take(_decodes(enums, struct, entry.placement))
			take(_nested_start(resolved, struct, entry.placement))
		for entry in struct.entries:
			take(_reserved(resolved, struct, entry.placement, extent))
			take(_must_eq(resolved, struct, entry.placement, extent))
			take(_enum(schema, resolved, struct, entry.placement, extent))

	take(framed)

	lines = [
		f"/* Generated by situ {__version__} from {basename}.situ"
		" -- do not edit.",
		" *",
		" * Capability-conformance checks over the C++ accessors: the",
		" * accessors held to the map they were generated beside.",
		" *",
		" * A plain `main` counting failures rather than a framework, which",
		" * is `cpp/vectors.c`'s decision and for its reason: the C suite",
		" * uses cmocka and this has no dependency to spend.",
		" *",
		" * The arithmetic is shared with `--target c` and",
		" * `--target python` -- which value a member must hold, where its",
		" * bytes land, which arm a message carries -- so the three cannot",
		" * disagree about the question, only about how each language",
		" * spells the answer.",
		" *",
		f" * All {len(CHECK_KINDS)} kinds:",
		" *",
		*(f" *   - {kind}" for kind in CHECK_KINDS),
		" *",
		" * Nothing is left out at the level of KINDS. What each kind",
		" * declines for a particular member is said where that member",
		" * would have been checked, because a suite that checks one of",
		" * nine things and does not say so reads as the whole map having",
		" * been checked -- and so does one that checks a kind for some",
		" * members and not others.",
		" */",
		"",
		"#include <cstdint>",
		"#include <cstdio>",
		"",
		f'#include "{basename}.hpp"',
		*([f'#include "{basename}_frame.hpp"'] if framed else []),
		"",
		"static int situ_failures = 0;",
		"",
		*bodies,
		"int main(void)",
		"{",
		*(f"\t{one}();" for one in names),
		"",
		# **The empty case says so rather than printing a ratio of
		# nothing.** `example/register` is every struct a bus transaction
		# and `example/protobuf` has none with a minimum, so both produced
		# a `main` announcing "0 of 0 check(s) failed" -- which is the
		# vacuous pass this tree names everywhere, and which the corpus
		# gate refuses by that exact string. A suite that checked nothing
		# has to be distinguishable from one that checked everything and
		# found nothing wrong.
		*([f'\tstd::printf("%d of {len(names)} check(s) failed\\n",'
		   " situ_failures);"] if names else
		  ['\tstd::printf("no check applies to this schema: every struct'
		   ' is a register,\\n");',
		   '\tstd::printf("or none has a minimum extent to acquire'
		   ' a view over\\n");']),
		"\treturn situ_failures == 0 ? 0 : 1;",
		"}",
	]
	return "\n".join(lines) + "\n"
