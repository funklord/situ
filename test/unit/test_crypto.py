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

from situc import requirements, traverse
from situc.capability import Axis, Value
from situc.codegen.c import generate as generate_c
from situc.codegen.cpp import generate as generate_cpp
from situc.codegen.python import generate as generate_py
from situc.codegen.rust import generate as generate_rs
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


SELF_MEASURED = [
	("delimited",   "u8   body[] until ','        [secret];"),
	("record run",  "Item body[] until \"\\r\\n\"     [secret];"),
	("while run",   "Item body[] while (more == 1) [secret];"),
	("varint",      "vint body                     [secret];"),
]


@pytest.mark.parametrize("what, member", SELF_MEASURED,
                         ids=[what for what, _ in SELF_MEASURED])
def test_a_secret_may_not_measure_itself(what: str, member: str) -> None:
	"""A `[secret]` whose own length is read from its own bytes.

	The other cases in this section are about a secret deciding where
	SOMETHING ELSE sits. This is the secret deciding where it itself ends,
	which leaks by the same route and one step earlier: the encoded length
	is a function of the value, so an observer counting bytes reads part of
	the secret off the wire without touching the cipher, and everything
	after it moves.

	All four were accepted -- `size=Unbounded` for the three runs and
	`Bounded(1, 10)` for the varint, with the enclosing struct `3..` and
	`3..12` respectively.
	"""
	rendered = failure("varint_type vint { encoding = leb128; max_bits = 64; }\n"
	                   "struct Item { u8 more; u8 a; }\n"
	                   "struct S {\n\t" + member + "\n\tu8 tail[2];\n}\n")
	assert "its own length is read from its own bytes" in rendered
	assert "visible to anyone counting bytes" in rendered


ERASER = "struct S { u8 n; u8 key[n] [secret]; u16 tail; }"


def _generated(body: str) -> dict[str, str]:
	"""One schema through all four backends, as text."""
	schema   = parse_text(PREAMBLE + body)
	resolved = resolve(schema, solve(schema))
	return {
		"c":      generate_c(schema, resolved, "unit").header,
		"cpp":    generate_cpp(schema, resolved, "unit").header,
		"rust":   generate_rs(schema, resolved, "unit").module,
		"python": generate_py(schema, resolved, "unit").module,
	}


def _eraser_body(source: str) -> str:
	"""The eraser, from its name to the end of it.

	A fixed character window reached the end of Python's docstring and no
	further, so the assertion below read prose rather than code -- and
	passed or failed on how long a comment happened to be, which is the
	kind of test that goes green for the wrong reason later.
	"""
	after = source.split("key_zeroize", 1)[1]
	for end in ('"""', "\n\t}", "\n\tdef ", "\n}"):
		if end in after:
			after = after.split(end, 1)[1] if end == '"""' else after
	# Everything up to whichever terminator comes first.
	cuts = [after.index(e) for e in ("\n\tdef ", "\n\t}", "\n}")
	        if e in after]
	return after[:min(cuts)] if cuts else after


def test_every_backend_erases_a_data_sized_secret() -> None:
	"""The shape both committed `[secret]` fixtures happen not to be.

	`packet`'s `session_key[16]` and `keystore`'s `secret_key[32]` are
	constant spans, so an eraser that handled only constants passed
	everything there was to pass. `u8 key[n] [secret]` is the other half and
	all four got it wrong, in two different directions:

	C read the declared length and did not clamp it, so `n` = 255 in a
	six-byte frame wrote 255 bytes -- a wire-controlled heap overflow, which
	ASan named and `test_spans.c` now catches with a canary. C++, Rust and
	Python guarded on `size_bits is None`, and a data-sized member's
	`size_bits` is ZERO rather than absent, so all three emitted a
	zero-length erase behind a comment promising erasure (26.316).

	Asserting the clamp rather than the erase, because the clamp is what
	distinguishes the fix from either bug: a zero-length erase has no clamp
	in it and an unclamped one has no minimum.
	"""
	text = _generated(ERASER)
	body = {name: _eraser_body(source) for name, source in text.items()}

	assert "situ_min_u32"   in body["c"]
	assert "situ_min_u32"   in body["cpp"]
	assert "core::cmp::min" in body["rust"]
	assert "min("           in body["python"]

	# And none of them still says "erase zero bytes", which is what the
	# three constant-span backends emitted here.
	assert ", 0u)"   not in body["c"]
	assert ", 0u)"   not in body["cpp"]
	assert "[1..1]"  not in body["rust"]
	assert "bytes(0)" not in body["python"]


def test_every_backend_still_erases_a_constant_span() -> None:
	"""The half that worked, kept honest while the other half was added.

	A constant span needs no clamp and gets none: reading the length out of
	the bytes to erase a fixed sixteen would be slower and no safer, and the
	two paths are told apart by `size_bits` being non-zero.
	"""
	text = _generated("struct S { u8 key[16] [secret]; u16 tail; }")

	assert "situ_zeroize(situ_base(view) + 0u, 16u);" in text["c"]
	assert "situ_zeroize(situ_base(raw_) + 0, 16u);" in text["cpp"]
	assert "self.bytes[0..16]" in text["rust"]
	assert "start + 16] = bytes(16)" in text["python"]


def test_a_secret_whose_length_is_public_is_allowed() -> None:
	"""The two controls, and the reason the rule is not "size is Unbounded".

	`[remaining]` is Unbounded and its length belongs to the frame, which an
	observer already knows from the message it is holding; `key[n]` is
	Bounded and its length is in the clear in `n`. Refusing either would
	forbid the ordinary ways to carry a key and buy nothing, and the first
	would be refused by a rule keyed on the size axis -- which is why the
	rule is keyed on who decides the length instead.
	"""
	build("struct S { u8 hdr; u8 rest[remaining] [secret]; }")
	build("struct S { u8 n; u8 key[n] [secret]; u8 tail[2]; }")


def test_a_secret_may_not_decide_a_computed_length() -> None:
	"""The same leak with arithmetic in the way.

	`sized_by` holds a path and holds nothing for `body[n * 2]`, so for as
	long as the check read only that, the commonest shape of a
	secret-dependent length walked straight past it -- `size=1..511`, the
	extent moving with the secret, no complaint.
	"""
	rendered = failure("""struct S {
		u8 n [secret];
		u8 body[n * 2];
	}
	""")
	assert "computes its size from the secret field `n`" in rendered
	assert "visible to anyone counting bytes" in rendered


def test_a_secret_may_not_decide_an_offset() -> None:
	"""A secret offset leaks without any length changing.

	The struct stays one size, so nobody counting bytes learns anything --
	and the accessor touches whichever bytes the secret names, which is the
	data-dependent access pattern 14.6 forbids in the same breath.
	"""
	rendered = failure("""struct S {
		u8 off [secret];
		u8 pad[4];
		u8 body[2] at off;
	}
	""")
	assert "is placed at an offset from the secret field `off`" in rendered
	assert "watching memory" in rendered


VARIANT = """struct A { u8 a; }
	struct B { %s b; }
	struct S {
		u8 kind [secret];
		variant body switch (kind) %s {
			case 1:  A as_a;
			case 2:  B as_b;
			default: error;
		}
	}
	"""


def test_a_secret_may_not_select_an_arm() -> None:
	"""The discriminant half, which 14.6 has always named.

	The function enforcing this said "a length or a discriminant" in its own
	docstring while checking only the length, so `size=2..3` compiled: the
	extent says which arm was taken and therefore something about the key.
	"""
	rendered = failure(VARIANT % ("u16", ""))
	assert "selects an arm using the secret field `kind`" in rendered
	assert "the arms differ in width" in rendered


def test_a_secret_discriminant_is_refused_however_the_arms_are_shaped() -> None:
	"""Equal widths, and `[equalize]`, and neither is a way out.

	The first version of this rule permitted both, on the reasoning that the
	extent is what an observer counts and padding the arms removes it. That
	is true and it is one of two channels: reading an arm emits
	`if (kind != 1u)`, so the accessor branches on the secret whatever the
	arms are shaped like, and that is the bullet after the one about layout.

	Both shapes are here rather than one because they even the extent by
	different routes -- equal widths by accident of the schema, `[equalize]`
	on purpose -- and a rule conditioned on the extent let both through.
	"""
	for arms, attr in (("u8", ""), ("u16", "[equalize]")):
		rendered = failure(VARIANT % (arms, attr))
		assert "selects an arm using the secret field `kind`" in rendered
		assert "branches on secret material" in rendered
		assert "the arms differ in width" not in rendered, \
			"the extent is even here, so only the branch may be cited"
	assert "not the remedy here" in failure(VARIANT % ("u16", "[equalize]")), \
		"and `[equalize]` must be named as a non-remedy, since it is the " \
		"first thing an author reaching for it will try"


def test_equal_width_arms_do_not_invalidate_a_view() -> None:
	"""The other half of the same discovery, which is not about secrets.

	`arm_sizes` pairs each size with its arm's name, so the obvious
	`len(set(arm_sizes)) > 1` counts ARMS -- true of every variant worth
	writing. `invalidating_members` reached for it and so told the
	generators that writing any discriminant shifts the bytes after it.

	`icmp_message` is the committed example: six arms, every one 32 bits,
	and its header told callers to re-acquire their views after writing
	`type` while its setter bumped the generation. Nothing moves.
	"""
	built = build("""struct A { u8 a; }
	struct G { u8 g; }
	struct S {
		u8 kind;
		variant body switch (kind) {
			case 1:  A as_a;
			case 2:  G as_g;
			default: error;
		}
	}
	""")
	placement = {entry.placement.name: entry.placement
	             for entry in built.structs["S"].entries}["body"]
	assert len(placement.arm_sizes) == 2, "two arms, so the pair-set says 2"
	assert placement.arm_widths == {8}, "one width, which is the question"
	assert traverse.invalidating_members(built.structs)["S"] == set()


LAYOUT_DECIDING = """struct A { u8 a; }
struct B { u16 b; }
struct S {
	u8 n;
	u8 m;
	u8 off;
	u8 kind;
	u8 named[n];
	u8 arith[m * 2];
	u8 spot[1] at off;
	variant body switch (kind) {
		case 1:  A as_a;
		case 2:  B as_b;
		default: error;
	}
}
"""


def test_every_layout_deciding_field_is_refused_as_a_secret() -> None:
	"""The population, rather than the four cases above one at a time.

	`traverse.invalidating_members` already answers "which fields decide
	where later members sit", because the differential needs to know which
	setters shift the bytes underneath a view. That is 14.6's question read
	from the other end -- a field a writer can move the layout with is a
	field an observer can read the layout for -- so every driver it names
	must be refused as a secret.

	Containment rather than equality, and the gap is one case: a variant
	whose arms are all one width moves nothing, so it is not a driver, and
	it is refused anyway because reading an arm branches on the
	discriminant. The secret rule is the wider of the two and this asserts
	the direction that has to hold.

	Deriving the list here rather than typing it is what keeps this honest:
	a fifth source of layout dependence added to the differential arrives in
	this test as a fifth driver, and fails it until the secret check learns
	about it too. A list typed out would have gone on passing -- which is
	how three of the first four came to be missing.
	"""
	drivers = traverse.invalidating_members(build(LAYOUT_DECIDING).structs)["S"]
	assert drivers == {"n", "m", "off", "kind"}, \
		"the schema no longer exercises every source; the sweep below is " \
		"only as wide as this set"

	for driver in sorted(drivers):
		marked = LAYOUT_DECIDING.replace(f"\tu8 {driver};",
		                                 f"\tu8 {driver} [secret];")
		assert marked != LAYOUT_DECIDING, f"no field `{driver}` to mark"
		rendered = failure(marked)
		assert f"the secret field `{driver}`" in rendered
		assert "layout depends on secret material" in rendered


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
