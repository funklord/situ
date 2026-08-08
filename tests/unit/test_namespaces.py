"""Namespaces, and the C identifier collisions they exist to prevent.

Two features that answer the same question from opposite ends. A namespace lets
two types share a name on purpose; the collision check refuses two that share a
generated identifier by accident. Neither has an opinion about how a schema
spells its names -- that is the author's business (section 25) -- and the tests
below are written in a mix of conventions to keep it that way.
"""

from __future__ import annotations

import pytest

from situc import ast
from situc.codegen.c import generate
from situc.diagnostics import SituError
from situc.dump import dump
from situc.layout import solve
from situc.invariant import paths_in
from situc.namespaces import namespace_of, qualify, unqualify
from situc.parser import parse_text
from situc.resolve import resolve
from situc.unparse import unparse

PREAMBLE = "endian big;\n"

TWO_HEADERS = """namespace outer {
	enum kind : u8 { hello = 1, data = 2, }

	struct header {
		u8   version [must_eq = 1];
		kind type;
		u16  length;
	}
}

namespace inner {
	struct header {
		u32 seq;
	}
}

struct packet {
	outer::header out;
	inner::header in;
}
"""


def build(body: str, prefix: str = "situ") -> str:
	schema   = parse_text(PREAMBLE + body)
	resolved = resolve(schema, solve(schema))
	return generate(schema, resolved, "unit", prefix).header


def paths(body: str) -> set[str]:
	schema   = parse_text(PREAMBLE + body)
	resolved = resolve(schema, solve(schema))
	return {entry.placement.path
	        for struct in resolved.structs.values()
	        for entry in struct.entries}


def refusal(body: str) -> str:
	with pytest.raises(SituError) as caught:
		build(body)
	return caught.value.diagnostic.render()


# -- namespaces -------------------------------------------------------------


def test_two_types_may_share_a_name() -> None:
	"""The whole reason the feature exists."""
	held = paths(TWO_HEADERS)
	assert "outer::header.length" in held
	assert "inner::header.seq" in held


def test_an_unqualified_name_resolves_in_its_own_namespace() -> None:
	"""`kind type;` inside `outer` means `outer::kind`, with no fallback.

	No fallback is the point: a schema that silently reached a `kind` from
	somewhere else would produce a layout that looks right and is not.
	"""
	held = paths(TWO_HEADERS)
	assert "outer::header.type" in held

	rendered = refusal("""namespace a { struct s { u8 x; } }
	struct t { kind k; }
	""")
	assert "unknown type `kind`" in rendered


def test_a_qualified_name_reaches_across_namespaces() -> None:
	assert "packet.out.version" in paths(TWO_HEADERS)


def test_namespaces_flatten_into_the_generated_names() -> None:
	header = build(TWO_HEADERS)

	assert "situ_outer_header_length_get" in header
	assert "situ_inner_header_seq_get" in header
	assert "typedef enum situ_outer_kind {" in header
	assert "#define SITU_OUTER_HEADER_SIZE_FIXED" in header

	# No separator survives into code. Comments keep it on purpose: they quote
	# the schema, and the schema is where the name has two parts.
	for line in header.splitlines():
		stripped = line.strip()
		if stripped.startswith(("/*", "*", "//")):
			continue
		assert "::" not in line, line


def test_the_prefix_stacks_outside_the_namespace() -> None:
	"""It is called a prefix, so it prefixes rather than replaces.

	A downstream integrator vendoring two copies of a schema wants to tell them
	apart while keeping the author's structure intact.
	"""
	header = build(TWO_HEADERS, prefix="vendor")

	assert "vendor_outer_header_length_get" in header
	assert "vendor_inner_header_seq_get" in header


def test_an_empty_prefix_leaves_the_file_owning_the_whole_name() -> None:
	header = build(TWO_HEADERS, prefix="")
	assert "outer_header_length_get" in header


def test_namespaced_schemas_round_trip() -> None:
	"""The blocks are reconstructed from the qualified names."""
	first = parse_text(PREAMBLE + TWO_HEADERS)
	again = parse_text(unparse(first))
	assert dump(again) == dump(first)

	rendered = unparse(first)
	assert "namespace outer {" in rendered
	assert "\tstruct header {" in rendered
	# Inside the block the qualification comes back off.
	assert "\t\tkind type;" in rendered


def test_nesting_is_refused_and_names_its_phase() -> None:
	rendered = refusal("""namespace a {
		namespace b { struct s { u8 x; } }
	}
	""")
	assert "a nested `namespace` is not yet implemented" in rendered
	assert "one level is supported" in rendered


def test_a_nested_qualified_name_is_refused_the_same_way() -> None:
	"""Both halves of the restriction say the same thing."""
	rendered = refusal("namespace a { struct s { u8 x; } }\n"
	                   "struct t { a::b::c x; }\n")
	assert "a nested qualified name is not yet implemented" in rendered


def test_an_empty_namespace_is_refused() -> None:
	rendered = refusal("namespace a { require size(b) == 1; }\nstruct b { u8 x; }")
	assert "declares no types" in rendered
	assert "keep two types of the same name apart" in rendered


def test_requirements_inside_a_namespace_see_its_names() -> None:
	schema   = parse_text(PREAMBLE + "namespace a { struct s { u16 x; }\n"
	                                 "require size(s) == 2; }")
	resolved = resolve(schema, solve(schema))
	from situc import requirements

	assert requirements.discharge(schema, resolved)[0].satisfied


def test_qualify_and_namespace_of_are_inverses() -> None:
	assert namespace_of(qualify("a", "b")) == "a"
	assert namespace_of("plain") == ""


# -- collisions -------------------------------------------------------------


def test_two_paths_flattening_to_one_identifier_are_refused() -> None:
	"""What used to surface as a redefinition error in generated code.

	The C compiler would name a function nobody wrote and point at no schema at
	all, which is the diagnostic quality section 17 exists to rule out.
	"""
	rendered = refusal("struct A_b { u8 c; }\nstruct A { u8 b_c; }\n")

	assert "generate the same C identifier" in rendered
	assert "`A.b_c`" in rendered and "`A_b.c`" in rendered
	assert "situ_A_b_c" in rendered
	assert "rename either one, or put them in separate namespaces" in rendered


def test_a_type_can_collide_with_a_region() -> None:
	"""The gate types of section 14.3 widened the type namespace.

	`enum a_b` and the sealed region `b` of struct `a` both reach `situ_a_b`,
	and neither is obviously wrong to write.
	"""
	rendered = refusal("""codec aead {
		length_preserving; seekable = linear; granularity = byte;
		authenticated; invertible; deterministic;
	}
	enum a_b : u8 { one = 1, }
	struct a {
		sealed b(aead) { u8 inner; }
		tag u8[16];
	}
	""")
	assert "enum `a_b`" in rendered
	assert "generate the same C identifier" in rendered


def test_a_namespace_resolves_a_collision() -> None:
	"""Which is what the remedy in the diagnostic suggests."""
	header = build("""namespace one { struct A_b { u8 c; } }
	namespace two { struct A { u8 b_c; } }
	""")
	assert "situ_one_A_b_c_get" in header
	assert "situ_two_A_b_c_get" in header


def test_names_differing_only_in_case_warn_rather_than_fail() -> None:
	"""Legal C, and a real hazard in the macro namespace.

	Which convention a schema uses is the author's business, so this is said
	out loud and not enforced.
	"""
	schema   = parse_text(PREAMBLE + "struct Reading { u8 a; }\n"
	                                 "struct reading { u8 b; }\n")
	resolved = resolve(schema, solve(schema))
	generated = generate(schema, resolved, "unit")

	assert len(generated.warnings) == 1
	rendered = generated.warnings[0].render()
	assert "differ only in case" in rendered
	assert "macro names are uppercased" in rendered
	assert "SITU_READING" in rendered


def test_a_schema_mixing_conventions_is_accepted() -> None:
	"""snake_case and PascalCase in one file, and nothing to say about it."""
	schema   = parse_text(PREAMBLE + "struct wire_header { u8 a; }\n"
	                                 "struct PayloadRecord { u8 b; }\n")
	resolved = resolve(schema, solve(schema))
	generated = generate(schema, resolved, "unit")

	assert generated.warnings == []
	assert "situ_wire_header_a_get" in generated.header
	assert "situ_PayloadRecord_b_get" in generated.header


# -- an invariant inside a namespace -----------------------------------------


NAMESPACED_INVARIANT = """namespace wire {
	struct s {
		u16 total;
		u8  body[4];
	}

	invariant s.total == size(s.body);
}
"""


def test_an_invariant_inside_a_namespace_is_qualified() -> None:
	"""It was not, and the construct could not be written at all.

	`rewrite` had no case for an invariant, so it fell through to the
	directive case and came out untouched: flattening renamed the struct to
	`wire::s` and left the invariant naming `s`, which `check_invariants`
	then refused with "unknown struct". A hard error on a valid schema rather
	than a wrong answer, which is the better half of the bug.
	"""
	schema = parse_text(PREAMBLE + NAMESPACED_INVARIANT)
	invariant = schema.invariants()[0]

	assert schema.structs()[0].name == "wire::s"
	assert invariant.derived == "wire::s.total"
	assert paths_in(invariant.expr) == ["wire::s.body"]


def test_only_the_head_of_the_path_is_qualified() -> None:
	"""`s` is the name being scoped; `total` is a field of what it resolves
	to, and a namespace has nothing to say about it."""
	invariant = parse_text(PREAMBLE + NAMESPACED_INVARIANT).invariants()[0]

	assert invariant.derived.endswith(".total")
	assert "wire::total" not in invariant.derived


def test_a_namespaced_invariant_reaches_the_backend() -> None:
	"""Qualifying is not the point; being maintained is.

	`invariant.derived` matches the struct by name, so an unqualified
	invariant beside a qualified struct would have selected nothing even if
	the front end had let it through -- the field would have kept its setter
	and nothing would have recomputed.
	"""
	schema    = parse_text(PREAMBLE + NAMESPACED_INVARIANT)
	resolved  = resolve(schema, solve(schema))
	generated = generate(schema, resolved, "unit")

	assert "situ_wire_s_total_recompute" in generated.header
	assert "wire::s.total == size(wire::s.body)" in generated.header


def test_unqualifying_an_invariant_is_the_same_walk_backwards() -> None:
	"""The unparser reverses `rewrite` with a different `name_of`, so a case
	added for one direction arrives in the other by construction."""
	invariant = parse_text(PREAMBLE + NAMESPACED_INVARIANT).invariants()[0]

	back = unqualify(invariant, "wire")

	assert isinstance(back, ast.Invariant)
	assert back.derived == "s.total"
	assert paths_in(back.expr) == ["s.body"]
