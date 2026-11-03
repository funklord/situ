"""Capability-conformance checks over the Python accessors (sections 18, 22).

26.829 found that section 1's four artifacts reach different numbers of its
four languages, and `gen-checks` reached C alone: `situc/codegen/c/checks.py`
is 3239 lines over 70 functions, emitting nine kinds of check and asserting
the `MUTATE`, `OFFSET` and `SIZE` axes of the capability map.

**This is the first of those nine kinds, in Python, and the file it emits
says so.** Porting all nine is a port rather than an increment and does not
belong at the tail of a run; starting with one that stands alone does. The
kind chosen is the bounds check, for three reasons: it is one assertion per
struct rather than per field, it needs nothing but the acquiring call, and it
is the one section 12.3 calls the single check everything after it trusts --
so a backend that got it wrong would have every other guarantee resting on
nothing.

`unittest` rather than pytest, for the reason `python/vectors.py` gives:
section 24 requires `situc` to run from a bare interpreter with no
third-party packages, and a suite handed to a user should ask no more.
"""

from __future__ import annotations

from typing import Literal

from situc import __version__, ast
from situc.capability import Axis
from situc.codegen import (PROBE_BYTES, digit_pattern, placed_bytes,
                           passes_unknown, reserved_policy,
                           refuse_parameters, required_pattern,
                           required_run, selected_arms)
from situc.codegen.python.emit import py_name
from situc.layout import BITS_PER_BYTE, Placement
from situc.propagate import Resolved
from situc.resolve import ResolvedSchema, ResolvedStruct
from situc.traverse import (enclosing_arm, has_computable_extent,
                            inside_an_unresolved_arm, own_entries)
from situc.types import ScalarKind

#: The nine kinds `--target c` emits, so the generated file can name the
#: eight it does not. Written out rather than derived, because the C emitter
#: names them in the test identifiers it builds and there is no list to
#: read -- which is itself worth knowing for whoever ports the rest.
C_KINDS = (
	"refuses a short buffer",
	"a field occupies its claimed bytes",
	"a field round-trips in place",
	"a field decodes a known encoding",
	"a nested member starts where the map says",
	"`must_eq` is enforced",
	"`reserved` must be zero",
	"an enum rejects a value no member names",
	"a framing reader reassembles across every boundary",
)


def generate(schema: ast.Schema, resolved: ResolvedSchema,
		basename: str, prefix: str = "situ") -> str:
	"""A `unittest` module holding each struct to its own minimum extent."""
	refuse_parameters(schema)

	enums   = {decl.name for decl in schema.enums()}
	checked = [struct for struct in resolved.structs.values()
	           if _has_a_minimum(struct)]
	lines = _preamble(basename, len(checked))

	if not checked:
		# A class with no methods is an `IndentationError`, which is how
		# `example/protobuf` failed before this existed. Said rather than
		# left empty: a module that imports and defines nothing reads as a
		# suite that passed.
		lines.extend([
			"\tdef test_nothing_here_has_a_minimum(self) -> None:",
			'\t\t"""No struct in this schema has a view with a minimum'
			' extent."""',
			"\t\tself.skipTest(\"every struct is a register, or its"
			" minimum is zero\")",
			"",
		])

	for struct in checked:
		lines.extend(_short_buffer(basename, struct))
		for entry in own_entries(struct):
			lines.extend(_occupies(basename, struct, entry.placement))
			lines.extend(_round_trip(basename, enums, struct, entry))
			lines.extend(_decodes(basename, enums, struct, entry.placement))
			lines.extend(_nested_start(basename, resolved, struct,
			                           entry.placement))
		# **Once per struct and over `struct.entries`, not `own_entries`**,
		# which is what reaches a reserved field inside a variant arm --
		# and `--target c` has always iterated the same way. The arm is
		# selected by `_prelude` before the field is broken, so the
		# refusal is about that field in a message that carries it.
		lines.extend(_reserved_checks(basename, resolved, struct))
		lines.extend(_must_eq_checks(basename, resolved, struct))
		lines.extend(_enum_checks(basename, schema, resolved, struct))

	lines.extend([
		"",
		'if __name__ == "__main__":',
		"\tunittest.main()",
	])
	return "\n".join(lines) + "\n"


def _has_a_minimum(struct: ResolvedStruct) -> bool:
	"""Whether a buffer can be one byte short of this struct at all.

	Three exclusions, and two of them cost a failing run before they were
	here -- found by running the generated suites over the corpus, which is
	the whole argument for that gate.

	**A register has no view at all.** It is a bus transaction rather than
	bytes off a wire, so its accessors take a device handle and there is no
	`at` to refuse anything; `example/register` raised
	`AttributeError: type object 'dma_status' has no attribute 'at'`.
	`c/fuzz.py` excludes registers for the same reason and says so, and
	`gen-dissector` has since it was written.

	**A zero-byte minimum has no short buffer.** `at` on an empty message
	is the degenerate case rather than a refusal, so the check would assert
	a refusal that correctly does not happen. `example/protobuf` is a bare
	`tlv` region whose minimum is zero, and `c/fuzz.py` carries a warning
	about this exact filter: there it dropped protobuf out of fuzzing
	entirely and left a harness that exercised nothing. Here the struct
	genuinely has nothing to check, so excluding it is right -- and leaving
	the module with an empty class body is not, which is the other half of
	what protobuf found.
	"""
	return (struct.layout.is_byte_sized
	        and struct.layout.register is None
	        and struct.layout.size_bytes > 0)


#: How the count of kinds reads in prose, so the sentence naming it and the
#: list it counts cannot disagree. They did: the sentence said "Two" while
#: the list below it had grown to three, and nothing compared them -- a
#: value written in two places is the shape this tree keeps finding. It is
#: derived from `DONE` now, and `test_the_preamble_counts_its_own_kinds`
#: holds the two together.
_COUNT_WORD = ("no", "One", "Two", "Three", "Four", "Five",
               "Six", "Seven", "Eight", "Nine")

#: The kinds this backend emits: the first `DONE` of `C_KINDS`. One number,
#: so adding a kind moves the sentence, the emitted list and the declined
#: list together.
DONE = 8


def _preamble(basename: str, structs: int) -> list[str]:
	return [
		f'"""Generated by situc {__version__} from {basename}.situ'
		f' -- do not edit.',
		"",
		"Capability-conformance checks over the Python accessors: the",
		"accessors held to the map they were generated beside.",
		"",
		f"    python3 -m unittest {basename}_checks",
		"",
		f"**{_COUNT_WORD[DONE]} of the {_COUNT_WORD[len(C_KINDS)].lower()} kinds `situc gen-checks --target c` emits**,",
		f"over {structs} struct(s):",
		"",
		"  - a buffer one byte short of a struct's minimum must not yield a",
		"    view. Section 12.3 calls that the single check everything after",
		"    it trusts, which is why it was the one to port first -- a",
		"    backend that got it wrong would have every other guarantee",
		"    resting on nothing.",
		"  - a field occupies exactly the bytes the map claims. Each of them",
		"    must reach the value and nothing else may, which is checked by",
		"    flipping every byte of the buffer in turn.",
		"  - a field the map calls writable in place round-trips through its",
		"    setter and moves no byte outside its own claim. The map's own",
		"    verdict is the gate: where it does not say `InPlaceFixed`, no",
		"    check is emitted, so this asserts the `MUTATE` axis where the",
		"    two above assert `OFFSET` and `SIZE`.",
		"  - a byte-aligned field decodes a known encoding: a distinctive",
		"    pattern written into its claimed bytes must read back as the",
		"    number those bytes mean in the declared order. The expectation",
		"    is computed with `int.from_bytes`, which is the standard",
		"    library and not the backend's own read path -- so the check is",
		"    independent of the thing it checks.",
		"  - a nested struct starts where the map says it does, measured",
		"    from the parent. Its interior is checked under its own",
		"    struct, against a buffer where it begins at zero, which says",
		"    nothing about where the parent puts it: mutation testing of",
		"    `--target c` moved every nested member one byte late and that",
		"    whole suite still passed.",
		"  - a `reserved` field holds what its policy says, and",
		"    `validate` refuses it when it does not. Section 8.8: reserved",
		"    bits are malleability control, so a receiver that ignores them",
		"    lets a sender vary bytes the format calls fixed. The first kind",
		"    here to need a BASELINE -- every other constraint satisfied",
		"    before this one is broken -- without which breaking a field in",
		"    an already-invalid buffer would prove nothing. A field inside",
		"    a variant arm is reached too: the arm is selected first, so",
		"    the refusal is about that field in a message that carries it.",
		"    Each field this declines says which and why below.",
		"  - `must_eq` is enforced: a pinned value, broken by adding one",
		"    inside its own width, must be refused on parse. The scalar",
		"    form only -- 49 of the corpus's 67 pins are a foldable",
		"    scalar and the other 18 are byte RUNS, which a number cannot",
		"    break (`\"BM\"` has nothing to add one to), so those are",
		"    declined by name. Same baseline, and the same reason for it.",
		"  - an enum rejects a value no member names, where the schema",
		"    says it should. `default = pass` is section 8.7's deliberate",
		"    escape hatch -- IANA keeps assigning numbers and a receiver",
		"    refusing tomorrow's would refuse tomorrow's traffic -- so an",
		"    enum carrying it has nothing to reject and gets no check.",
		"    The value written is the lowest the MEMBER's own width can",
		"    hold that names no member, not the lowest byte."
		"",
		f"The {_COUNT_WORD[len(C_KINDS) - DONE].lower()} `--target c` also emits and this does not:",
		"",
		*[f"  - {kind}" for kind in C_KINDS[DONE:]],
		"",
		"Saying which rather than leaving them out silently: a suite that",
		"checks one of nine things and does not say so reads as the whole",
		"map having been checked.",
		'"""',
		"",
		"from __future__ import annotations",
		"",
		"import unittest",
		"",
		"import situ_runtime",
		"",
		f"import {basename}",
		"",
		"",
		"class Checks(unittest.TestCase):",
	]


def _break_to(placement: Placement, wrong: int) -> list[str] | None:
	"""Statements setting this member's bits to `wrong`, leaving the rest.

	Read-modify-write rather than a store, because a member narrower than a
	byte shares one with its neighbours: storing would break theirs too,
	and the refusal that followed would be true of the wrong field.

	`placed_bytes` puts the value where the member's own byte order and bit
	order say it goes, so this is the one place that has to know either.
	`None` where the member's bits cannot be placed at all.
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

	return [f"\t\traw[{at}] = (raw[{at}] & {0xFF ^ mask[at]:#04x})"
	        f" | {placed[at]:#04x}"
	        for at in sorted(mask)]


def _select_arm(struct: ResolvedStruct, placement: Placement,
		extent: int) -> list[str] | None:
	"""Statements putting the discriminant where this member exists.

	Empty where the member is in no variant arm, which is most of them.
	`None` where the arm cannot be selected from here, so the caller drops
	the check rather than asserting about bytes nobody selected.
	"""
	found = enclosing_arm(struct, placement)
	if found is None:
		return []

	variant, arm = found
	if arm.value is None:
		return None			# the default arm: no value selects it

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

	# **Clear the bits and write, rather than OR the value in.** The
	# baseline has already been here: an enum discriminant with
	# `default = error` gets its lowest member written to make the buffer
	# valid, and OR-ing 0x02 over that 0x01 selects arm three.
	return [f"\t\t# Select the arm this member is in:"
	        f" {variant.discriminant} = {arm.value}.",
	        *(f"\t\traw[{at}] = {placed[at]:#04x}" if mask[at] == 0xFF
	          else f"\t\traw[{at}] = (raw[{at}] & {0xFF ^ mask[at]:#04x})"
	               f" | {placed[at]:#04x}"
	          for at in sorted(mask))]


def _reach_version(struct: ResolvedStruct, extent: int) -> list[str] | None:
	"""Put the version field where every versioned member is present.

	The same job `_select_arm` does for an arm: a member behind
	`[since = 3]` is not in a message whose version byte is zero, so a
	check that poked its bytes and asserted `validate` would refuse them
	would be asserting about a field the message does not carry --
	`validate` accepts it, correctly.

	The HIGHEST version in the struct rather than this member's own, so
	that the baseline, which satisfies every versioned member's
	constraints, describes a message that actually exists.
	"""
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

	return [f"\t\t# Version {max(versioned)}, so every versioned member"
	        " is here.",
	        *(f"\t\traw[{at}] = {placed[at]:#04x}"
	          for at in sorted(placed))]


def _prelude(resolved: ResolvedSchema, struct: ResolvedStruct,
		placement: Placement, extent: int) -> list[str] | None:
	"""The buffer as a check on this member needs it before breaking it.

	Three things, asked in one place: every constraint that applies to THIS
	message satisfied, the version put where every versioned member is
	present, and the arm this member lives in selected. A member inside an
	arm needs the last two as well as the first -- the baseline that
	satisfies its siblings, and the discriminant that puts them in the
	message at all.

	`None` where any of the three cannot be arranged, so the caller drops
	the check rather than asserting about bytes nobody selected.
	"""
	select = _select_arm(struct, placement, extent)
	if select is None:
		return None

	found = enclosing_arm(struct, placement)
	inside = found[1].member if found is not None else None
	base = _baseline(resolved, struct, extent, inside)
	if base is None:
		return None

	reach = _reach_version(struct, extent)
	if reach is None:
		return None

	lines: list[str] = []
	if base:
		lines += ["\t\t# Every constraint satisfied, so the refusal below is"
		          " about",
		          "\t\t# the field this check breaks and nothing else.",
		          *base, ""]
	if reach:
		lines += [*reach, ""]
	if select:
		lines += [*select, ""]
	return lines


def _enum_checks(basename: str, schema: ast.Schema,
		resolved: ResolvedSchema, struct: ResolvedStruct) -> list[str]:
	"""An enum field admits its members and, with `default = error`,
	nothing else.

	Section 8.7 says so and every backend emitted it as a comment until
	three of them were compared.
	"""
	if not struct.layout.is_byte_sized or struct.layout.size_bytes == 0:
		return []

	extent = struct.layout.size_bytes
	lines: list[str] = []
	for entry in struct.entries:
		lines.extend(_one_enum(basename, schema, resolved, struct,
		                       entry.placement, extent))
	return lines


def _one_enum(basename: str, schema: ast.Schema, resolved: ResolvedSchema,
		struct: ResolvedStruct, placement: Placement,
		extent: int) -> list[str]:
	"""One enum field's rejection check, or a named decline, or nothing."""
	values = resolved.layout.env.enums.get(placement.type_name or "")
	if not values or "[]" in placement.path:
		return []

	# `default = pass` says an unknown value is accepted, which is exactly
	# what section 8.7 offers it for -- IANA keeps assigning numbers, and a
	# receiver refusing tomorrow's would refuse tomorrow's traffic. Not a
	# decline to name: the schema is saying there is nothing to reject.
	if passes_unknown(schema, placement.type_name):
		return []

	reason = None
	unknown = None
	if placement.offset_bits is None:
		reason = ("it sits at an offset the message chooses, so no one"
		          " buffer breaks it")
	elif not placement.size_bits:
		reason = "it owns no bit to break"
	elif inside_an_unresolved_arm(struct, placement):
		reason = ("it is inside a variant arm this backend cannot name, so"
		          " no message can be built that carries it")
	elif (placement.offset_bits + placement.size_bits - 1) \
			// BITS_PER_BYTE >= extent:
		reason = "it lies outside the frame this builds"
	else:
		# **The range is the MEMBER's width, not a byte.** It was 256
		# whatever the field held in `--target c`, beside a gate that
		# skipped anything wider than a byte -- so `ethernet.ethertype`,
		# `mqtt.packet.kind` and twenty-one others declared their members
		# and nobody checked that a value outside them was refused.
		taken = set(values.values())
		unknown = next((v for v in range(1 << placement.size_bits)
		                if v not in taken), None)
		if unknown is None:
			reason = (f"`{placement.type_name}` names every value its"
			          f" {placement.size_bits} bits can hold, so there is"
			          " none to reject")
	if reason is not None:
		return [f"\t# Not enforced here: `{placement.path}`, because"
		        f" {reason}.", ""]

	prelude = _prelude(resolved, struct, placement, extent)
	if prelude is None:
		return [f"\t# Not enforced here: `{placement.path}`, because no"
		        " buffer carrying it with every other constraint",
		        "\t# satisfied can be built, so breaking it would prove"
		        " nothing.", ""]

	assert unknown is not None
	broken = _break_to(placement, unknown)
	if broken is None:
		return [f"\t# Not enforced here: `{placement.path}`, because its"
		        " bits are not ones a value can be placed into.", ""]

	name = py_name(f"{struct.name}_{placement.name}")
	return [
		f"\tdef test_{name}_rejects_a_value_no_member_names(self) -> None:",
		f'\t\t"""`{placement.path}` is a `{placement.type_name}`, which',
		"\t\trejects unknown values (section 8.7). A field declared to",
		"\t\tadmit a handful of values that takes all 256 is not the",
		'\t\tfield the schema described."""',
		f"\t\traw = bytearray({extent})",
		"\t\tmsg = situ_runtime.Message(raw)",
		f"\t\tview = {basename}.{py_name(struct.name)}.at(msg, 0"
		+ ("" if struct.layout.is_fixed_size else f", {extent}") + ")",
		"",
		*prelude,
		"\t\tview.validate()",
		"",
		f"\t\t# {unknown} names no member of `{placement.type_name}`.",
		*broken,
		"\t\twith self.assertRaises(situ_runtime.ConstraintError):",
		"\t\t\tview.validate()",
		"",
	]


def _must_eq_checks(basename: str, resolved: ResolvedSchema,
		struct: ResolvedStruct) -> list[str]:
	"""Every pinned value in this struct, held to its pin.

	Malleability control again, and section 8.8's argument applies
	unchanged: a value the schema does not admit has to be refused on
	parse, or a sender varies bytes the format calls fixed.

	The scalar form only. 49 of the corpus's 67 pins are a foldable scalar
	`[must_eq = ...]`; the other 18 are byte RUNS, which a number cannot
	break -- `"BM"` has nothing to add one to, and a schema admitting
	several alternatives needs a byte outside every one of them. Those are
	declined by name until `--target c`'s pinned-run body is ported too.
	"""
	if not struct.layout.is_byte_sized or struct.layout.size_bytes == 0:
		return []

	extent = struct.layout.size_bytes
	lines: list[str] = []
	for entry in struct.entries:
		lines.extend(_one_must_eq(basename, resolved, struct,
		                          entry.placement, extent))
	return lines


def _one_must_eq(basename: str, resolved: ResolvedSchema,
		struct: ResolvedStruct, placement: Placement,
		extent: int) -> list[str]:
	"""One pin's check, or a named decline, or nothing."""
	from situc.expr import evaluate

	must = next((attr for attr in placement.attrs
	             if attr.name == "must_eq"), None)
	# A pin reaches here two ways: written on the member, and carried by a
	# byte run whose value the schema states -- an enum over runs, or
	# `[must_eq = "BM"]`, which the solver folds into the same field. A
	# reserved run is the third and belongs to `_reserved_checks`.
	run = bool(required_run(placement, every=True))
	if must is None and not (run and placement.kind != "reserved"):
		return []
	if must is not None and must.value is None:
		return []

	reason = None
	if run and placement.kind != "reserved":
		reason = ("its pin is a byte run, which a number cannot break -- it"
		          " wants a byte no pin admits")
	elif placement.offset_bits is None:
		reason = ("its pinned value sits at an offset the message chooses,"
		          " so no one buffer breaks it")
	elif inside_an_unresolved_arm(struct, placement):
		reason = ("it is inside a variant arm this backend cannot name, so"
		          " no message can be built that carries it")
	elif not placement.size_bits:
		reason = "it owns no bit to break"
	elif (placement.offset_bits + placement.size_bits - 1) \
			// BITS_PER_BYTE >= extent:
		reason = "its pinned value lies outside the frame this builds"
	if reason is None:
		assert must is not None and must.value is not None
		try:
			demanded = int(evaluate(must.value, resolved.layout.env))
		except Exception:	# noqa: BLE001 - unfoldable is unbreakable
			reason = "its `must_eq` is not a compile-time value"
	if reason is not None:
		return [f"\t# Not enforced here: `{placement.path}`, because"
		        f" {reason}.", ""]

	prelude = _prelude(resolved, struct, placement, extent)
	if prelude is None:
		return [f"\t# Not enforced here: `{placement.path}`, because no"
		        " buffer carrying it with every other constraint",
		        "\t# satisfied can be built, so breaking it would prove"
		        " nothing.", ""]

	# **Any width, which is why this goes through `_break_to`.** The C
	# version wrote one byte and silently skipped every member that was not
	# exactly one byte wide, so `bmp.signature`, `sqlite.magic` and
	# `image_header.format_version` were all pinned and none was tested.
	# Adding one wraps inside the member's own width, so the result differs
	# from the demanded value at every width above zero.
	wrong = (demanded + 1) & ((1 << placement.size_bits) - 1)
	broken = _break_to(placement, wrong)
	if broken is None:
		return [f"\t# Not enforced here: `{placement.path}`, because its"
		        " bits are not ones a value can be placed into.", ""]

	name = py_name(f"{struct.name}_{placement.name}")
	return [
		f"\tdef test_{name}_must_eq_is_enforced(self) -> None:",
		f'\t\t"""`{placement.path}` is pinned to {demanded}.',
		"",
		"\t\tMalleability control rather than pedantry (section 8.8): a",
		"\t\tvalue the schema does not admit has to be refused on",
		'\t\tparse."""',
		f"\t\traw = bytearray({extent})",
		"\t\tmsg = situ_runtime.Message(raw)",
		f"\t\tview = {basename}.{py_name(struct.name)}.at(msg, 0"
		+ ("" if struct.layout.is_fixed_size else f", {extent}") + ")",
		"",
		*prelude,
		"\t\t# The control: without it a refusal below proves nothing,",
		"\t\t# since a buffer the schema already refuses would refuse",
		"\t\t# whatever this wrote into it.",
		"\t\tview.validate()",
		"",
		f"\t\t# {placement.path} is pinned to {demanded};"
		f" {wrong} is not it.",
		*broken,
		"\t\twith self.assertRaises(situ_runtime.ConstraintError):",
		"\t\t\tview.validate()",
		"",
	]


def _reserved_checks(basename: str, resolved: ResolvedSchema,
		struct: ResolvedStruct) -> list[str]:
	"""Every reserved field in this struct, held to its policy.

	**Section 8.8's reason, which is not pedantry**: reserved bits are
	malleability control. A receiver that ignores them lets a sender vary
	bytes the format calls fixed, which is a signature-bypass primitive in
	anything authenticated.

	Over `struct.entries` rather than `own_entries`, which is what reaches
	a field inside a variant arm -- `_prelude` selects the arm before the
	field is broken, so the refusal is about that field in a message that
	carries it. `--target c` has always iterated this way, and the first
	version of this did not: it was called per own-entry, so 9 of the
	corpus's reserved fields were invisible to it and a per-field decline
	naming them could never fire.

	The baseline is what makes any of it mean something. Breaking a field
	in a buffer that was already invalid proves nothing, and for
	`must_be_one` a zeroed buffer breaks the policy before the check
	touches it -- so the check would pass against a validator that did
	nothing. `view.validate()` has to succeed first.
	"""
	if not struct.layout.is_byte_sized or struct.layout.size_bytes == 0:
		return []

	extent = struct.layout.size_bytes
	lines: list[str] = []
	for entry in struct.entries:
		lines.extend(_one_reserved(basename, resolved, struct,
		                           entry.placement, extent))
	return lines


def _one_reserved(basename: str, resolved: ResolvedSchema,
		struct: ResolvedStruct, placement: Placement,
		extent: int) -> list[str]:
	"""One reserved field's check, or a named decline, or nothing."""
	policy = reserved_policy(placement)
	if policy is None or placement.offset_bits is None:
		return []
	if not placement.size_bits:
		# A zero-width reserved field owns no bit to break, so there is no
		# check and nothing a reader could mistake for one. Silent on
		# purpose, unlike the declines below.
		return []

	reason = None
	if inside_an_unresolved_arm(struct, placement):
		# `enclosing_arm` says "no arm" for a member in an arm it cannot
		# name as well as for one in no arm, and reading the first as the
		# second emits a check about bytes nobody selected (26.839).
		reason = ("it is inside a variant arm this backend cannot name, so"
		          " no message can be built that carries it")
	elif "[]" in placement.path:
		# Deliberate: walking every element on every parse is a cost the
		# caller should choose, so the element type's own `validate` is
		# where that check lives. Demanding it here would fail a design
		# decision rather than a bug.
		reason = "it is an array element, which validates its own"
	elif placement.pinned_runs:
		reason = ("it is a pinned run, whose check is the pin rather than a"
		          " bit policy")
	elif placement.offset_bits + placement.size_bits > extent * BITS_PER_BYTE:
		reason = "its bits lie past the struct's own extent"
	if reason is not None:
		return [f"\t# Not enforced here: `{placement.path}`, because"
		        f" {reason}.", ""]

	prelude = _prelude(resolved, struct, placement, extent)
	if prelude is None:
		return [f"\t# Not enforced here: `{placement.path}`, because no"
		        " buffer carrying it with every other constraint",
		        "\t# satisfied can be built, so breaking it would prove"
		        " nothing.", ""]

	wrong = 0 if policy == "must_be_one" else (1 << placement.size_bits) - 1
	broken = _break_to(placement, wrong)
	if broken is None:
		return [f"\t# Not enforced here: `{placement.path}`, because its"
		        " bits cannot be placed from the map alone", "\t# -- a"
		        " `native` byte order has no one answer here.", ""]

	name = py_name(f"{struct.name}_{placement.name}")
	return [
		f"\tdef test_{name}_{policy}_is_enforced(self) -> None:",
		f'\t\t"""`{placement.path}` is reserved [{policy}].',
		"",
		"\t\tSection 8.8: reserved bits are malleability control, not",
		"\t\tpedantry. A receiver that ignores them lets a sender vary",
		"\t\tbytes the format calls fixed, which is a bypass primitive",
		'\t\tin anything authenticated."""',
		f"\t\traw = bytearray({extent})",
		"\t\tmsg = situ_runtime.Message(raw)",
		f"\t\tview = {basename}.{py_name(struct.name)}.at(msg, 0"
		+ ("" if struct.layout.is_fixed_size else f", {extent}") + ")",
		"",
		*prelude,
		"\t\tview.validate()",
		"",
		f"\t\t# {placement.path} declares {policy}.",
		*broken,
		"\t\twith self.assertRaises(situ_runtime.ConstraintError):",
		"\t\t\tview.validate()",
		"",
	]


def _baseline(resolved: ResolvedSchema, struct: ResolvedStruct,
		extent: int, inside: str | None = None) -> list[str] | None:
	"""Statements making a zeroed buffer satisfy every constraint.

	**Without this a constraint check is vacuous.** It could only show that
	a wrong value is refused, and for `must_be_one` a zeroed buffer already
	breaks it -- so the check would pass against a validator that did
	nothing. Establishing that the RIGHT value is accepted first is what
	gives the refusal its meaning, and it is why the three constraint kinds
	wait on this rather than being written one at a time.

	Ported from `--target c`, whose own version paid for every branch here.
	The arithmetic -- which value a member must hold, where its bytes land,
	which arm a message carries -- is `situc/codegen`'s now rather than a
	backend's, so what is left is turning `{offset: byte}` into Python.

	`inside` is the arm the check about to be built lives in, and the arms
	are why it takes an argument: a member of an arm is in the message only
	when the discriminant selects that arm, so satisfying every arm at once
	writes several mutually exclusive layouts over each other -- Modbus has
	ten exception arms sharing one byte and the baseline set it ten times.

	`None` where some constraint cannot be satisfied from here, because a
	baseline that is not actually valid would make every check built on it
	assert the wrong thing.
	"""
	writes: list[str] = []
	carried = selected_arms(resolved, struct, inside)

	for entry in struct.entries:
		placement = entry.placement
		if placement.offset_bits is None or not placement.size_bits:
			continue

		# A TEXT NUMBER's bytes are digits and a zeroed buffer holds none:
		# `hex u32 ino[8]` reads eight NULs, which is not a number in any
		# radix, so `validate` refuses a fixture nothing had broken yet.
		digits = digit_pattern(resolved, struct, placement)
		if digits is not None:
			if placement.offset_bits % BITS_PER_BYTE:
				return None
			at = placement.offset_bits // BITS_PER_BYTE
			if at + len(digits) > extent:
				return None
			writes.extend(f"\t\traw[{at + i}] = {byte:#04x}"
			              for i, byte in enumerate(digits))
			continue

		# A pinned byte RUN, before the array skip below, because all three
		# spellings of one are arrays and the skip takes every one of them.
		# `edges.sig` is `u8 sig[4] [must_eq = "WOZ2"]`, and skipping it
		# left the baseline claiming a validity it did not have.
		run = required_run(placement)
		if run is not None:
			if placement.offset_bits % BITS_PER_BYTE:
				return None
			at = placement.offset_bits // BITS_PER_BYTE
			if at + len(run) > extent:
				return None
			writes.extend(f"\t\traw[{at + i}] = {byte:#04x}"
			              for i, byte in enumerate(run))
			continue

		if placement.array_count is not None or placement.sized_by is not None:
			continue
		if "[]" in placement.path:
			continue		# an array element, not the array

		# Not this message's arm, so not this message's constraint.
		held = enclosing_arm(struct, placement)
		if held is not None and held[1].member not in carried:
			continue

		value = required_pattern(resolved, struct, placement)
		if value in (None, 0):
			continue		# nothing to write: the buffer starts zeroed
		assert value is not None

		placed = placed_bytes(
			placement.offset_bits, placement.size_bits, value,
			placement.endian, placement.bit_order,
			bool(placement.scalar and placement.scalar.is_bit_packed))
		if placed is None or max(placed) >= extent:
			return None

		writes.extend(f"\t\traw[{at}] |= {placed[at]:#04x}"
		              for at in sorted(placed) if placed[at])

	return writes


def _nested_start(basename: str, resolved: ResolvedSchema,
		struct: ResolvedStruct, placement: Placement) -> list[str]:
	"""Where a nested struct sits inside its parent.

	**The hole this closes is specific and was found by mutation testing in
	`--target c`**: a nested struct's interior is checked thoroughly, but
	under its own struct's section, against a buffer where it begins at
	zero. Moving every nested member one byte late passed that whole suite,
	because each nested field still agreed with every other nested field.
	Only this says where the PARENT puts it.

	`_at` is the view's absolute offset in the message, so the subtraction
	is the parent-relative start -- the same quantity C reads as
	`inner.base - view.base`, and the reason this is one assertion rather
	than a re-derivation: recomputing the offset from the parent would be
	asking the emitter what it thinks twice.
	"""
	if placement.kind != "field" or placement.type_name is None:
		return []
	nested = resolved.structs.get(placement.type_name)
	if nested is None:
		return []		# a scalar member; the two kinds above hold those

	# **Past here the member IS a nested struct, so a decline is named.**
	# 35 of the corpus's 72 nested members can be located and 37 cannot,
	# and a reader of a suite that said nothing would take the 35 for all
	# of them -- which is the argument the preamble already makes about the
	# four kinds this backend does not emit, applied inside a kind. The
	# skip is a comment rather than a failing test because each reason is
	# the map being right.
	offset_bits = placement.offset_bits
	reason: str | None = None
	if offset_bits is None:
		reason = "its offset is one the message decides"
	elif placement.array_count is not None:
		reason = "it is an array, whose elements the two kinds above hold"
	elif placement.repeat_while is not None or placement.delimiters:
		reason = "it is a run, reached through an `at` rather than a property"
	elif placement.sized_by is not None:
		reason = "its extent is driven by another member"
	elif placement.sealed_by is not None:
		reason = "it is reached through a gate, which has its own checks"
	elif offset_bits % BITS_PER_BYTE:
		reason = "it does not start on a byte"
	elif not struct.layout.is_byte_sized or struct.layout.size_bytes == 0:
		reason = f"`{struct.name}` has no byte extent to acquire"
	elif not nested.layout.is_fixed_size \
			and not has_computable_extent(resolved.structs, nested):
		reason = ("its type cannot be measured from its own bytes, so there"
		          " is no sub-view accessor")
	if reason is not None:
		return [f"	# Not located: `{placement.path}`, because {reason}.",
		        ""]
	assert offset_bits is not None, "the None case is named above"

	size   = struct.layout.size_bytes
	offset = offset_bits // BITS_PER_BYTE
	member = py_name(placement.name)
	name   = py_name(f"{struct.name}_{placement.name}")
	return [
		f'	def test_{name}_starts_where_the_map_says(self) -> None:',
		f'		"""`{placement.path}` starts {offset} byte(s) into'
		f' `{struct.name}`."""',
		f"		raw = bytearray({size})",
		f"		msg = situ_runtime.Message(raw)",
		f"		view = {basename}.{py_name(struct.name)}.at(msg, 0"
		+ ("" if struct.layout.is_fixed_size else f", {size}") + ")",
		"",
		f"		inner = view.{member}",
		"",
		f"		# The map's offset, measured from the parent rather than",
		f"		# recomputed from it. Its own fields are checked under its",
		f"		# own struct, where it begins at zero; only this says where",
		f"		# the parent actually puts it.",
		f"		self.assertEqual(inner._at - view._at, {offset},",
		f'			"{placement.path} does not start where the map says")',
		"",
	]


def _decodes(basename: str, enums: set[str], struct: ResolvedStruct,
		placement: Placement) -> list[str]:
	"""A byte-aligned field reads the number its bytes mean.

	The other three kinds ask WHERE a value comes from and whether writing
	it disturbs anything. This one asks WHAT the bytes mean -- the byte
	order, the width and the signedness -- which is the part of the map a
	reader of the generated code is most likely to take on trust.

	**The expectation is computed with `int.from_bytes`**, the standard
	library, rather than by the rule the backend reads with. That is what
	keeps the check independent of the thing it checks: an emitter that had
	the byte order backwards would produce a getter and an expectation that
	agreed, and this would still fail.

	`0x81` leads the pattern so the top bit is set, which is what makes a
	signed field's sign extension part of the claim rather than an untested
	corner.

	Byte-aligned only. A bit-packed field's value is a shift and a mask
	inside its byte, so the pattern and the expectation both need the bit
	position -- a different computation, and one the next pass should take
	rather than fold in here where it would read as the same check.
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

	# `int.from_bytes` is the independent implementation, so it has to be an
	# implementation OF the thing being checked. It is that for a plain
	# integer and for nothing else, so the test is a whitelist rather than a
	# list of kinds to skip: a scalar kind added later declines until
	# somebody writes the independent decoder for it.
	#
	# `bcd2` is the measured case. The corpus's RTC holds its year as packed
	# decimal, where each nibble is a digit -- so the byte 0x81 means 81 and
	# the getter was right to say so against this check's 129. Fixed point is
	# the other exclusion and a subtler one: the two `*FIXED` fields pass
	# today, because the getter hands back the stored integer and the
	# schema's own comment says the caller applies the scale. But `REPR` says
	# only `ValueConverted` for both those and for BCD, where the getter DOES
	# convert -- so the map does not say whose the scaling is, and a check
	# that passed by relying on the answer would be resting on something
	# unstated. Recorded at 26.832 rather than depended on.
	if placement.scalar.kind not in (ScalarKind.UINT, ScalarKind.SINT):
		return []
	if placement.scalar.frac_bits:
		return []

	# **`--target c` declines a covered field here and this does not**, which
	# is a difference between the backends rather than a gap in one. C's
	# reason is that a covered field is "reached through the coverage
	# machinery, not a plain read"; in Python it is a plain read --
	# `icmp_message.type` is `repr=MemoryIdentical` over `self._read`, and
	# coverage costs it only its setter, which the round-trip kind above
	# already gates on. 40 of the corpus's byte-aligned scalars are covered,
	# and they are a real part of the 129 the sabotage above fails, so
	# copying C's exclusion would have thrown away checks that work.

	width = placement.size_bits // BITS_PER_BYTE
	if width < 1 or width > 8:
		return []

	first = placement.offset_bits // BITS_PER_BYTE
	size  = struct.layout.size_bytes
	if first + width > size:
		return []

	# **Asked positively, because there are more than two answers.** The
	# first version of this read `"big" if endian is BIG else "little"`,
	# which is true of the two cases that motivated it and quietly wrong
	# about every other -- the defect shape situ keeps finding, a two-branch
	# predicate absorbing a third state into one arm.
	#
	# There are two such states in the corpus and `endian native` was only
	# the one I had thought of. It means the host decides, so no single
	# number is what the bytes mean. The other is `endian_marker`: TIFF's
	# first two bytes say which order the rest of the file uses, so the
	# resolved placement carries `endian=None` and the order is a runtime
	# value. That `None` fell into the `little` arm, and the check failed
	# against an accessor that was reading correctly.
	if placement.endian is ast.Endian.BIG:
		order: Literal["big", "little"] = "big"
	elif placement.endian is ast.Endian.LITTLE:
		order = "little"
	else:
		return []

	pattern  = bytes(PROBE_BYTES[:width])
	expected = int.from_bytes(pattern, order,
			signed=placement.scalar.signed)

	name    = py_name(f"{struct.name}_{placement.name}")
	acquire = (f"{basename}.{py_name(struct.name)}.at(msg, 0"
	           + ("" if struct.layout.is_fixed_size else f", {size}") + ")")
	return [
		f"	def test_{name}_decodes_a_known_encoding(self) -> None:",
		f'		"""{width} byte(s) at {first}, {order} order,'
		f' signed={placement.scalar.signed}."""',
		f"		raw = bytearray({size})",
		"		msg = situ_runtime.Message(raw)",
		f"		view = {acquire}",
		"",
		f"		raw[{first}:{first + width}] = bytes.fromhex"
		f'("{pattern.hex()}")',
		"",
		f"		# {expected} is what those bytes mean in {order} order,",
		"		# computed by `int.from_bytes` -- the standard library,",
		"		# not the rule the accessor reads with. An emitter with",
		"		# the byte order backwards would agree with itself and",
		"		# still fail here.",
		f"		self.assertEqual(view.{py_name(placement.name)},"
		f' {expected},',
		f'			"{placement.name} does not decode its own bytes")',
		"",
	]


def _round_trip(basename: str, enums: set[str],
		struct: ResolvedStruct, entry: Resolved) -> list[str]:
	"""A field the map calls writable in place round-trips and moves nothing.

	**The map's own verdict is the gate**, which is the part of this check
	worth getting right. Where `mutate` is not `InPlaceFixed` the field is
	not claimed to be writable without moving anything, so no check is
	emitted -- and a backend that emitted a setter anyway would be caught by
	the absence of a check rather than by one, which is why the generated
	module says how many it emitted.

	This is the first of the kinds here to assert `MUTATE`; the two above
	assert `OFFSET` and `SIZE`.

	Writes `1` into a zeroed buffer, which is in range for any scalar of at
	least one bit and is a change from what is there, so a setter that did
	nothing would be caught -- which writing the value back unchanged would
	not. Enum-typed fields are declined for the same reason the vector
	suites decline them in Rust: `1` need not name a member, and the getter
	is entitled to refuse a value the enum does not have.
	"""
	placement = entry.placement
	if entry.vector.get(Axis.MUTATE).base != "InPlaceFixed":
		return []

	# **The gate is two axes, not one**, and the second cost five schemas
	# before it was here. `mutate = InPlaceFixed` says writing the field
	# moves no bytes; `auth = Covered` says writing it leaves an
	# authentication tag stale, and the backend withholds the plain setter
	# for that reason (section 14.2). So a field can be writable in place
	# and have no setter at all -- `tcp_header.urgent` and
	# `usb.token.address` are both, and both raised
	# `AttributeError: property 'urgent' of 'tcp_header' object has no
	# setter`. Writing one is a different operation with a finalize after
	# it, which is its own check rather than this one.
	if entry.vector.get(Axis.AUTH).base != "Uncovered":
		return []
	if placement.offset_bits is None or placement.kind != "field":
		return []
	if placement.scalar is None or placement.radix is not None:
		return []
	if placement.size_max_bits != placement.size_bits:
		return []
	if placement.array_count is not None:
		return []
	if placement.type_name in enums:
		return []
	if not struct.layout.is_byte_sized or struct.layout.size_bytes == 0:
		return []

	first = placement.offset_bits // BITS_PER_BYTE
	last  = (placement.offset_bits + placement.size_bits - 1) // BITS_PER_BYTE
	size  = struct.layout.size_bytes
	if last >= size:
		return []

	name    = py_name(f"{struct.name}_{placement.name}")
	acquire = (f"{basename}.{py_name(struct.name)}.at(msg, 0"
	           + ("" if struct.layout.is_fixed_size else f", {size}") + ")")
	return [
		f"	def test_{name}_round_trips_in_place(self) -> None:",
		f'		"""The map calls `{struct.name}.{placement.name}` writable in'
		f' place."""',
		f"		raw = bytearray({size})",
		"		msg = situ_runtime.Message(raw)",
		f"		view = {acquire}",
		"",
		f"		view.{py_name(placement.name)} = 1",
		"",
		"		# Read back through a FRESH view. `InPlaceFixed` says",
		"		# writing this field moves no bytes; it does not say a",
		"		# view taken before the write survives it.",
		"		# `test/schema/shared`'s `len` drives another member's",
		"		# extent, so writing it bumps the message generation and",
		"		# the old view is correctly refused with",
		"		# `StaleViewError` -- section 12.3's typestate working,",
		"		# not a field that failed to round-trip. Whether bytes",
		"		# MOVED is asked of `raw` below, which no generation",
		"		# affects.",
		f"		view = {acquire}",
		f'		self.assertEqual(view.{py_name(placement.name)}, 1,'
		f' "{placement.name} did not take 1")',
		"",
		f"		for i in range({size}):",
		f"			if {first} <= i <= {last}:",
		"				continue",
		"			self.assertEqual(raw[i], 0,",
		f'				f"writing {placement.name} moved byte {{i}}")',
		"",
	]


def _occupies(basename: str, struct: ResolvedStruct,
		placement: Placement) -> list[str]:
	"""A field reads exactly the bytes the map claims for it, and no others.

	The heart of the artifact: the map is a claim about which bytes a value
	comes from, and this is the only check that reads it back. Flipping each
	byte in turn and asking whether the getter noticed tests the claim from
	outside, which is what makes it independent of how the accessor was
	emitted.

	`noticed` rather than "every claimed byte changes the value": a field
	narrower than its byte, or one whose value does not depend on every bit
	of it, legitimately does not notice a flip in a bit it does not use. The
	C version makes the same distinction and for the same reason -- the
	claim is that the value comes from THESE bytes and from no others, not
	that it uses all of them.

	Declined where the claim cannot be read back:

	- a dynamic offset, where the map has no byte to name;
	- a field with no getter -- `reserved`, a marker, a `parameter`;
	- a text number, whose getter RAISES on digits that are not digits, so
	  flipping a byte does not change the value but refuses to produce one,
	  and the check would read a refusal as a change;
	- **a field whose extent is not fixed**, which has no definite claimed
	  range to flip. `udp_header.payload` is `[remaining]`, so at the
	  struct's minimum it is ZERO bytes long and flipping its range changes
	  nothing -- the check asserted that some claimed byte reaches the value
	  and 22 of the corpus's 42 schemas said otherwise, every one of them
	  correctly. The map claims a byte range for a field at a static offset
	  with a fixed extent, and for no other.
	"""
	if placement.offset_bits is None or placement.kind != "field":
		return []
	if placement.scalar is None or placement.radix is not None:
		return []
	if placement.size_max_bits != placement.size_bits:
		return []
	if not struct.layout.is_byte_sized or struct.layout.size_bytes == 0:
		return []

	first = placement.offset_bits // BITS_PER_BYTE
	last  = (placement.offset_bits + placement.size_bits - 1) // BITS_PER_BYTE
	size  = struct.layout.size_bytes
	if last >= size:
		return []

	# A byte run's property is a `memoryview`, which compares equal to
	# bytes; `bytes(...)` is what makes a difference printable and is the
	# same cast the vector suites use.
	read = f"view.{py_name(placement.name)}"
	if placement.array_count is not None:
		read = f"bytes({read})"

	name = py_name(f"{struct.name}_{placement.name}")
	return [
		f"	def test_{name}_occupies_its_claimed_bytes(self) -> None:",
		f'		"""The map claims bytes {first}..{last} of'
		f' `{struct.name}` for `{placement.name}`."""',
		f"		raw = bytearray({size})",
		"		msg = situ_runtime.Message(raw)",
		f"		view = {basename}.{py_name(struct.name)}.at(msg, 0"
		+ ("" if struct.layout.is_fixed_size else f", {size}") + ")",
		"",
		f"		before = {read}",
		"		noticed = False",
		f"		for i in range({first}, {last} + 1):",
		"			raw[i] ^= 0xFF",
		f"			if {read} != before:",
		"				noticed = True",
		"			raw[i] ^= 0xFF",
		f'		self.assertTrue(noticed, "no claimed byte reaches'
		f' {placement.name}")',
		"",
		f"		for i in range({size}):",
		f"			if {first} <= i <= {last}:",
		"				continue",
		"			raw[i] ^= 0xFF",
		f"			self.assertEqual({read}, before,"
		f' f"byte {{i}} must not reach {placement.name}")',
		"			raw[i] ^= 0xFF",
		"",
	]


def _short_buffer(basename: str, struct: ResolvedStruct) -> list[str]:
	minimum = struct.layout.size_bytes
	short   = minimum - 1
	# A variable struct's `at` takes the message length and a fixed one's
	# does not, which is the Python backend's own branch on
	# `layout.is_fixed_size` -- the thing 26.829's vector emitter got wrong
	# for 13 of 27 schemas before it was run.
	extent = "" if struct.layout.is_fixed_size else f", {short}"

	return [
		f"\tdef test_{py_name(struct.name)}_refuses_a_short_buffer"
		f"(self) -> None:",
		f'\t\t"""`{struct.name}` needs {minimum} bytes; {short} must refuse."""',
		f"\t\traw = bytearray({short})",
		"\t\tmsg = situ_runtime.Message(raw)",
		"",
		"\t\twith self.assertRaises(situ_runtime.BoundsError):",
		f"\t\t\t{basename}.{py_name(struct.name)}.at(msg, 0{extent})",
		"",
	]
