"""`gen-checks --target rust`: the capability map held to the Rust accessors.

The fourth backend of the four, and by 26.844's argument the cheapest: the
arithmetic -- which value a member must hold, where its bytes land, which
arm a message carries, what a probe pattern is -- is all in
`situc/codegen`, so what is written here is Rust's vocabulary for
acquiring a view, writing through a setter and asking a getter.

A test crate root with `#[test]` functions, which is what
`rust/vectors.py` emits and for its reason: `rustc --test` compiles it
with no dependency to declare.

**Seven of the nine kinds.** The two it declines are named in the
generated file and in 26.845: a nested member's START cannot be asked,
the Rust view holding a private slice rather than an offset anything can
read, and a framing reader's is deferred with it.
"""

from __future__ import annotations

from situc import __version__, ast
from situc.capability import Axis
from situc.codegen import (CHECK_KINDS, PROBE_BYTES, digit_pattern,
                           passes_unknown, placed_bytes, refuse_parameters,
                           required_pattern, required_run, reserved_policy,
                           selected_arms)
from situc.codegen.c.names import c_name
from situc.codegen.rust.emit import _ident, _pascal
from situc.layout import BITS_PER_BYTE, Placement
from situc.propagate import Resolved
from situc.resolve import ResolvedSchema, ResolvedStruct
from situc.traverse import enclosing_arm, inside_an_unresolved_arm, own_entries
from situc.types import ScalarKind

#: The two this backend does not emit, and why, so the generated file can
#: say so rather than leaving a reader to take seven for nine.
DECLINED = {
	"a nested member starts where the map says":
		"a Rust view holds a private slice, so nothing can read a"
		" sub-view's offset to compare it against the map",
	"a framing reader reassembles across every boundary":
		"deferred with it, the reader's own vocabulary being the next"
		" thing to establish",
}


def _acquire(basename: str, struct: ResolvedStruct, extent: int,
		label: str) -> list[str]:
	"""A zeroed buffer and a view over it."""
	return [
		f"\t\tlet raw = [0u8; {extent}];",
		f"\t\tlet view = crate::{basename}::{_pascal(struct.name)}"
		"::new(&raw)",
		f'\t\t\t.expect("{label}: a zeroed buffer must take a view");',
	]


def _short_buffer(basename: str, struct: ResolvedStruct) -> list[str]:
	"""One byte short of the minimum must yield no view.

	Section 12.3 calls this the single check everything after it trusts.
	"""
	minimum = struct.layout.size_bytes
	short   = minimum - 1
	name    = c_name(struct.name)
	return [
		"\t#[test]",
		f"\tfn {name}_refuses_a_short_buffer() {{",
		f'\t\t// `{struct.name}` needs {minimum} bytes; {short} must refuse.',
		f"\t\tlet raw = [0u8; {max(short, 1)}];",
		f"\t\tassert!(crate::{basename}::{_pascal(struct.name)}"
		f"::new(&raw[..{short}]).is_err(),",
		f'\t\t\t"{struct.name}: {short} bytes yielded a view");',
		"\t}",
		"",
	]


def _decodes(basename: str, enums: set[str], struct: ResolvedStruct,
		placement: Placement) -> list[str]:
	"""A byte-aligned integer reads the number its bytes mean.

	The expectation is computed here with Python's own `int.from_bytes`
	rather than by the rule the accessor reads with (26.832).
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
	label = placement.path
	name  = c_name(placement.path)
	return [
		"\t#[test]",
		f"\tfn {name}_decodes_a_known_encoding() {{",
		f"\t\t// {width} byte(s) at {first}, {order} order,"
		f" signed={placement.scalar.signed}. {expected} is what those",
		"\t\t// bytes mean, computed by Python's `int.from_bytes` rather",
		"\t\t// than by the rule the accessor reads with.",
		f"\t\tlet mut raw = [0u8; {extent}];",
		*(f"\t\traw[{first + i}] = {byte:#04x};"
		  for i, byte in enumerate(pattern)),
		"",
		f"\t\tlet view = crate::{basename}::{_pascal(struct.name)}"
		"::new(&raw)",
		f'\t\t\t.expect("{label}: the buffer must take a view");',
		f"\t\tassert_eq!(i128::from(view.{_ident(placement.name)}()),"
		f" {expected},",
		f'\t\t\t"{label} does not decode its own bytes");',
		"\t}",
		"",
	]


def _occupies(basename: str, enums: set[str], struct: ResolvedStruct,
		placement: Placement) -> list[str]:
	"""A field occupies exactly the bytes the map claims.

	Each byte of the claim must reach the value and no other byte may,
	asked by flipping every byte of the buffer in turn.
	"""
	if placement.offset_bits is None or placement.kind != "field":
		return []
	if placement.scalar is None or placement.size_max_bits != placement.size_bits:
		return []
	if placement.array_count is not None or placement.radix is not None:
		return []
	# **An enum-typed field cannot be asked this, and the second attempt is
	# why.** Rust's getter answers `Option<T>`, so
	# `i128::from(view.r#type())` does not compile -- rustc said so for
	# `header.type` at once. The generated enum derives `PartialEq`, so
	# comparing the two `Option`s directly looked like the better answer
	# than declining the field; it compiled, and the check then FAILED with
	# "byte 1 is claimed and does not reach it".
	#
	# Because `Option` is lossy exactly where this check looks. A zeroed
	# `header.type` names no variant and reads `None`; flipping the byte to
	# 0xFF names no variant either and reads `None` as well. Two different
	# bytes, one observation, and a check about which bytes REACH a value
	# cannot be built on an observation that collapses them.
	#
	# So the first instinct was right and the reason is better than the
	# instinct: not "the type is awkward" but "the getter does not
	# distinguish the inputs this check varies".
	if placement.type_name in enums:
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
	getter = _ident(placement.name)
	kind   = _pascal(struct.name)
	return [
		"\t#[test]",
		f"\tfn {name}_occupies_its_claimed_bytes() {{",
		f"\t\t// {label} claims bytes {first}..{last}. Every byte of the",
		"\t\t// claim must reach it and no other byte may.",
		f"\t\tlet mut raw = [0u8; {extent}];",
		"\t\tlet base = {",
		f"\t\t\tlet view = crate::{basename}::{kind}::new(&raw)",
		f'\t\t\t\t.expect("{label}: a zeroed buffer must take a view");',
		f"\t\t\ti128::from(view.{getter}())",
		"\t\t};",
		"",
		f"\t\tfor i in 0..{extent} {{",
		"\t\t\traw[i] = !raw[i];",
		"\t\t\tlet moved = {",
		f"\t\t\t\tlet view = crate::{basename}::{kind}::new(&raw)",
		f'\t\t\t\t\t.expect("{label}: the buffer must take a view");',
		f"\t\t\t\ti128::from(view.{getter}()) != base",
		"\t\t\t};",
		"\t\t\traw[i] = !raw[i];",
		"",
		f"\t\t\tif ({first}..={last}).contains(&i) {{",
		"\t\t\t\tassert!(moved,",
		f'\t\t\t\t\t"{label}: byte {{}} is claimed and does not reach it",'
		" i);",
		"\t\t\t} else {",
		"\t\t\t\tassert!(!moved,",
		f'\t\t\t\t\t"{label}: byte {{}} is not claimed and reaches it",'
		" i);",
		"\t\t\t}",
		"\t\t}",
		"\t}",
		"",
	]


def _round_trip(basename: str, enums: set[str], struct: ResolvedStruct,
		entry: Resolved) -> list[str]:
	"""A field the map calls writable in place round-trips and moves nothing.

	**The gate is two axes, not one.** `mutate = InPlaceFixed` says writing
	moves no bytes; `auth = Covered` says writing leaves a tag stale and
	the backend withholds the plain setter for it (section 14.2), so a
	field can be writable in place and have no setter at all.

	The write goes through the `*Mut` view, which is Rust's whole answer to
	the question the other backends settle with a generation counter: a
	`&mut [u8]` cannot coexist with the `&[u8]` the reading view holds, so
	the borrow checker enforces what `StaleViewError` reports.
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

	label  = placement.path
	name   = c_name(placement.path)
	getter = _ident(placement.name)
	# **`_ident` applies to the whole identifier, not to a part of it.**
	# `set_{_ident("type")}` is `set_r#type`, which rustc rejects with
	# "prefix `set_r` is unknown" -- the raw-identifier escape belongs in
	# front of the name it escapes, and `set_type` is not a keyword so it
	# needs none. The vectors port made the mirror of this mistake, using
	# `c_name` where `_ident` was wanted (26.829).
	setter = _ident(f"set_{c_name(placement.name)}")
	kind   = _pascal(struct.name)
	return [
		"\t#[test]",
		f"\tfn {name}_round_trips_in_place() {{",
		f"\t\t// The map calls {label} writable in place.",
		f"\t\tlet mut raw = [0u8; {extent}];",
		"\t\t{",
		f"\t\t\tlet mut view = crate::{basename}::{kind}Mut"
		"::new(&mut raw)",
		f'\t\t\t\t.expect("{label}: a zeroed buffer must take a view");',
		f"\t\t\tview.{setter}(1);",
		"\t\t}",
		"",
		"\t\t// Read back through a FRESH view. The borrow checker is what",
		"\t\t// makes the scope above necessary, and it is Rust's answer to",
		"\t\t// the staleness the other backends report at run time: the",
		"\t\t// `&mut [u8]` cannot coexist with the `&[u8]` a reader holds.",
		"\t\t{",
		f"\t\t\tlet view = crate::{basename}::{kind}::new(&raw)",
		f'\t\t\t\t.expect("{label}: the written buffer must take a view");',
		f"\t\t\tassert_eq!(i128::from(view.{getter}()), 1,",
		f'\t\t\t\t"{label} did not take 1");',
		"\t\t}",
		"",
		f"\t\tfor i in 0..{extent} {{",
		f"\t\t\tif ({first}..={last}).contains(&i) {{",
		"\t\t\t\tcontinue;",
		"\t\t\t}",
		"\t\t\tassert_eq!(raw[i], 0,",
		f'\t\t\t\t"writing {label} moved byte {{}}", i);',
		"\t\t}",
		"\t}",
		"",
	]


def _baseline(resolved: ResolvedSchema, struct: ResolvedStruct,
		extent: int, inside: str | None = None) -> list[str] | None:
	"""Statements making a zeroed buffer satisfy every constraint.

	Without it a constraint check is vacuous: a zeroed buffer already
	breaks `must_be_one`, so the check would pass against a validator that
	did nothing (26.838).
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
			writes.extend(f"\t\traw[{at + i}] = {byte:#04x};"
			              for i, byte in enumerate(digits))
			continue

		run = required_run(placement)
		if run is not None:
			if placement.offset_bits % BITS_PER_BYTE:
				return None
			at = placement.offset_bits // BITS_PER_BYTE
			if at + len(run) > extent:
				return None
			writes.extend(f"\t\traw[{at + i}] = {byte:#04x};"
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

		writes.extend(f"\t\traw[{at}] |= {placed[at]:#04x};"
		              for at in sorted(placed) if placed[at])

	return writes


def _break_to(placement: Placement, wrong: int) -> list[str] | None:
	"""Statements setting this member's bits to `wrong`, leaving the rest."""
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

	return [f"\t\traw[{at}] = (raw[{at}] & {0xFF ^ mask[at]:#04x})"
	        f" | {placed[at]:#04x};"
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

	return [f"\t\t// Select the arm this member is in:"
	        f" {variant.discriminant} = {arm.value}.",
	        *(f"\t\traw[{at}] = {placed[at]:#04x};" if mask[at] == 0xFF
	          else f"\t\traw[{at}] = (raw[{at}] & {0xFF ^ mask[at]:#04x})"
	               f" | {placed[at]:#04x};"
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

	return [f"\t\t// Version {max(versioned)}, so every versioned member is"
	        " here.",
	        *(f"\t\traw[{at}] = {placed[at]:#04x};"
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
		lines += ["\t\t// Every constraint satisfied, so the refusal below",
		          "\t\t// is about the field this breaks and nothing else.",
		          *base]
	if reach:
		lines += reach
	if select:
		lines += select
	return lines


def _constraint(basename: str, resolved: ResolvedSchema,
		struct: ResolvedStruct, placement: Placement, extent: int,
		suffix: str, note: list[str], wrong: int,
		broke: str) -> list[str] | None:
	"""The shape every constraint check here has.

	Acquire, satisfy everything, `validate` must accept, break one field,
	`validate` must refuse. Written once because three copies of "accept
	then refuse" are three chances to leave out the accept, which is the
	half that gives the refusal its meaning.
	"""
	prelude = _prelude(resolved, struct, placement, extent)
	if prelude is None:
		return None
	broken = _break_to(placement, wrong)
	if broken is None:
		return None

	label = placement.path
	name  = c_name(placement.path)
	kind  = _pascal(struct.name)
	return [
		"\t#[test]",
		f"\tfn {name}_{suffix}() {{",
		*note,
		f"\t\tlet mut raw = [0u8; {extent}];",
		*prelude,
		"",
		"\t\t// The control: without it a refusal below proves nothing, a",
		"\t\t// buffer the schema already refuses refusing whatever this",
		"\t\t// wrote into it.",
		"\t\t{",
		f"\t\t\tlet view = crate::{basename}::{kind}::new(&raw)",
		f'\t\t\t\t.expect("{label}: the baseline must take a view");',
		"\t\t\tview.validate()",
		f'\t\t\t\t.expect("{label}: the baseline must validate");',
		"\t\t}",
		"",
		f"\t\t// {broke}",
		*broken,
		"",
		"\t\t{",
		f"\t\t\tlet view = crate::{basename}::{kind}::new(&raw)",
		f'\t\t\t\t.expect("{label}: the broken buffer must take a view");',
		"\t\t\tassert!(view.validate().is_err(),",
		f'\t\t\t\t"{label}: a value the schema forbids was accepted");',
		"\t\t}",
		"\t}",
		"",
	]


def _reserved(basename: str, resolved: ResolvedSchema,
		struct: ResolvedStruct, placement: Placement,
		extent: int) -> list[str]:
	"""A reserved field holds what its policy says (section 8.8)."""
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
	return _constraint(
		basename, resolved, struct, placement, extent,
		f"{policy}_is_enforced",
		[f"\t\t// {placement.path} is reserved [{policy}]. Section 8.8:",
		 "\t\t// reserved bits are malleability control, not pedantry."],
		wrong, f"{placement.path} declares {policy}.") or []


def _must_eq(basename: str, resolved: ResolvedSchema,
		struct: ResolvedStruct, placement: Placement,
		extent: int) -> list[str]:
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
	return _constraint(
		basename, resolved, struct, placement, extent, "must_eq_is_enforced",
		[f"\t\t// {placement.path} is pinned to {demanded}. A value the",
		 "\t\t// schema does not admit has to be refused on parse (8.8)."],
		wrong,
		f"{placement.path} is pinned to {demanded}; {wrong} is not.") or []


def _enum(basename: str, schema: ast.Schema, resolved: ResolvedSchema,
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

	return _constraint(
		basename, resolved, struct, placement, extent,
		"rejects_a_value_no_member_names",
		[f"\t\t// {placement.path} is a `{placement.type_name}`, which",
		 "\t\t// rejects unknown values (8.7). A field declared to admit a",
		 "\t\t// handful that takes all 256 is not the field described."],
		unknown,
		f"{unknown} names no member of `{placement.type_name}`.") or []


def generate(schema: ast.Schema, resolved: ResolvedSchema,
		basename: str, prefix: str = "situ") -> str:
	"""The capability map held to the Rust accessors, as a test crate root."""
	refuse_parameters(schema)

	enums  = {decl.name for decl in schema.enums()}
	bodies: list[str] = []

	for struct in resolved.structs.values():
		if struct.layout.register is not None:
			continue		# a bus transaction, with no view to acquire
		if not struct.layout.is_byte_sized or struct.layout.size_bytes == 0:
			continue
		extent = struct.layout.size_bytes

		bodies.extend(_short_buffer(basename, struct))
		for entry in own_entries(struct):
			bodies.extend(_occupies(basename, enums, struct,
			                        entry.placement))
			bodies.extend(_round_trip(basename, enums, struct, entry))
			bodies.extend(_decodes(basename, enums, struct, entry.placement))
		for entry in struct.entries:
			bodies.extend(_reserved(basename, resolved, struct,
			                        entry.placement, extent))
			bodies.extend(_must_eq(basename, resolved, struct,
			                       entry.placement, extent))
			bodies.extend(_enum(basename, schema, resolved, struct,
			                    entry.placement, extent))

	emitted = len(CHECK_KINDS) - len(DECLINED)
	lines = [
		f"// Generated by situ {__version__} from {basename}.situ"
		" -- do not edit.",
		"//",
		"// Capability-conformance checks over the Rust accessors: the",
		"// accessors held to the map they were generated beside.",
		"//",
		"//     rustc --edition 2021 --test " f"{basename}_checks.rs",
		"//",
		"// The arithmetic is shared with the other three backends -- which",
		"// value a member must hold, where its bytes land, which arm a",
		"// message carries -- so none of them can disagree about the",
		"// question, only about how its language spells the answer.",
		"//",
		f"// {emitted} of the {len(CHECK_KINDS)} kinds:",
		"//",
		*(f"//   - {kind}" for kind in CHECK_KINDS
		  if kind not in DECLINED),
		"//",
		f"// The {len(DECLINED)} it does not, and why -- said rather than",
		"// left out, because a reader of seven would otherwise take them",
		"// for nine:",
		"//",
		*(f"//   - {kind}: {why}" for kind, why in sorted(DECLINED.items())),
		"//",
		"// What each kind declines for a particular member it says where",
		"// that member would have been checked.",
		"",
		"mod situ_rt;",
		f"mod {basename};",
		"",
		"#[cfg(test)]",
		"mod checks {",
		*(bodies if bodies else [
			"\t#[test]",
			"\tfn nothing_here_takes_a_view() {",
			"\t\t// Every struct in this schema is a register, or none has",
			"\t\t// a minimum extent, so there is nothing to acquire a view",
			"\t\t// over and no check applies.",
			"\t\t//",
			"\t\t// Said as a test rather than left as an empty module: a",
			"\t\t// crate of no tests passes as loudly as one that checked",
			"\t\t// everything, and `example/register` and",
			"\t\t// `example/protobuf` are both.",
			"\t}",
			"",
		]),
		"}",
	]
	return "\n".join(lines) + "\n"
