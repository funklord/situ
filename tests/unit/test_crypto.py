"""The cryptographic model (project.md section 14).

Three properties carry the chapter, and each has its own section below:

- coverage is resolved, not guessed. Which bytes a tag authenticates decides
  what goes stale on a write, so every way of leaving it ambiguous is an error.
- in-place mutation and authentication are in direct conflict (14.2). The
  lattice keeps them on separate axes, because the interesting answer is
  "writable, but it costs you a tag recomputation" rather than either half.
- the stage gate of 14.3 is unrepresentable-by-construction, not discouraged.
"""

from __future__ import annotations

import pytest

from situc import requirements
from situc.capability import Axis, Value
from situc.diagnostics import SituError
from situc.dump import dump
from situc.layout import solve
from situc.parser import parse_text
from situc.propagate import Resolved
from situc.resolve import ResolvedSchema, resolve
from situc.unparse import unparse

CODEC = """codec aead {
	length_preserving;
	seekable = linear;
	granularity = byte;
	authenticated;
	invertible;
	deterministic;
}
"""

PREAMBLE = "endian big;\n" + CODEC


def build(body: str, preamble: str = PREAMBLE) -> ResolvedSchema:
	schema = parse_text(preamble + body)
	return resolve(schema, solve(schema))


def entries(body: str, preamble: str = PREAMBLE) -> dict[str, Resolved]:
	return {
		entry.placement.path: entry
		for struct in build(body, preamble).structs.values()
		for entry in struct.entries
	}


def axis_of(body: str, path: str, axis: Axis, preamble: str = PREAMBLE) -> Value:
	return entries(body, preamble)[path].vector.get(axis)


def discharge(body: str, preamble: str = PREAMBLE) -> list[requirements.Outcome]:
	schema   = parse_text(preamble + body)
	resolved = resolve(schema, solve(schema))
	return requirements.discharge(schema, resolved)


def failure(body: str, preamble: str = PREAMBLE) -> str:
	with pytest.raises(SituError) as caught:
		build(body, preamble)
	return caught.value.diagnostic.render()


SIMPLE = """struct S {
	authenticated {
		u32 seq;
	}
	u8  hop;
	tag u8[16];
}
"""


# -- the front end ----------------------------------------------------------


def test_the_constructs_parse() -> None:
	paths = set(entries(SIMPLE))
	assert "S.authenticated" in paths
	assert "S.seq" in paths
	assert "S.tag" in paths


def test_an_authenticated_block_does_not_open_a_namespace() -> None:
	"""5.3 addresses `Packet.hdr.seq`, not `Packet.authenticated.hdr.seq`.

	The block asserts coverage over members that stay exactly where they were,
	so it cannot rename them. A sealed region is the opposite case: its interior
	is the codec's output, so it does.
	"""
	paths = set(entries(SIMPLE))
	assert "S.seq" in paths and "S.authenticated.seq" not in paths

	sealed = set(entries("""struct S {
		sealed(aead) { u32 inner; }
		tag u8[16];
	}
	"""))
	assert "S.sealed.inner" in sealed and "S.inner" not in sealed


def test_regions_and_tags_may_be_named() -> None:
	paths = set(entries("""struct S {
		authenticated head { u32 seq; }
		sealed body(aead) { u32 inner; }
		tag u8 outer[16] covers(head, body);
	}
	"""))
	assert {"S.head", "S.body", "S.outer"} <= paths


def test_crypto_schemas_round_trip() -> None:
	source = PREAMBLE + SIMPLE
	first  = parse_text(source)
	again  = parse_text(unparse(first))
	assert dump(again) == dump(first)


def test_named_crypto_schemas_round_trip() -> None:
	source = PREAMBLE + """struct S {
		authenticated head { u32 seq; }
		sealed body(aead, nonce = seq) [allow_unverified_read] { u32 inner; }
		checksum u16 crc[1] covers(head);
		tag u8 outer[16] covers(head, body);
	}
	"""
	first = parse_text(source)
	again = parse_text(unparse(first))
	assert dump(again) == dump(first)


def test_a_tag_needs_a_length() -> None:
	with pytest.raises(SituError) as caught:
		parse_text(PREAMBLE + "struct S { authenticated { u8 a; } tag u8; }")
	assert "needs a length" in caught.value.diagnostic.render()


def test_a_bit_packed_tag_is_refused() -> None:
	rendered = failure("struct S { authenticated { u8 a; } tag u3[16]; }")
	assert "whole-byte scalar" in rendered


def test_a_data_dependent_tag_length_is_refused() -> None:
	rendered = failure("""struct S {
		u8 n;
		authenticated { u8 a; }
		tag u8[n];
	}
	""")
	assert "constant length" in rendered


# -- coverage ---------------------------------------------------------------


def test_coverage_is_inferred_as_every_region_in_declaration_order() -> None:
	held = entries("""struct S {
		authenticated head { u32 seq; }
		sealed body(aead) { u32 inner; }
		tag u8[16];
	}
	""")
	assert held["S.tag"].placement.tag_covers == ("head", "body")


def test_an_explicit_covers_clause_overrides_inference() -> None:
	held = entries("""struct S {
		authenticated head { u32 seq; }
		authenticated tail { u32 other; }
		tag u8[16] covers(head);
	}
	""")
	assert held["S.tag"].placement.tag_covers == ("head",)
	assert held["S.seq"].placement.covered_by == ("tag",)
	assert held["S.other"].placement.covered_by == ()


def test_an_unknown_region_in_covers_is_refused() -> None:
	rendered = failure("""struct S {
		authenticated head { u32 seq; }
		tag u8[16] covers(nowhere);
	}
	""")
	assert "unknown region `nowhere`" in rendered
	assert "regions in this struct: head" in rendered


def test_a_region_covered_by_no_tag_is_refused() -> None:
	"""A construct whose meaning is silently nothing is what 14.5 refuses."""
	rendered = failure("struct S { authenticated { u32 seq; } }")
	assert "covered by no tag" in rendered
	assert "nothing to go stale" in rendered


def test_overlapping_coverage_that_does_not_nest_is_refused() -> None:
	rendered = failure("""struct S {
		authenticated a { u32 one; }
		authenticated b { u32 two; }
		authenticated c { u32 three; }
		tag u8 first[16]  covers(a, b);
		tag u8 second[16] covers(b, c);
	}
	""")
	assert "overlap without nesting" in rendered
	assert "neither can be computed first" in rendered


def test_nested_coverage_is_allowed_and_orders_innermost_first() -> None:
	"""Decision 0011: an inner tag's own bytes are input to the outer one."""
	held = entries("""struct S {
		authenticated outer_region {
			u32 seq;
			authenticated inner_region { u32 secret_seq; }
		}
		tag u8 inner[16] covers(inner_region);
		tag u8 outer[16] covers(outer_region, inner_region);
	}
	""")
	assert held["S.secret_seq"].placement.covered_by == ("inner", "outer")
	assert held["S.seq"].placement.covered_by == ("outer",)


def test_a_tag_inside_its_own_coverage_is_refused() -> None:
	rendered = failure("""struct S {
		authenticated head {
			u32 seq;
			tag u8[16] covers(head);
		}
	}
	""")
	assert "inside the region it covers" in rendered
	assert "its own bytes as input" in rendered


def test_two_regions_may_not_share_a_name() -> None:
	rendered = failure("""struct S {
		authenticated head { u32 one; }
		sealed head(aead) { u32 two; }
		tag u8[16];
	}
	""")
	assert "region `head` is declared more than once" in rendered


# -- the auth axis ----------------------------------------------------------


def test_covered_bytes_name_their_tag() -> None:
	held = entries(SIMPLE)
	assert held["S.seq"].vector.get(Axis.AUTH) == Value("Covered", ("tag",))
	assert held["S.hop"].vector.get(Axis.AUTH) == Value("Uncovered")


def test_the_tag_itself_is_not_covered() -> None:
	"""Or finalize would look like it invalidated its own output."""
	assert axis_of(SIMPLE, "S.tag", Axis.AUTH) == Value("Uncovered")


def test_a_tag_is_written_by_finalize_and_by_nothing_else() -> None:
	entry = entries(SIMPLE)["S.tag"]
	assert entry.vector.get(Axis.MUTATE) == Value("Immutable")
	assert [w.rule.name for w in entry.blame(Axis.MUTATE)] == ["tag-field"]


def test_the_auth_axis_unions_tags_on_meet() -> None:
	"""It is a set-valued identity, so a struct under two tags reports both.

	Picking one would lose exactly the information a caller needs: which tags
	go stale when this field is written.
	"""
	resolved = build("""struct S {
		authenticated a { u32 one; }
		authenticated b { u32 two; }
		tag u8 first[16]  covers(a);
		tag u8 second[16] covers(b);
	}
	""")
	struct = resolved.find_struct("S")
	assert struct is not None
	assert struct.vector.get(Axis.AUTH) == Value("Covered", ("first", "second"))


def test_a_checksum_covers_exactly_as_a_tag_does() -> None:
	"""14.1: the two share the entire mechanism.

	Whether the algorithm is a MAC or a CRC changes nothing about which bytes
	have to be recomputed, which is the only question the lattice asks.
	"""
	held = entries("""struct S {
		authenticated head { u32 seq; }
		checksum u16 crc[1];
	}
	""")
	assert held["S.seq"].vector.get(Axis.AUTH) == Value("Covered", ("crc",))
	assert held["S.crc"].vector.get(Axis.MUTATE) == Value("Immutable")


# -- the stage gate (14.3) --------------------------------------------------


SEALED = """struct S {
	u32 nonce_field;
	sealed(aead, nonce = nonce_field) {
		u16 inner_kind;
		u32 inner_seq;
	}
	tag u8[16];
}
"""


def test_a_sealed_interior_is_verify_gated() -> None:
	assert axis_of(SEALED, "S.sealed.inner_seq", Axis.STAGE) == Value("VerifyGated")
	assert axis_of(SEALED, "S.nonce_field", Axis.STAGE) == Value("CompileTime")


def test_allow_unverified_read_is_loud_rather_than_silent() -> None:
	"""14.3 permits the escape hatch and insists it be greppable and reported."""
	body = SEALED.replace("sealed(aead, nonce = nonce_field) {",
	                      "sealed(aead, nonce = nonce_field) [allow_unverified_read] {")
	entry = entries(body)["S.sealed.inner_seq"]

	assert entry.vector.get(Axis.STAGE) == Value("TransformTime")
	assert [w.rule.name for w in entry.blame(Axis.STAGE)] == ["allow-unverified-read"]
	assert "before the tag verifies" in entry.blame(Axis.STAGE)[0].effect.because


def test_a_nonce_must_be_readable_before_the_region_it_seeds() -> None:
	rendered = failure("""struct S {
		sealed(aead, nonce = missing) { u32 inner; }
		tag u8[16];
	}
	""")
	assert "unknown nonce field `missing`" in rendered
	assert "before it can decode" in rendered


def test_a_nonce_declared_after_the_region_is_refused() -> None:
	rendered = failure("""struct S {
		sealed(aead, nonce = later) { u32 inner; }
		u32 later;
		tag u8[16];
	}
	""")
	assert "unknown nonce field `later`" in rendered


# -- requirements (14.2) ----------------------------------------------------


def test_in_place_fails_on_covered_bytes_and_names_both_fixes() -> None:
	"""The diagnostic 14.2 asks for, in full.

	In-place mutation is *possible* here; what it costs is a tag recomputation.
	Saying only "not in place" would be false, and saying only "possible" would
	hide the cost, so the diagnostic says both and prices each fix.
	"""
	with pytest.raises(SituError) as caught:
		discharge(SIMPLE + "require in_place(S.seq);")

	rendered = caught.value.diagnostic.render()
	assert "auth(S.seq) is Covered(tag), required Uncovered" in rendered
	assert "leaves the tag stale until finalize recomputes it" in rendered
	assert "move the field outside the covering region" in rendered
	assert "require in_place_dirty(...)" in rendered


def test_in_place_dirty_passes_where_in_place_fails() -> None:
	outcome = discharge(SIMPLE + "require in_place_dirty(S.seq);")[-1]
	assert outcome.satisfied


def test_in_place_dirty_still_fails_when_the_write_would_move_things() -> None:
	"""It forgives the tag, not the layout."""
	with pytest.raises(SituError) as caught:
		discharge("""struct S {
			u8 n;
			authenticated { u8 body[n]; }
			tag u8[16];
		}
		require in_place_dirty(S.body);
		""")
	assert "mutate(S.body)" in caught.value.diagnostic.render()


def test_no_tag_invalidation_is_statically_checkable() -> None:
	"""14.2 says so explicitly: it passes only if the field is Uncovered."""
	assert discharge(SIMPLE + "require no_tag_invalidation(S.hop);")[-1].satisfied

	with pytest.raises(SituError) as caught:
		discharge(SIMPLE + "require no_tag_invalidation(S.seq);")
	assert "auth(S.seq) is Covered(tag)" in caught.value.diagnostic.render()


def test_in_place_passes_outside_coverage() -> None:
	"""Which is precisely why real protocols put such fields there."""
	assert discharge(SIMPLE + "require in_place(S.hop);")[-1].satisfied


def test_verify_gated_holds_for_a_sealed_region() -> None:
	assert discharge(SEALED + "require verify_gated(S.sealed);")[-1].satisfied


def test_verify_gated_fails_for_bytes_that_were_never_gated() -> None:
	"""An exact demand, not a lower bound: a field nothing gates does not
	satisfy a requirement that nothing can read it before verification."""
	with pytest.raises(SituError) as caught:
		discharge(SEALED + "require verify_gated(S.nonce_field);")

	rendered = caught.value.diagnostic.render()
	assert "stage(S.nonce_field) is CompileTime, required VerifyGated" in rendered


# -- secrets (14.6) ---------------------------------------------------------


def test_a_secret_field_is_marked_secret() -> None:
	assert axis_of("struct S { u8 key[16] [secret]; }", "S.key",
	               Axis.SECRECY) == Value("Secret")


def test_a_secret_may_not_decide_a_length() -> None:
	"""A secret-dependent length is a side channel the schema can rule out."""
	rendered = failure("""struct S {
		u8 n [secret];
		u8 body[n];
	}
	""")
	assert "takes its size from the secret field `n`" in rendered
	assert "visible to anyone counting bytes" in rendered


# -- strictness (14.5) ------------------------------------------------------


def test_lenient_is_not_canonical() -> None:
	body = "strictness = lenient;\nstruct S { u8 a; }\n"
	assert axis_of(body, "S.a", Axis.CANONICAL, preamble="") == Value("NonCanonical")


def test_strict_is_the_default_and_needs_no_directive() -> None:
	assert axis_of("struct S { u8 a; }", "S.a", Axis.CANONICAL,
	               preamble="") == Value("Canonical")


def test_strict_may_be_stated_explicitly() -> None:
	body = "strictness = strict;\nstruct S { u8 a; }\n"
	assert axis_of(body, "S.a", Axis.CANONICAL, preamble="") == Value("Canonical")


def test_an_unknown_strictness_is_refused() -> None:
	with pytest.raises(SituError) as caught:
		parse_text("strictness = whatever;\nstruct S { u8 a; }\n")
	assert "expected `strict` or `lenient`" in caught.value.diagnostic.render()


# -- the canonicity checklist (14.4) ----------------------------------------
#
# Section 14.4 lists every structural source of encoding freedom and calls the
# list a checklist for the implementer. Each row here is one item, so a source
# that stops being detected shows up as a named failure rather than as a
# `require canonical` that quietly starts passing.

CANONICITY = [
	("endian native",
	 "endian native;\nstruct S [allow_host_dependent] { u16 a; }",
	 "endian-native"),
	("reserved [unknown]",
	 "struct S { u8 a; reserved u8 [unknown]; }",
	 "reserved-unknown"),
	("enum default = pass",
	 "enum E : u8 { one = 1, default = pass, }\nstruct S { E a; }",
	 "enum-default-pass"),
	("tlv unknown = preserve",
	 "struct S { tlv opts (tag_type = u8, unknown = preserve); }",
	 "tlv-unknown-preserve"),
	("a non-minimal varint",
	 "varint_type v { encoding = leb128; max_bits = 32; }\nstruct S { v a; }",
	 "non-minimal-varint"),
	("strictness = lenient",
	 "strictness = lenient;\nstruct S { u8 a; }",
	 "strictness-lenient"),
]


@pytest.mark.parametrize(("cause", "body", "rule"), CANONICITY,
	ids=[case[0] for case in CANONICITY])
def test_every_source_of_non_canonicity_is_detected(cause: str, body: str,
		rule: str) -> None:
	entry = next(entry for entry in entries(body, preamble="endian big;\n").values()
	             if entry.vector.get(Axis.CANONICAL).base == "NonCanonical")

	assert rule in [w.rule.name for w in entry.blame(Axis.CANONICAL)], (
		f"{cause} is not blamed on `{rule}`")


def test_a_non_deterministic_codec_is_not_canonical() -> None:
	"""The seventh item, which needs a signature rather than a construct.

	A transform that can produce more than one output for the same input makes
	the region it covers non-canonical whatever the schema around it does.
	"""
	sloppy = CODEC.replace("\tdeterministic;\n", "")
	held   = entries("""struct S {
		sealed(aead) { u32 inner; }
		tag u8[16];
	}
	""", preamble="endian big;\n" + sloppy)

	entry = held["S.sealed.inner"]
	assert entry.vector.get(Axis.CANONICAL) == Value("NonCanonical")


def test_require_canonical_passes_on_a_sealed_deterministic_packet() -> None:
	"""Positional layout plus a deterministic codec: exactly one encoding.

	This is what makes a format signable at all, and it is the property
	protobuf cannot offer for five independent reasons (section 9.7).
	"""
	assert discharge(SEALED + "require canonical(S);")[-1].satisfied
