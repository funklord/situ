"""Whole-schema well-formedness checks.

These are the checks that need more than one declaration in view, so they run
after parsing rather than inside it. Everything here is decidable from names
and structure alone -- nothing evaluates an expression or computes a layout,
which is what keeps it in phase 1.

Situ has no field numbers: position carries identity and names are the identity
(project.md section 4). That makes duplicate names a correctness problem rather
than a style one, which is why they are errors here.
"""

from __future__ import annotations

from situc import ast
from situc.diagnostics import Diagnostic, Label, Severity, SituError, error
from situc.types import is_scalar_name

Structs = dict[str, ast.StructDecl]

# Members that hold other members, and whether their interior shares the
# enclosing struct's namespace. A `positional` block and an `authenticated` one
# are transparent: they assert something about members that still belong to the
# struct. A region introduced by a codec is not, because its interior is the
# transform's output rather than the struct's bytes.
TRANSPARENT_BLOCKS = (ast.PositionalBlock, ast.Authenticated)
OPAQUE_BLOCKS      = (ast.Coded, ast.Sealed, ast.Indexed)


def nested(member: ast.Member) -> tuple[ast.Member, ...]:
	"""The members a member contains, whatever kind of container it is.

	One place knows this, so adding a container means changing one function
	rather than every recursion in this file.
	"""
	if isinstance(member, TRANSPARENT_BLOCKS + OPAQUE_BLOCKS):
		return member.members
	if isinstance(member, ast.Variant):
		return tuple(member.members_of())
	return ()


def check(schema: ast.Schema) -> None:
	"""Run every whole-schema check, raising on the first failure."""
	check_unique_declarations(schema)
	check_const_names_do_not_shadow_attributes(schema)
	check_unique_enum_members(schema)
	check_unique_member_names(schema)
	check_unique_attributes(schema)
	check_types_resolve(schema)
	check_variant_exhaustiveness(schema)
	check_codec_bindings(schema)
	check_tag_coverage(schema)
	check_nonce_references(schema)
	check_no_recursive_types(schema)


Notes = list[str] | None


def _redeclaration(kind: str, name: str, first: ast.Node, second: ast.Node,
	notes: Notes = None) -> SituError:
	"""Point at both the duplicate and the original."""
	return SituError(Diagnostic(
		severity = Severity.ERROR,
		message  = f"{kind} `{name}` is declared more than once",
		primary  = Label(second.span, "redeclared here"),
		labels   = [Label(first.span, "first declared here")],
		notes    = notes or [],
	))


# ---------------------------------------------------------------------------
# Declaration names
# ---------------------------------------------------------------------------


def _named_declarations(schema: ast.Schema) -> list[tuple[str, str, ast.Decl]]:
	named: list[tuple[str, str, ast.Decl]] = []
	for decl in schema.decls:
		if isinstance(decl, ast.StructDecl):
			named.append(("struct", decl.name, decl))
		elif isinstance(decl, ast.EnumDecl):
			named.append(("enum", decl.name, decl))
		elif isinstance(decl, ast.ConstDecl):
			named.append(("const", decl.name, decl))
		elif isinstance(decl, ast.VarintDecl):
			named.append(("varint type", decl.name, decl))
		elif isinstance(decl, ast.EndianMarkerDecl):
			named.append(("endian marker", decl.name, decl))
		elif isinstance(decl, ast.CodecDecl):
			named.append(("codec", decl.name, decl))
	return named


def check_unique_declarations(schema: ast.Schema) -> None:
	"""Structs, enums and constants share one namespace.

	They have to: a field's type is an identifier and an array size is an
	identifier, so a struct and a const with the same name would make
	`Foo x[Foo];` unreadable.
	"""
	seen: dict[str, tuple[str, ast.Decl]] = {}

	for kind, name, decl in _named_declarations(schema):
		previous = seen.get(name)
		if previous is not None:
			first_kind, first_decl = previous
			note = [] if first_kind == kind else [
				f"the earlier declaration is a {first_kind}; types and constants "
				"share one namespace",
			]
			raise _redeclaration(kind, name, first_decl, decl, note)
		seen[name] = (kind, decl)


def check_const_names_do_not_shadow_attributes(schema: ast.Schema) -> None:
	"""A constant may not be named after an attribute.

	This closes the hole left open by docs/decisions/0006-bracket-disambiguation.md.
	`[` introduces either an array size or an attribute list, and a lone
	identifier is read as an attribute when it is in the attribute vocabulary.
	So `const max = 4;` would make `u8 buf[max];` parse as a scalar carrying the
	flag `max` rather than an array of four bytes -- silently, and with no way
	for the author to spell what they meant.

	Rejecting the constant kills the ambiguity at its source rather than at
	every use, which is what section 17.0 asks for.
	"""
	# Imported here rather than at module scope: the parser calls into this
	# module, so a top-level import would close the cycle.
	from situc.parser import ATTRIBUTE_NAMES

	for decl in schema.consts():
		if decl.name in ATTRIBUTE_NAMES:
			raise error(
				f"constant `{decl.name}` collides with an attribute name",
				decl.span,
				label = "cannot be used as a constant name",
				notes = [
					f"`[{decl.name}]` after a field would read as an attribute, "
					"not as an array size",
					"rename the constant (docs/decisions/0006-bracket-disambiguation.md)",
				],
			)


# ---------------------------------------------------------------------------
# Member names
# ---------------------------------------------------------------------------


def check_unique_enum_members(schema: ast.Schema) -> None:
	for decl in schema.enums():
		seen: dict[str, ast.EnumMember] = {}
		for member in decl.members:
			previous = seen.get(member.name)
			if previous is not None:
				raise _redeclaration("enum member", member.name, previous, member)
			seen[member.name] = member


def check_unique_member_names(schema: ast.Schema) -> None:
	"""Field names are unique within a struct.

	A `positional` block does not open a scope: it is a staticness assertion
	over members that still belong to the enclosing struct (section 9.2), so
	its members share the struct's namespace. `authenticated` is transparent for
	the same reason -- it asserts coverage over members that stay where they
	were, which is why 5.3 addresses `Packet.hdr.seq` rather than naming the
	block.
	"""
	for decl in schema.structs():
		seen: dict[str, ast.Field] = {}
		_collect_member_names(decl.members, seen)


def _collect_member_names(members: tuple[ast.Member, ...], seen: dict[str, ast.Field]) -> None:
	for member in members:
		# Only the transparent blocks are walked: a coded or sealed region's
		# interior is its own namespace, so a name may repeat across the seam.
		if isinstance(member, TRANSPARENT_BLOCKS + (ast.Variant,)):
			_collect_member_names(nested(member), seen)
		elif isinstance(member, ast.Field):
			previous = seen.get(member.name)
			if previous is not None:
				raise _redeclaration("field", member.name, previous, member, [
					"situ has no field numbers: the name is the identity "
					"(project.md section 4)",
				])
			seen[member.name] = member


def check_unique_attributes(schema: ast.Schema) -> None:
	"""One attribute may not appear twice in the same list.

	`[must_eq = 1, must_eq = 2]` has no defensible reading, and picking either
	silently is the kind of guess section 17.0 forbids.
	"""
	for decl in schema.decls:
		if isinstance(decl, ast.StructDecl):
			_check_attr_list(decl.attrs)
			_check_member_attrs(decl.members)


def _check_member_attrs(members: tuple[ast.Member, ...]) -> None:
	for member in members:
		if isinstance(member, (ast.Field, ast.Reserved, ast.TagField)):
			_check_attr_list(member.attrs)
		_check_member_attrs(nested(member))


def _check_attr_list(attrs: tuple[ast.Attr, ...]) -> None:
	seen: dict[str, ast.Attr] = {}
	for attr in attrs:
		previous = seen.get(attr.name)
		if previous is not None:
			raise _redeclaration("attribute", attr.name, previous, attr)
		seen[attr.name] = attr


# ---------------------------------------------------------------------------
# Type references
# ---------------------------------------------------------------------------


def check_types_resolve(schema: ast.Schema) -> None:
	"""Every named type resolves to a declaration in this file.

	Skipped entirely when the schema imports another file, because the missing
	name may legitimately live there and import resolution does not exist yet.
	The check is worth having in the common single-file case: a typo in a type
	name would otherwise survive the whole front end.
	"""
	if any(isinstance(decl, ast.ImportDirective) for decl in schema.decls):
		return

	declared  = {decl.name for decl in schema.structs()}
	declared |= {decl.name for decl in schema.enums()}
	declared |= {decl.name for decl in schema.varints()}
	declared |= {decl.name for decl in schema.markers()}

	for decl in schema.structs():
		_check_member_types(decl.members, declared)


def _check_member_types(members: tuple[ast.Member, ...], declared: set[str]) -> None:
	for member in members:
		_check_member_types(nested(member), declared)

		if not isinstance(member, (ast.Field, ast.Reserved, ast.TagField)):
			continue

		type_ref = member.type_ref
		if type_ref.is_scalar or type_ref.name in declared:
			continue

		notes = ["expected a scalar type or a struct or enum declared in this file"]
		near  = _nearest(type_ref.name, declared)
		if near is not None:
			notes.insert(0, f"a type named `{near}` is declared; did you mean that?")

		raise error(
			f"unknown type `{type_ref.name}`",
			type_ref.span,
			label = "not declared",
			notes = notes,
		)


def _nearest(name: str, candidates: set[str]) -> str | None:
	"""The closest declared name, when one is close enough to be a typo.

	Case-insensitive equality and one-character edits only: a wider search
	produces confident-sounding wrong suggestions, which are worse than none.
	"""
	lowered = name.lower()
	for candidate in sorted(candidates):
		if candidate.lower() == lowered or _within_one_edit(name, candidate):
			return candidate
	return None


def _within_one_edit(a: str, b: str) -> bool:
	if abs(len(a) - len(b)) > 1:
		return False

	if len(a) == len(b):
		return sum(x != y for x, y in zip(a, b)) == 1

	shorter, longer = (a, b) if len(a) < len(b) else (b, a)
	for index in range(len(longer)):
		if longer[:index] + longer[index + 1 :] == shorter:
			return True
	return False


# ---------------------------------------------------------------------------
# Codecs
# ---------------------------------------------------------------------------


def check_codec_bindings(schema: ast.Schema) -> None:
	"""Every `impl` names a declared codec, and no codec is bound twice.

	A signature with *no* binding is legal and analyses cleanly: section 13.1
	makes that the normal case for a protocol under design, and the missing
	implementation is an error at code generation rather than here.
	"""
	codecs = {decl.name: decl for decl in schema.codecs()}
	bound: dict[str, ast.ImplDecl] = {}

	for impl in schema.impls():
		if impl.codec not in codecs:
			raise error(
				f"`impl` names unknown codec `{impl.codec}`",
				impl.span,
				label = "no such codec",
				notes = ["declare the signature first; an implementation binds to "
				         "a contract, not the other way round"],
			)

		previous = bound.get(impl.codec)
		if previous is not None:
			raise _redeclaration("implementation of", impl.codec, previous, impl, [
				"a codec has one implementation; swapping it means replacing the "
				"binding, not adding another",
			])
		bound[impl.codec] = impl

	for struct in schema.structs():
		for region in _coded_regions(struct.members):
			if region.codec not in codecs:
				raise error(
					f"unknown codec `{region.codec}`",
					region.span,
					label = "not declared",
					notes = ["declare it with `codec " + region.codec + " { ... }`",
					         "a codec's properties are what the lattice reads; "
					         "without them nothing can be said about the region"],
				)


def _coded_regions(members: tuple[ast.Member, ...]) -> list[ast.Coded | ast.Sealed]:
	"""Every region carrying a codec. `sealed` is one of them (decision 0009)."""
	found: list[ast.Coded | ast.Sealed] = []
	for member in members:
		if isinstance(member, (ast.Coded, ast.Sealed)):
			found.append(member)
		found.extend(_coded_regions(nested(member)))
	return found


# ---------------------------------------------------------------------------
# Tag coverage
# ---------------------------------------------------------------------------


def auth_regions(members: tuple[ast.Member, ...]
		) -> list[ast.Authenticated | ast.Sealed]:
	"""Every authenticated and sealed region, in declaration order.

	Declaration order is load-bearing: it is what an omitted `covers` clause
	means (section 14.1), so it is the order the tag's coverage is built in and
	the order the generated recomputation runs in.
	"""
	found: list[ast.Authenticated | ast.Sealed] = []
	for member in members:
		if isinstance(member, (ast.Authenticated, ast.Sealed)):
			found.append(member)
		found.extend(auth_regions(nested(member)))
	return found


def tag_fields(members: tuple[ast.Member, ...]) -> list[ast.TagField]:
	found: list[ast.TagField] = []
	for member in members:
		if isinstance(member, ast.TagField):
			found.append(member)
		found.extend(tag_fields(nested(member)))
	return found


def coverage_of(tag: ast.TagField, regions: list[ast.Authenticated | ast.Sealed]
		) -> tuple[str, ...]:
	"""Which regions a tag covers, with inference applied (section 14.1)."""
	if tag.covers:
		return tag.covers
	return tuple(region.name for region in regions)


def check_tag_coverage(schema: ast.Schema) -> None:
	"""Every rule of section 14.1 that needs only names and structure.

	Coverage is the thing the whole chapter turns on: which bytes go stale when
	a field is written. Getting it wrong silently would make the dirty bit a
	decoration, so each way of getting it wrong is an error here rather than a
	surprise at run time.
	"""
	for struct in schema.structs():
		regions = auth_regions(struct.members)
		tags    = tag_fields(struct.members)

		_check_region_names(regions)
		_check_regions_are_covered(struct, regions, tags)

		by_name = {region.name: region for region in regions}
		spread: list[tuple[ast.TagField, set[str]]] = []

		for tag in tags:
			covers = coverage_of(tag, regions)
			_check_covers_resolve(tag, covers, by_name)
			_check_tag_is_outside_its_coverage(tag, covers, by_name)
			spread.append((tag, set(covers)))

		_check_coverage_is_disjoint_or_nested(spread)


def _check_region_names(regions: list[ast.Authenticated | ast.Sealed]) -> None:
	seen: dict[str, ast.Member] = {}
	for region in regions:
		previous = seen.get(region.name)
		if previous is not None:
			raise _redeclaration("region", region.name, previous, region, [
				"a `covers` clause names regions, so two of them cannot share "
				"a name",
				"name them: `sealed inner(codec) { ... }`",
			])
		seen[region.name] = region


def _check_regions_are_covered(struct: ast.StructDecl,
		regions: list[ast.Authenticated | ast.Sealed],
		tags: list[ast.TagField]) -> None:
	"""A region no tag covers is a region that authenticates nothing.

	`authenticated { }` states that these bytes are covered by a tag. With no
	tag in the struct the statement is false, and a construct whose meaning is
	silently nothing is exactly what section 14.5 refuses.
	"""
	if not regions or tags:
		return

	region = regions[0]
	kind   = "sealed" if isinstance(region, ast.Sealed) else "authenticated"

	raise error(
		f"`{kind} {region.name}` is covered by no tag",
		region.span,
		label = f"struct `{struct.name}` declares no `tag` or `checksum`",
		notes = [
			"coverage is what makes the region mean anything: without a tag "
			"there is nothing to go stale and nothing to verify",
			"add `tag u8[16];` to the struct, whose coverage is then inferred "
			"as every authenticated and sealed region in it "
			"(project.md section 14.1)",
		],
	)


def _check_covers_resolve(tag: ast.TagField, covers: tuple[str, ...],
		by_name: dict[str, ast.Authenticated | ast.Sealed]) -> None:
	for name in covers:
		if name in by_name:
			continue

		known = ", ".join(sorted(by_name)) or "none in this struct"
		raise error(
			f"`{tag.name}` covers unknown region `{name}`",
			tag.span,
			label = "no such authenticated or sealed region",
			notes = [f"regions in this struct: {known}",
			         "a `covers` clause names regions, not fields: coverage is "
			         "over a contiguous span of bytes, and a region is what "
			         "gives one a name"],
		)


def _check_tag_is_outside_its_coverage(tag: ast.TagField, covers: tuple[str, ...],
		by_name: dict[str, ast.Authenticated | ast.Sealed]) -> None:
	"""A tag may not sit inside the bytes it authenticates.

	Computing it would need its own value as input. Nothing about this is
	recoverable at run time, so it is an error here.
	"""
	for name in covers:
		region = by_name.get(name)
		if region is not None and any(held is tag for held in tag_fields(region.members)):
			raise error(
				f"`{tag.name}` is inside the region it covers",
				tag.span,
				label = f"declared inside `{name}`",
				notes = ["computing it would take its own bytes as input",
				         f"move it out of `{name}`, or narrow its `covers` "
				         "clause to regions that do not contain it"],
			)


def _check_coverage_is_disjoint_or_nested(
		spread: list[tuple[ast.TagField, set[str]]]) -> None:
	"""Section 14.1: disjoint or nested coverage only.

	Two tags whose coverage overlaps without one containing the other have no
	defensible recomputation order -- each covers bytes the other has yet to
	write. Nested coverage does have one, and
	docs/decisions/0011-nested-tag-coverage.md fixes it as innermost first.
	"""
	for index, (tag, covers) in enumerate(spread):
		for other_tag, other in spread[:index]:
			shared = covers & other
			if not shared or covers <= other or other <= covers:
				continue

			listed = ", ".join(sorted(shared))
			raise SituError(Diagnostic(
				severity = Severity.ERROR,
				message  = f"`{tag.name}` and `{other_tag.name}` overlap without "
				           "nesting",
				primary  = Label(tag.span, f"also covers {listed}"),
				labels   = [Label(other_tag.span, "covered here too")],
				notes    = [
					"each tag covers bytes the other does not, so neither can be "
					"computed first: whichever runs second invalidates the one "
					"before it",
					"make the coverage disjoint, or make one a subset of the "
					"other, which recomputes innermost first "
					"(project.md section 14.1)",
				],
			))


def check_nonce_references(schema: ast.Schema) -> None:
	"""`sealed(codec, nonce = ref)` must name a field the reader has already.

	A nonce read from inside the region it seeds is unusable: the decoder needs
	it before it can decode anything. This is the same rule as the discriminant
	of a variant, for the same reason.
	"""
	for struct in schema.structs():
		for region in auth_regions(struct.members):
			if not isinstance(region, ast.Sealed):
				continue

			reference = _argument(region.args, "nonce")
			if reference is None:
				continue

			name = _reference_name(reference)
			if name is None:
				raise error(
					"a nonce must name a field",
					reference.span,
					label = "expected a field name",
					notes = ["`nonce = counter` or `nonce = hdr.counter`"],
				)

			available = [field.name for field in _fields(_before(struct.members, region))]
			if name in available:
				continue

			raise error(
				f"unknown nonce field `{name}`",
				reference.span,
				label = "not a field declared before this region",
				notes = [
					f"fields available here: {', '.join(available) or 'none'}",
					"the decoder needs the nonce before it can decode, so it has "
					"to be parsed strictly earlier -- a nonce inside the sealed "
					"region cannot be read without the key it helps derive",
				],
			)


def _argument(args: tuple[ast.Attr, ...], name: str) -> ast.Expr | None:
	for arg in args:
		if arg.name == name:
			return arg.value
	return None


def _reference_name(expr: ast.Expr) -> str | None:
	"""The head of a field reference: `counter` or `hdr.counter`."""
	if isinstance(expr, ast.NameRef):
		return expr.name
	if isinstance(expr, ast.Access):
		return _reference_name(expr.base)
	return None


def _before(members: tuple[ast.Member, ...], target: ast.Member) -> tuple[ast.Member, ...]:
	"""Members declared before `target`, flattening the transparent blocks.

	Flattened first and sliced second, because the target may be nested: a
	sealed region inside an `authenticated` block is still preceded by whatever
	came before that block.
	"""
	flat = _flatten(members)
	for index, member in enumerate(flat):
		if member is target:
			return tuple(flat[:index])
	return tuple(flat)


def _flatten(members: tuple[ast.Member, ...]) -> list[ast.Member]:
	found: list[ast.Member] = []
	for member in members:
		if isinstance(member, TRANSPARENT_BLOCKS):
			found.append(member)
			found.extend(_flatten(nested(member)))
		else:
			found.append(member)
	return found


# ---------------------------------------------------------------------------
# Variants
# ---------------------------------------------------------------------------


def check_variant_exhaustiveness(schema: ast.Schema) -> None:
	"""A variant over an enum must cover it, or say what to do otherwise.

	Section 9.6: a missing case without a `default` arm is an error. This is the
	same position as section 14.5's on unknown enum values -- an unhandled
	discriminant is a malleability surface, and silently accepting one is what
	situ refuses to do.
	"""
	enums = {decl.name: decl for decl in schema.enums()}

	for struct in schema.structs():
		for variant in _variants(struct.members):
			_check_one_variant(variant, struct, enums)


def _variants(members: tuple[ast.Member, ...]) -> list[ast.Variant]:
	found: list[ast.Variant] = []
	for member in members:
		if isinstance(member, ast.Variant):
			found.append(member)
		found.extend(_variants(nested(member)))
	return found


def _check_one_variant(variant: ast.Variant, struct: ast.StructDecl,
		enums: dict[str, ast.EnumDecl]) -> None:
	enum = _discriminant_enum(variant, struct, enums)
	if enum is None:
		# Not switching on an enum, so there is no set of values to be
		# exhaustive over. A default arm is then the only way to be total.
		if variant.default_arm is None:
			raise error(
				f"variant `{variant.name}` has no `default` arm",
				variant.span,
				label = "not exhaustive",
				notes = ["the discriminant is not an enum, so the compiler cannot "
				         "tell which values are covered",
				         "add `default: error;` to reject the rest, which is the "
				         "safe choice (project.md section 14.5)"],
			)
		return

	covered = {name for name in (_case_member(arm, enum) for arm in variant.arms)
	           if name is not None}
	missing = [member.name for member in enum.members if member.name not in covered]

	if missing and variant.default_arm is None:
		listed = ", ".join(f"`{name}`" for name in missing)
		raise error(
			f"variant `{variant.name}` does not cover every value of `{enum.name}`",
			variant.span,
			label = f"missing: {listed}",
			notes = [
				f"add a `case {enum.name}.{missing[0]}:` arm, or a `default:` arm "
				"for the rest",
				"`default: error;` rejects an unknown discriminant, which is the "
				"safe choice (project.md section 14.5)",
			],
		)


def _discriminant_enum(variant: ast.Variant, struct: ast.StructDecl,
		enums: dict[str, ast.EnumDecl]) -> ast.EnumDecl | None:
	"""The enum the discriminant field is typed as, if it is one."""
	from situc.expr import path_text

	path = path_text(variant.discriminant)
	if path is None:
		return None

	head = path.partition(".")[0]
	for member in _fields(struct.members):
		if member.name == head and "." not in path:
			return enums.get(member.type_ref.name)

	return None


def _case_member(arm: ast.VariantArm, enum: ast.EnumDecl) -> str | None:
	"""The enum member a `case` names, if it names one of this enum's."""
	if arm.value is None:
		return None
	if isinstance(arm.value, ast.Access) and isinstance(arm.value.base, ast.NameRef):
		if arm.value.base.name == enum.name:
			return arm.value.name
	return None


def _fields(members: tuple[ast.Member, ...]) -> list[ast.Field]:
	"""Fields in the enclosing struct's own namespace.

	Deliberately does not cross into a coded or sealed region: a discriminant or
	a length may not name a value the transform produces (section 13.3), so a
	field in there is not a candidate for anything asked of this list.
	"""
	found: list[ast.Field] = []
	for member in members:
		if isinstance(member, ast.Field):
			found.append(member)
		elif isinstance(member, TRANSPARENT_BLOCKS + (ast.Variant,)):
			found.extend(_fields(nested(member)))
	return found


# ---------------------------------------------------------------------------
# Recursion
# ---------------------------------------------------------------------------


def check_no_recursive_types(schema: ast.Schema) -> None:
	"""Reject recursive struct declarations (project.md section 2).

	Recursive types make size and capability computation non-terminating, so
	they are rejected at parse time rather than diagnosed later by a solver that
	failed to converge.
	"""
	structs = {decl.name: decl for decl in schema.structs()}

	for name in structs:
		cycle = _find_cycle(name, structs, [])
		if cycle is not None:
			raise _recursion_error(cycle, structs)


def _find_cycle(name: str, structs: Structs, path: list[str]) -> list[str] | None:
	if name in path:
		return path[path.index(name) :] + [name]

	decl = structs.get(name)
	if decl is None:
		return None

	for referenced in _referenced_structs(decl.members, structs):
		found = _find_cycle(referenced, structs, path + [name])
		if found is not None:
			return found

	return None


def _referenced_structs(members: tuple[ast.Member, ...], structs: Structs) -> list[str]:
	names: list[str] = []
	for member in members:
		if isinstance(member, ast.Field) and member.type_ref.name in structs:
			names.append(member.type_ref.name)
		names.extend(_referenced_structs(nested(member), structs))
	return names


def _recursion_error(cycle: list[str], structs: Structs) -> SituError:
	decl  = structs[cycle[0]]
	chain = " -> ".join(cycle)

	if len(cycle) == 2:
		summary = f"struct `{cycle[0]}` contains itself"
	else:
		summary = f"struct `{cycle[0]}` is recursive through {chain}"

	return error(
		summary,
		decl.span,
		label = "declared here",
		notes = [
			f"cycle: {chain}",
			"recursive types make size and capability computation "
			"non-terminating, so they are rejected (project.md section 2)",
		],
	)
