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


def check(schema: ast.Schema) -> None:
	"""Run every whole-schema check, raising on the first failure."""
	check_unique_declarations(schema)
	check_const_names_do_not_shadow_attributes(schema)
	check_unique_enum_members(schema)
	check_unique_member_names(schema)
	check_unique_attributes(schema)
	check_types_resolve(schema)
	check_variant_exhaustiveness(schema)
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
	its members share the struct's namespace.
	"""
	for decl in schema.structs():
		seen: dict[str, ast.Field] = {}
		_collect_member_names(decl.members, seen)


def _collect_member_names(members: tuple[ast.Member, ...], seen: dict[str, ast.Field]) -> None:
	for member in members:
		if isinstance(member, ast.PositionalBlock):
			_collect_member_names(member.members, seen)
		elif isinstance(member, ast.Variant):
			for arm in member.members_of():
				_collect_member_names((arm,), seen)
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
		if isinstance(member, ast.PositionalBlock):
			_check_member_attrs(member.members)
		elif isinstance(member, (ast.Field, ast.Reserved)):
			_check_attr_list(member.attrs)


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
		if isinstance(member, ast.PositionalBlock):
			_check_member_types(member.members, declared)
			continue
		if not isinstance(member, (ast.Field, ast.Reserved)):
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
		elif isinstance(member, ast.PositionalBlock):
			found.extend(_variants(member.members))
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
	found: list[ast.Field] = []
	for member in members:
		if isinstance(member, ast.Field):
			found.append(member)
		elif isinstance(member, ast.PositionalBlock):
			found.extend(_fields(member.members))
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
		if isinstance(member, ast.PositionalBlock):
			names.extend(_referenced_structs(member.members, structs))
		elif isinstance(member, ast.Field) and member.type_ref.name in structs:
			names.append(member.type_ref.name)
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
