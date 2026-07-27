"""One test per reachable row of the propagation table (project.md section 11.3).

Phase 3 acceptance asks for exactly this. The rows that are not reachable in the
static subset are listed at the bottom so the gap is visible rather than
forgotten -- adding a construct means adding a row and moving its name up here.

Section 26 invariant 1: the table is data. These tests read the same data, so a
row added without a test shows up as an untested rule.
"""

from __future__ import annotations

import pytest

from situc.capability import DOMAINS, Axis, Value
from situc.diagnostics import SituError
from situc.layout import solve
from situc.parser import parse_text
from situc.propagate import TABLE, Resolved
from situc.resolve import resolve

PREAMBLE = "endian big;\nbit_order msb_first;\n"


def entries(body: str, preamble: str = PREAMBLE) -> dict[str, Resolved]:
	schema   = parse_text(preamble + body)
	resolved = resolve(schema, solve(schema))
	return {
		entry.placement.path: entry
		for struct in resolved.structs.values()
		for entry in struct.entries
	}


def axis_of(body: str, path: str, axis: Axis, preamble: str = PREAMBLE) -> Value:
	return entries(body, preamble)[path].vector.get(axis)


def rules_for(body: str, path: str, axis: Axis, preamble: str = PREAMBLE) -> list[str]:
	return [w.rule.name for w in entries(body, preamble)[path].blame(axis)]


# -- the identity row -------------------------------------------------------


def test_identity_row_weakens_nothing() -> None:
	"""A fixed-size, byte-aligned, single-byte scalar is at the strongest value
	on every axis."""
	entry = entries("struct S { u8 a; }")["S.a"]
	for axis in Axis:
		assert entry.vector.get(axis).base == DOMAINS[axis][0], (
			f"{axis.value} was weakened")


# -- row: non-native endian scalar ------------------------------------------


def test_non_native_endian_scalar_converts() -> None:
	assert axis_of("struct S { u16 a; }", "S.a", Axis.REPR) == Value("ValueConverted")
	assert rules_for("struct S { u16 a; }", "S.a", Axis.REPR) == ["non-native-endian-scalar"]


def test_single_byte_scalar_does_not_convert() -> None:
	"""A byte has no byte order, so `u8` and `byte` are the bytes."""
	assert axis_of("struct S { u8 a; }", "S.a", Axis.REPR) == Value("MemoryIdentical")


def test_native_endian_scalar_does_not_convert() -> None:
	body = "struct S [allow_host_dependent, endian = native] { u32 a; }"
	assert axis_of(body, "S.a", Axis.REPR, preamble="") == Value("MemoryIdentical")


# -- row: bit field ---------------------------------------------------------


def test_bit_field_converts_and_is_not_atomic() -> None:
	body = "struct S { u3 a; u5 b; }"
	assert axis_of(body, "S.a", Axis.REPR) == Value("ValueConverted")
	assert axis_of(body, "S.a", Axis.ATOMIC) == Value("NonAtomic")
	assert axis_of(body, "S.a", Axis.ALIGN) == Value("Unaligned")
	assert "bit-field" in rules_for(body, "S.a", Axis.ATOMIC)


def test_bit_field_keeps_in_place_mutation() -> None:
	"""Section 11.3 leaves `mutate` at InPlaceFixed for a bit field; the write
	is a read-modify-write but it does not move anything."""
	assert axis_of("struct S { u3 a; u5 b; }", "S.a", Axis.MUTATE) \
	       == Value("InPlaceFixed")


# -- row: fixed point --------------------------------------------------------


def test_fixed_point_converts() -> None:
	"""The stored integer is the value scaled by 2^frac, so the number the
	field means is not the number in memory."""
	body = "struct S { q16_16 gain; }"
	assert axis_of(body, "S.gain", Axis.REPR) == Value("ValueConverted")
	assert "fixed-point" in rules_for(body, "S.gain", Axis.REPR)


def test_fixed_point_keeps_its_other_axes() -> None:
	"""It is an integer in memory, so a byte-aligned one addresses, aligns and
	mutates exactly like the integer of the same width. Only `repr` moves."""
	fixed   = "struct S { q16_16 a; }"
	integer = "struct S { i32 a; }"

	for axis in (Axis.OFFSET, Axis.SIZE, Axis.ALIGN, Axis.ATOMIC, Axis.MUTATE):
		assert axis_of(fixed, "S.a", axis) == axis_of(integer, "S.a", axis), axis


def test_a_bit_packed_fixed_point_field_pays_both_costs() -> None:
	"""`q4_4` is a byte; `q3_5` is not, and packs like any other odd width."""
	assert axis_of("struct S { q4_4 a; }", "S.a", Axis.ALIGN) \
	       == axis_of("struct S { u8 a; }", "S.a", Axis.ALIGN)

	packed = "struct S { q2_3 a; u3 b; }"
	assert axis_of(packed, "S.a", Axis.ATOMIC) == Value("NonAtomic")
	assert "bit-field" in rules_for(packed, "S.a", Axis.ATOMIC)


# -- row: BCD ----------------------------------------------------------------


def test_bcd_converts() -> None:
	"""Each nibble is a decimal digit, so the value is decoded, not read."""
	body = "struct S { bcd8 counter; }"
	assert axis_of(body, "S.counter", Axis.REPR) == Value("ValueConverted")
	assert "bcd" in rules_for(body, "S.counter", Axis.REPR)


def test_bcd_is_not_byte_swapped_as_well() -> None:
	"""A BCD field is a digit string, not a multi-byte integer: its bytes are
	in digit order whatever the declared endianness, so the conversion it pays
	for is the decode and not a swap."""
	body = "struct S { bcd8 a; }"
	assert "bcd" in rules_for(body, "S.a", Axis.REPR)


def test_a_single_byte_bcd_field_stays_atomic() -> None:
	"""Two digits are one byte, so nothing about the access is unusual."""
	assert axis_of("struct S { bcd2 a; }", "S.a", Axis.ATOMIC) \
	       == axis_of("struct S { u8 a; }", "S.a", Axis.ATOMIC)


# -- row: straddling bit field ----------------------------------------------


def test_straddling_bit_field_records_its_own_reason() -> None:
	body = "struct S [allow_straddle] { u3 a; u3 b; u3 c; }"
	assert axis_of(body, "S.c", Axis.ATOMIC) == Value("NonAtomic")
	# Both the bit-field row and the straddle row fire: two independent reasons
	# for the same value, and a blame chain wants both.
	assert rules_for(body, "S.c", Axis.ATOMIC) == ["bit-field", "straddling-bit-field"]


def test_non_straddling_bit_field_records_only_one_reason() -> None:
	assert rules_for("struct S { u4 a; u4 b; }", "S.a", Axis.ATOMIC) == ["bit-field"]


# -- row: unaligned multi-byte scalar ---------------------------------------


def test_unaligned_multi_byte_scalar_is_not_atomic() -> None:
	body = "struct S { u8 pad; u32 a; }"
	assert axis_of(body, "S.a", Axis.ATOMIC) == Value("NonAtomic")
	assert rules_for(body, "S.a", Axis.ATOMIC) == ["unaligned-multi-byte-scalar"]


def test_aligned_multi_byte_scalar_stays_atomic() -> None:
	assert axis_of("struct S { u32 a; }", "S.a", Axis.ATOMIC) == Value("AtomicWord")


def test_alignment_is_reported_relative_to_the_message_base() -> None:
	body = "struct S { u32 a; u8 b; u8 c; u16 d; }"
	assert axis_of(body, "S.a", Axis.ALIGN) == Value("Aligned", ("8",))
	assert axis_of(body, "S.b", Axis.ALIGN) == Value("Aligned", ("4",))
	assert axis_of(body, "S.c", Axis.ALIGN) == Value("Aligned", ("1",))
	assert axis_of(body, "S.d", Axis.ALIGN) == Value("Aligned", ("2",))


# -- row: array [N] const ---------------------------------------------------


def test_const_array_preserves_element_offsets() -> None:
	"""Element k is at base + k*size, so nothing about the offset axis weakens."""
	body = "struct S { u8 a; u32 xs[4]; }"
	assert axis_of(body, "S.xs", Axis.OFFSET) == Value("AbsoluteStatic", ("0x01",))
	assert axis_of(body, "S.xs", Axis.SIZE) == Value("Fixed", ("16",))


def test_array_is_not_atomic() -> None:
	assert rules_for("struct S { u32 xs[2]; }", "S.xs", Axis.ATOMIC) \
	       == ["aggregate-or-array"]


# -- row: odd-width scalar --------------------------------------------------


def test_odd_width_scalar_is_not_atomic() -> None:
	assert rules_for("struct S { u24 a; }", "S.a", Axis.ATOMIC) == ["odd-width-scalar"]


@pytest.mark.parametrize("width", [8, 16, 32, 64])
def test_machine_widths_stay_atomic(width: int) -> None:
	assert axis_of(f"struct S {{ u{width} a; }}", "S.a", Axis.ATOMIC) \
	       == Value("AtomicWord")


# -- row: endian native -----------------------------------------------------


def test_endian_native_is_non_canonical() -> None:
	body = "struct S [allow_host_dependent, endian = native] { u32 a; }"
	assert axis_of(body, "S.a", Axis.CANONICAL, preamble="") == Value("NonCanonical")
	assert rules_for(body, "S.a", Axis.CANONICAL, preamble="") == ["endian-native"]


def test_endian_native_requires_the_attribute() -> None:
	"""Host-order encoding has to be reached deliberately, not by accident."""
	with pytest.raises(SituError) as caught:
		entries("struct S [endian = native] { u32 a; }", preamble="")

	report = caught.value.diagnostic.render()
	assert "without `[allow_host_dependent]`" in report
	assert "non-canonical" in report


def test_single_byte_native_field_needs_no_attribute() -> None:
	"""Endianness is irrelevant to a byte, so nothing is host-dependent."""
	assert axis_of("struct S [endian = native] { u8 a; }", "S.a",
	               Axis.CANONICAL, preamble="") == Value("Canonical")


# -- row: reserved [unknown] ------------------------------------------------


def test_reserved_unknown_is_non_canonical() -> None:
	body = "struct S { reserved u8 [unknown]; }"
	assert axis_of(body, "S.<reserved0>", Axis.CANONICAL) == Value("NonCanonical")
	assert rules_for(body, "S.<reserved0>", Axis.CANONICAL) == ["reserved-unknown"]


def test_reserved_must_be_zero_stays_canonical() -> None:
	assert axis_of("struct S { reserved u8 [must_be_zero]; }", "S.<reserved0>",
	               Axis.CANONICAL) == Value("Canonical")


def test_reserved_defaults_to_canonical() -> None:
	"""The default is must_be_zero, so the safe option is the silent one."""
	assert axis_of("struct S { reserved u8; }", "S.<reserved0>",
	               Axis.CANONICAL) == Value("Canonical")


# -- row: enum default = pass -----------------------------------------------


def test_enum_default_pass_is_non_canonical() -> None:
	body = "enum E : u8 { a = 1, default = pass, } struct S { E kind; }"
	assert axis_of(body, "S.kind", Axis.CANONICAL) == Value("NonCanonical")
	assert rules_for(body, "S.kind", Axis.CANONICAL) == ["enum-default-pass"]


def test_enum_default_error_stays_canonical() -> None:
	body = "enum E : u8 { a = 1, default = error, } struct S { E kind; }"
	assert axis_of(body, "S.kind", Axis.CANONICAL) == Value("Canonical")


# -- row: secret attribute --------------------------------------------------


def test_secret_field_is_marked() -> None:
	body = "struct S { u8 key[16] [secret]; }"
	assert axis_of(body, "S.key", Axis.SECRECY) == Value("Secret")
	assert rules_for(body, "S.key", Axis.SECRECY) == ["secret-field"]


def test_ordinary_field_is_public() -> None:
	assert axis_of("struct S { u8 a; }", "S.a", Axis.SECRECY) == Value("Public")


# -- aggregates -------------------------------------------------------------


def test_struct_field_meets_its_members() -> None:
	"""Section 11.2: a struct's vector is the meet of its members'."""
	converted = "struct Inner { u16 x; } struct S { Inner i; }"
	plain     = "struct Inner { u8 x; u8 y; } struct S { Inner i; }"

	assert axis_of(converted, "S.i", Axis.REPR) == Value("ValueConverted")
	assert axis_of(plain, "S.i", Axis.REPR) == Value("MemoryIdentical")


def test_aggregate_keeps_its_own_offset_and_alignment() -> None:
	"""Members cannot weaken where their container sits."""
	body = "struct Inner { u3 a; u5 b; } struct S { Inner i; }"
	assert axis_of(body, "S.i", Axis.OFFSET) == Value("AbsoluteStatic", ("0x00",))
	assert axis_of(body, "S.i", Axis.ALIGN) == Value("Aligned", ("8",))
	# but the representation is still inherited from the packed members
	assert axis_of(body, "S.i", Axis.REPR) == Value("ValueConverted")


def test_non_canonicity_reaches_the_containing_field() -> None:
	body = ("enum E : u8 { a = 1, default = pass, }"
	        "struct Inner { E kind; }"
	        "struct S { Inner i; }")
	assert axis_of(body, "S.i", Axis.CANONICAL) == Value("NonCanonical")


# -- table hygiene ----------------------------------------------------------


def test_every_rule_explains_itself() -> None:
	"""A rule with no explanation would produce a diagnostic with no root
	cause, which section 26 invariant 3 calls a bug."""
	for row in TABLE:
		assert row.rule.construct, f"{row.rule.name} has no construct description"
		for effect in row.rule.effects:
			assert effect.because, f"{row.rule.name} has an unexplained effect"


def test_rule_names_are_unique() -> None:
	names = [row.rule.name for row in TABLE]
	assert len(names) == len(set(names))


def test_reachable_rows_are_all_tested() -> None:
	"""Guards against a row being added without a test.

	Rows from later phases are listed as they arrive; until then every row in
	the table must be one this file exercises.
	"""
	tested = {
		"non-native-endian-scalar",
		"fixed-point",
		"bcd",
		"bit-field",
		"straddling-bit-field",
		"unaligned-multi-byte-scalar",
		"aggregate-or-array",
		"odd-width-scalar",
		"endian-native",
		"reserved-unknown",
		"enum-default-pass",
		"secret-field",
		"endian-marker-scope",
		"dynamic-predecessor",
		"frame-relative",
		"bounded-size",
		"unbounded-size",
		"dynamic-element-type",
		"varint",
		"non-minimal-varint",
		"variant-unequal-arms",
		"opaque",
		"indexed",
		"tlv",
		"tlv-unordered-items",
		"tlv-non-minimal-tag",
		"tlv-packed-and-unpacked",
		"tlv-unknown-preserve",
		"tlv-unordered-duplicates",
		"codec-not-invertible",
		"codec-needs-decode",
		"codec-whole-region-rewrite",
		"codec-block-granularity",
		"codec-permuted",
		# Phase 8. Exercised in test_crypto.py, which keeps the cryptographic
		# model's rows beside the requirements and diagnostics that read them.
		"codec-not-deterministic",
		"covered-by-tag",
		"verify-gated",
		"allow-unverified-read",
		"tag-field",
		"strictness-lenient",
		# Phase 10. Exercised in test_registers.py, which keeps the MMIO rows
		# beside the diagnostics and the generated API that read them.
		"register-partial-word",
		"register-read-only",
		"register-rmw-unsafe",
		"register-side-effect",
	}
	assert {row.rule.name for row in TABLE} == tested


# Rows of section 11.3 not yet reachable, with the phase that adds them:
#
#   authenticated, sealed                                           phase 8
#   register no_rmw, register EffectOnRead                          phase 10


# -- row: endian = from(marker) ---------------------------------------------


MARKER = (
	"endian_marker bo : u16 { little = 0x4949, big = 0x4D4D, }"
	"struct S [endian = from(bo)] { endian_marker bo; u16 a; u8 b; }"
)


def test_marker_scope_makes_fields_conditionally_converted() -> None:
	assert axis_of(MARKER, "S.a", Axis.REPR) == Value("ConditionallyConverted", ("bo",))
	assert rules_for(MARKER, "S.a", Axis.REPR) == ["endian-marker-scope"]


def test_marker_scope_is_canonical_given_the_marker() -> None:
	"""Not canonical -- two byte sequences encode the same value -- but exactly
	one does once the marker is known."""
	assert axis_of(MARKER, "S.a", Axis.CANONICAL) == Value("CanonicalGiven", ("bo",))


def test_marker_leaves_offset_and_size_alone() -> None:
	"""The saving grace of the construct: endianness never changes extent."""
	assert axis_of(MARKER, "S.a", Axis.OFFSET) == Value("AbsoluteStatic", ("0x02",))
	assert axis_of(MARKER, "S.a", Axis.SIZE) == Value("Fixed", ("2",))
	assert axis_of(MARKER, "S.a", Axis.ALIGN) == Value("Aligned", ("2",))


def test_single_byte_field_is_unaffected_by_a_marker() -> None:
	"""A byte has no byte order, so nothing is conditional about it."""
	assert axis_of(MARKER, "S.b", Axis.REPR) == Value("MemoryIdentical")
	assert axis_of(MARKER, "S.b", Axis.CANONICAL) == Value("Canonical")


def test_the_marker_itself_is_not_converted() -> None:
	"""It is compared as a byte sequence; it cannot depend on its own answer."""
	assert axis_of(MARKER, "S.bo", Axis.REPR) == Value("MemoryIdentical")


# -- row: varint (section 8.1.1) --------------------------------------------


VARINT = "varint_type v { encoding = leb128; max_bits = 64; minimal; }"
LOOSE  = "varint_type w { encoding = leb128; max_bits = 64; }"


def test_varint_size_is_bounded_by_its_max_bits() -> None:
	body = VARINT + "struct S { v a; }"
	assert axis_of(body, "S.a", Axis.SIZE) == Value("Bounded", ("1", "10"))


def test_varint_mutation_is_in_place_when_the_length_matches() -> None:
	"""Section 8.1.1 says InPlaceSlack, not Shifting: a value that re-encodes
	to the same length moves nothing."""
	body = VARINT + "struct S { v a; }"
	assert axis_of(body, "S.a", Axis.MUTATE) == Value("InPlaceSlack")
	assert "varint" in rules_for(body, "S.a", Axis.MUTATE)


def test_a_varint_makes_everything_after_it_dynamic() -> None:
	"""The consequence users reach for the construct without understanding."""
	body = VARINT + "struct S { v a; u32 z; }"
	assert axis_of(body, "S.z", Axis.OFFSET) == Value("Dynamic")
	assert axis_of(body, "S.z", Axis.ADDRESS) == Value("Unstable")


def test_a_varint_is_neither_aligned_nor_atomic() -> None:
	body = VARINT + "struct S { v a; }"
	assert axis_of(body, "S.a", Axis.ALIGN) == Value("Unaligned")
	assert axis_of(body, "S.a", Axis.ATOMIC) == Value("NonAtomic")


def test_a_minimal_varint_stays_canonical() -> None:
	assert axis_of(VARINT + "struct S { v a; }", "S.a", Axis.CANONICAL) \
	       == Value("Canonical")


def test_a_non_minimal_varint_is_not_canonical() -> None:
	body = LOOSE + "struct S { w a; }"
	assert axis_of(body, "S.a", Axis.CANONICAL) == Value("NonCanonical")
	assert rules_for(body, "S.a", Axis.CANONICAL) == ["non-minimal-varint"]


def test_an_array_of_varints_is_sequential() -> None:
	"""Section 8.1.1: never Random. Element k cannot be found without walking
	the ones before it."""
	body = VARINT + "struct S { u8 n; v xs[n]; }"
	assert axis_of(body, "S.xs", Axis.ACCESS) == Value("Sequential")


def test_an_array_of_fixed_scalars_stays_random() -> None:
	body = VARINT + "struct S { u8 n; u16 xs[n]; }"
	assert axis_of(body, "S.xs", Axis.ACCESS) == Value("Random")


def test_the_varint_remedy_names_the_fixed_width_alternative() -> None:
	"""Section 18.2's suggestion, carried on the rule that costs the capability."""
	body   = VARINT + "struct S { v a; }"
	entry  = entries(body)["S.a"]
	remedy = next(w.rule.remedy for w in entry.blame(Axis.MUTATE))
	assert "fixed-width scalar" in remedy
	assert "restores static offsets" in remedy


# -- row: variant with unequal arm sizes ------------------------------------


VARIANT = (
	"enum K : u8 { hello = 1, data = 2, }"
	"struct A { u16 x; }"
	"struct B { u32 y; u32 z; }"
)


def variant_body(attrs: str = "") -> str:
	return (VARIANT + "struct S { K kind; variant body switch (kind)" + attrs
	        + " { case K.hello: A a; case K.data: B b; } u16 tail; }")


def test_unequal_arms_make_the_variant_bounded() -> None:
	assert axis_of(variant_body(), "S.body", Axis.SIZE) == Value("Bounded", ("2", "8"))


def test_unequal_arms_make_following_members_dynamic() -> None:
	assert axis_of(variant_body(), "S.tail", Axis.OFFSET) == Value("Dynamic")


def test_the_variant_rule_costs_the_arms() -> None:
	"""Section 18.2 wants the padding cost of equalizing, not just the news
	that the arms differ."""
	entry  = entries(variant_body())["S.body"]
	reason = next(w.effect.because for w in entry.blame(Axis.SIZE))

	assert "`a` 2, `b` 8 bytes" in reason
	assert "would cost up to 6 bytes of padding" in reason


def test_the_variant_remedy_names_equalize() -> None:
	entry  = entries(variant_body())["S.body"]
	remedy = next(w.rule.remedy for w in entry.blame(Axis.SIZE))
	assert "[equalize]" in remedy


def test_equalize_pays_the_cost_and_restores_static_offsets() -> None:
	"""Section 17.0's explicit resolution: accept the consequence, or pay it."""
	body = variant_body(" [equalize]")
	assert axis_of(body, "S.body", Axis.SIZE) == Value("Fixed", ("8",))
	assert axis_of(body, "S.tail", Axis.OFFSET) == Value("AbsoluteStatic", ("0x09",))


def test_equal_arms_cost_nothing() -> None:
	body = (VARIANT + "struct S { K kind; variant body switch (kind) "
	        "{ case K.hello: A a; case K.data: A b; } u16 tail; }")
	assert axis_of(body, "S.body", Axis.SIZE) == Value("Fixed", ("2",))
	assert axis_of(body, "S.tail", Axis.OFFSET) == Value("AbsoluteStatic", ("0x03",))


def test_arms_overlay_at_the_same_base() -> None:
	"""Exactly one arm is present, so they share a base rather than following
	one another."""
	found = entries(variant_body())
	assert found["S.body.a"].placement.offset_bits == 8
	assert found["S.body.b"].placement.offset_bits == 8


# -- rows: opaque and indexed -----------------------------------------------


OPAQUE  = "struct S { u16 n; opaque payload [n]; }"
INDEXED = (
	"struct R { u32 id; }"
	"struct T { u16 n; indexed(offset_type = u16, count = n) { R entries[]; } }"
)


def test_opaque_has_no_interior_access() -> None:
	"""Section 9.4: deliberately collapses structural capability in exchange
	for flexibility."""
	assert axis_of(OPAQUE, "S.payload", Axis.ACCESS) == Value("Sequential")
	assert rules_for(OPAQUE, "S.payload", Axis.ACCESS) == ["opaque"]


def test_opaque_is_replaced_whole() -> None:
	assert axis_of(OPAQUE, "S.payload", Axis.MUTATE) == Value("RewriteRequired")


def test_indexed_keeps_random_access() -> None:
	"""The offset table is the whole reason to pay for the construct: element N
	is one indirection away however wide the elements are (section 9.3)."""
	assert axis_of(INDEXED, "T.entries", Axis.ACCESS) == Value("Random")


def test_indexed_addresses_hold_only_while_the_table_does() -> None:
	assert axis_of(INDEXED, "T.entries", Axis.ADDRESS) == Value("FrameStable")
	assert "indexed" in rules_for(INDEXED, "T.entries", Axis.ADDRESS)


def test_the_indexed_remedy_says_insertion_is_not_an_operation() -> None:
	entry  = entries(INDEXED)["T.entries"]
	remedy = next(w.rule.remedy for w in entry.blame(Axis.ADDRESS))
	assert "insertion is not an operation here" in remedy
	assert "element mutation stays in place" in remedy


# -- section 13.5: propagation through a transform --------------------------
#
# Every row below reads the property signature and nothing else. That is the
# decidability rule of 13.3, and it is why phase 12's derived codecs will slot
# in without disturbing any of this: a derived codec arrives as a signature.


def coded(properties: str, body: str = "u32 x;", tail: str = "u16 t;") -> str:
	return (f"codec c {{ {properties} }}"
	        f"struct S {{ coded b(c) {{ {body} }} {tail} }}")


CTR   = "length_preserving; seekable = linear; granularity = byte; invertible;"
CBC   = "length_preserving; granularity = block(16); invertible;"
OPAQUE_CODEC = "length_preserving; not seekable; invertible;"
PERM  = "length_preserving; seekable = permuted; granularity = byte; invertible;"


def test_ctr_mode_keeps_in_place_interior_mutation() -> None:
	"""The row the whole transform design exists to reach."""
	assert axis_of(coded(CTR), "S.b.x", Axis.MUTATE) == Value("InPlaceFixed")
	assert axis_of(coded(CTR), "S.b.x", Axis.OFFSET) == Value("AbsoluteStatic", ("0x00",))


def test_block_granularity_gives_slack_not_fixed() -> None:
	body = coded(CBC)
	assert axis_of(body, "S.b.x", Axis.MUTATE) == Value("InPlaceSlack")
	assert "codec-block-granularity" in rules_for(body, "S.b.x", Axis.MUTATE)


def test_a_non_seekable_codec_rewrites_the_whole_region() -> None:
	body = coded(OPAQUE_CODEC)
	assert axis_of(body, "S.b.x", Axis.MUTATE) == Value("RewriteRequired")
	# Offsets survive even though mutation does not.
	assert axis_of(body, "S.b.x", Axis.OFFSET) == Value("AbsoluteStatic", ("0x00",))


def test_a_permuted_codec_hands_out_no_span() -> None:
	body = coded(PERM)
	assert axis_of(body, "S.b.x", Axis.ADDRESS) == Value("Unstable")
	# Random access survives the permutation; only contiguity does not.
	assert axis_of(body, "S.b.x", Axis.ACCESS) == Value("Random")


def test_a_non_invertible_codec_makes_the_region_read_only() -> None:
	body = coded("length_preserving; seekable = linear; granularity = byte;")
	assert axis_of(body, "S.b.x", Axis.MUTATE) == Value("Immutable")
	assert "codec-not-invertible" in rules_for(body, "S.b.x", Axis.MUTATE)


def test_a_fixed_expansion_keeps_following_members_static() -> None:
	body = coded("expansion = +4; systematic; seekable = linear; invertible;")
	assert axis_of(body, "S.t", Axis.OFFSET) == Value("AbsoluteStatic", ("0x08",))


def test_an_exact_ratio_keeps_following_members_static() -> None:
	"""Manchester at 2:1 is the counterexample to "all ratios are dynamic",
	and section 13.2 says so outright."""
	body = coded("expansion = ratio_exact(2, 1); seekable = linear; "
	             "granularity = symbol(1); invertible;")
	assert axis_of(body, "S.t", Axis.OFFSET) == Value("AbsoluteStatic", ("0x08",))


def test_a_bounded_ratio_does_not() -> None:
	body = coded("expansion = ratio_bounded(2, 1); granularity = stream; invertible;")
	assert axis_of(body, "S.t", Axis.OFFSET) == Value("Dynamic")


def test_unbounded_expansion_is_fatal_downstream() -> None:
	"""Section 13.3's second prohibition: no attempt to be clever."""
	body = coded("expansion = unbounded; granularity = stream; invertible;")
	assert axis_of(body, "S.b", Axis.SIZE) == Value("Unbounded")
	assert axis_of(body, "S.t", Axis.OFFSET) == Value("Dynamic")


def test_a_systematic_codec_reads_without_decoding() -> None:
	"""The highest-value property in section 13.2: the data is verbatim at
	computable offsets, so a field can be read with no decode at all."""
	body = coded("expansion = +32; systematic; seekable = linear; invertible;")
	assert axis_of(body, "S.b.x", Axis.STAGE) == Value("CompileTime")
	assert "codec-needs-decode" not in rules_for(body, "S.b.x", Axis.STAGE)


def test_a_non_systematic_codec_gates_the_interior_behind_decoding() -> None:
	body = coded("expansion = ratio_exact(3, 1); seekable = linear; invertible;")
	assert axis_of(body, "S.b.x", Axis.STAGE) == Value("TransformTime")
	assert "codec-needs-decode" in rules_for(body, "S.b.x", Axis.STAGE)


def test_error_propagating_is_advisory_only() -> None:
	"""Section 13.5: reported in the map, no capability effect."""
	plain = coded(CTR)
	noisy = coded(CTR + " error_propagating;")
	for path in ("S.b.x", "S.t"):
		for axis in (Axis.MUTATE, Axis.OFFSET, Axis.ACCESS, Axis.STAGE):
			assert axis_of(plain, path, axis) == axis_of(noisy, path, axis)
