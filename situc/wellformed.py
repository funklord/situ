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

from collections.abc import Sequence

from situc import ast
from situc.diagnostics import Diagnostic, Label, Severity, SituError, error
from situc.invariant import BUILTINS, paths_in
from situc.types import ScalarKind, is_scalar_name

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
	check_registers(schema)
	check_no_recursive_types(schema)
	check_delimiters(schema)
	check_versions(schema)
	check_invariants(schema)


#: Attributes that only mean anything on a delimited member.
DELIMITER_ATTRS = ("quoted", "escape")


def check_delimiters(schema: ast.Schema) -> None:
	"""`until` says where a member stops, so nothing else may also say it.

	Every refusal here is a member that would have two answers to one question,
	or an attribute with nothing to attach to. Both are ambiguity, which
	section 17.0 makes an error rather than a preference.
	"""
	for struct in schema.structs():
		for member in _walk_members(struct.members):
			if not isinstance(member, (ast.Field, ast.Reserved)):
				continue
			_check_one_delimiter(member)


def _check_one_delimiter(member: ast.Field | ast.Reserved) -> None:
	name = getattr(member, "name", "a reserved member")

	if member.until is None:
		for attr in member.attrs:
			if attr.name in DELIMITER_ATTRS:
				raise error(
					f"`{attr.name}` needs a delimiter to make inert",
					attr.span,
					label = "no `until` on this member",
					notes = [f"`{attr.name}` says how a delimiter may appear "
					         "inside the content, and this member has no "
					         "delimiter",
					         'add `until "D"`, or drop the attribute'],
				)
		return

	if member.array is not None and member.array.size is not None:
		raise error(
			f"`{name}` says twice where it stops",
			member.until.span,
			label = "a delimiter here",
			notes = ["the array already gives a length, and a member that ran "
			         "to whichever came first would be two formats depending on "
			         "the data",
			         "keep the length for a fixed-extent field, or the "
			         "delimiter for a framed one"],
		)

	# A text number is a single value *and* delimited: the delimiter says
	# where the digits stop, which is the one thing that can, since a number
	# written as digits is as wide as the number (section 8.6.2).
	if member.array is None and getattr(member, "radix", None) is None:
		raise error(
			f"`{name}` is a single value, so a delimiter has nothing to bound",
			member.until.span,
			label = "not an array",
			notes = ["a delimiter says how far a run of elements goes",
			         f"`{member.type_ref.name} {name}[] until ...` frames a "
			         "run of them",
			         f"`decimal {member.type_ref.name} {name} until ...` reads "
			         "the run as a number written in digits"],
		)

	radix = getattr(member, "radix", None)
	if radix is not None:
		scalar = member.type_ref.scalar
		if scalar is None or scalar.kind is not ScalarKind.UINT:
			raise error(
				f"`{name}` is a text number, so its type must be an unsigned "
				f"integer",
				member.type_ref.span,
				label = f"`{member.type_ref.name}` is not one",
				notes = ["the type gives the range of values the digits may "
				         "spell, and situ reads digits as a magnitude",
				         "a signed or fractional text format needs a sign or a "
				         "point, which is a grammar rather than a number"],
			)

	if any(attr.name == "nul_terminated" for attr in member.attrs):
		raise error(
			f"`{name}` is both delimited and nul-terminated",
			member.until.span,
			label = "a delimiter here",
			notes = ["`nul_terminated` reads the declared size as a capacity "
			         "and the content to the first zero; `until` has no "
			         "declared size to be the capacity of",
			         'a nul-framed member is `until "\\0"`'],
		)


def check_versions(schema: ast.Schema) -> None:
	"""`[since = N]` says a member arrived in version N (section 19.4).

	One rule carries the whole construct: **the versions across a struct's
	members must never decrease.** That is append-only, said structurally.

	Situ has no field numbers -- position carries identity (section 4) -- so a
	member inserted before an existing one moves every byte after it, and
	every deployed peer misreads the message. Elsewhere that is caught by
	`situc wire` after the fact; here it is refused, because a schema that
	says "this arrived in v2" is making a compatibility claim and the claim
	has to be true.
	"""
	for struct in schema.structs():
		version = _version_field(struct)
		members = [member for member in _walk_members(struct.members)
		           if isinstance(member, (ast.Field, ast.Reserved))]
		tagged  = [(member, _since_of(member)) for member in members]

		if not any(since is not None for _, since in tagged):
			# A struct that names a version field and has no `[since]` member
			# yet is the ordinary first revision of an extensible format --
			# the state every versioned schema is in before its first
			# extension. Refusing it would force the attribute to be added in
			# the same commit as the first new member, which is the commit
			# where its absence matters least and its presence is noisiest.
			if version is not None:
				_check_version_field(struct, version)
			continue

		if version is None:
			member = next(m for m, since in tagged if since is not None)
			raise error(
				f"`struct {struct.name}` has a versioned member and no version "
				f"field",
				member.span,
				label = "`[since]` here",
				notes = ["a reader has to know which version a message is "
				         "before it knows whether these bytes are present",
				         f"add `[version = f]` to `struct {struct.name}`, "
				         "naming the member that carries it"],
			)

		_check_version_field(struct, version)
		_check_append_only(struct, tagged)


def _check_version_field(struct: ast.StructDecl, version: str) -> None:
	held = _find_member(struct, version)
	if held is None:
		raise error(
			f"`{struct.name}` has no member `{version}`",
			struct.span,
			label = "no such version field",
			notes = ["`[version = f]` names a member of this struct"],
		)

	# `_find_member` walks every kind of member, and a version has to be one
	# that holds a number: a `variant` or a region has no value to read.
	if not isinstance(held, ast.Field) or held.array is not None \
			or not held.type_ref.is_scalar:
		raise error(
			f"`{version}` is not a single scalar, so it cannot say which "
			f"version this is",
			held.span,
			label = "the version field",
			notes = ["a version is one number, read before anything that "
			         "depends on it"],
		)

	if _since_of(held) is not None:
		raise error(
			f"`{version}` decides which members are present, so it cannot be "
			f"one of them",
			held.span,
			label = "`[since]` on the version field",
			notes = ["a reader would have to know the version to find the "
			         "version"],
		)


def _check_append_only(struct: ast.StructDecl,
		tagged: Sequence[tuple[ast.Field | ast.Reserved, int | None]]) -> None:
	"""Versions never decrease, so nothing is ever inserted before anything."""
	highest = 0
	for member, since in tagged:
		version = 1 if since is None else since

		if version < highest:
			name = getattr(member, "name", "a reserved member")
			raise error(
				f"`{name}` arrives in version {version}, after a member that "
				f"arrives in {highest}",
				member.span,
				label = f"`since = {version}` here",
				notes = [
					"situ has no field numbers: position carries identity "
					"(section 4), so a member added before an existing one "
					"moves every byte after it",
					"every version a member is added in must be at least the "
					"one before it -- which is append-only, and is the whole "
					"of what makes `[since]` a compatibility claim",
				],
			)
		highest = version


def _version_field(struct: ast.StructDecl) -> str | None:
	for attr in struct.attrs:
		if attr.name == "version" and isinstance(attr.value, ast.NameRef):
			return attr.value.name
	return None


def _since_of(member: ast.Member) -> int | None:
	for attr in getattr(member, "attrs", ()):
		if attr.name != "since":
			continue
		if not isinstance(attr.value, ast.IntLiteral) or attr.value.value < 1:
			raise error(
				"`since` takes a version number, counting from 1",
				attr.span,
				label = "expected a literal",
				notes = ["`[since = 2]`: the version this member arrived in"],
			)
		return attr.value.value
	return None


def check_invariants(schema: ast.Schema) -> None:
	"""An invariant names one field to maintain and what it derives from.

	Both halves have to be real and both have to be in the same struct: an
	invariant is a statement about one frame's bytes, and one that reached
	across structs would have no view to evaluate itself against.
	"""
	structs = {decl.name: decl for decl in schema.structs()}

	for invariant in schema.invariants():
		struct_name, _, field = invariant.derived.partition(".")

		if not field:
			raise error(
				f"`{invariant.derived}` is not a field path",
				invariant.span,
				label = "expected `struct.field`",
				notes = ["an invariant maintains one field of one struct, so it "
				         "names both: `invariant s.total == size(s.body);`"],
			)

		struct = structs.get(struct_name)
		if struct is None:
			raise error(
				f"unknown struct `{struct_name}`",
				invariant.span,
				label = "no such struct",
				notes = ["an invariant is evaluated against a view of one "
				         "struct, so the field it maintains has to be in one"],
			)

		if _find_member(struct, field) is None:
			raise error(
				f"`{struct_name}` has no field `{field}`",
				invariant.span,
				label = "no such field",
				notes = [f"the invariant would maintain a field that does not "
				         f"exist, so nothing would keep it true"],
			)

		for path in paths_in(invariant.expr):
			other, _, name = path.partition(".")
			if other != struct_name:
				raise error(
					f"`{path}` is not a field of `{struct_name}`",
					invariant.span,
					label = "outside the struct this invariant maintains",
					notes = [
						"an invariant is evaluated against one view, and a "
						"field of another struct is not reachable from it",
						f"every path in the expression must start `{struct_name}.`",
					],
				)
			if name and _find_member(struct, name) is None:
				raise error(
					f"`{struct_name}` has no field `{name}`",
					invariant.span,
					label = "no such field",
					notes = ["the invariant would depend on a field that does "
					         "not exist"],
				)

		if invariant.derived in paths_in(invariant.expr):
			raise error(
				f"`{invariant.derived}` derives from itself",
				invariant.span,
				label = "circular",
				notes = ["recomputing it would read the value it is about to "
				         "write, so it would hold whatever it already held"],
			)

		_check_invariant_calls(invariant)


def _check_invariant_calls(invariant: ast.Invariant) -> None:
	"""Every call on the right side names something an invariant may ask.

	Refused here rather than left to the backends. A backend that meets a call
	it does not know emits no recompute and says the *build* cannot evaluate
	it, which is the right thing to say about a dynamic offset this target
	cannot resolve and the wrong thing to say about `checksum(s.a)` -- there
	is no such question to answer, in this build or any other, and a reader
	told otherwise goes looking for a better compiler.
	"""
	offered = ", ".join(f"`{name}`" for name in sorted(BUILTINS))

	for call in _calls_in(invariant.expr):
		if call.name in BUILTINS:
			continue
		raise error(
			f"`{call.name}` is not something an invariant can ask",
			call.span,
			label = "not a layout question",
			notes = [
				f"an invariant derives a field from what the layout solver "
				f"already knows: {offered}, and arithmetic over them",
				"a value that has to be computed from the bytes is a codec or "
				"a tag, not an invariant -- those have their own machinery and "
				"their own dirty bits (section 14.2)",
			],
		)


def _calls_in(expr: ast.Expr) -> list[ast.Call]:
	if isinstance(expr, ast.Call):
		return [expr, *[held for arg in expr.args for held in _calls_in(arg)]]
	if isinstance(expr, ast.Binary):
		return _calls_in(expr.left) + _calls_in(expr.right)
	if isinstance(expr, ast.Unary):
		return _calls_in(expr.operand)
	if isinstance(expr, ast.Access):
		return _calls_in(expr.base)
	return []


def _find_member(struct: ast.StructDecl, name: str) -> ast.Member | None:
	for member in _walk_members(struct.members):
		if getattr(member, "name", None) == name:
			return member
	return None


def _walk_members(members: tuple[ast.Member, ...]) -> list[ast.Member]:
	found: list[ast.Member] = []
	for member in members:
		found.append(member)
		found.extend(_walk_members(nested(member)))
	return found


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


#: Attribute names the parser accepts -- they are listed in ATTRIBUTE_NAMES so
#: that bracket disambiguation does not change meaning as phases land -- but
#: which nothing downstream reads yet. Accepting one silently is worse than
#: refusing it: the schema says the text is ASCII, or nul-terminated, and the
#: generated code neither checks nor records it. Section 8.6 describes both;
#: neither is implemented.
#: What `[encoding = ...]` may say. Section 8.6 names these two, and an
#: unknown one is refused rather than ignored: a schema declaring `utf16` and
#: getting no check would be worse off than one declaring nothing.
TEXT_ENCODINGS = frozenset({"ascii", "utf8"})


def _check_attr_list(attrs: tuple[ast.Attr, ...]) -> None:
	seen: dict[str, ast.Attr] = {}
	for attr in attrs:
		previous = seen.get(attr.name)
		if previous is not None:
			raise _redeclaration("attribute", attr.name, previous, attr)
		seen[attr.name] = attr

		if attr.name == "encoding":
			named = getattr(attr.value, "name", None)
			if named not in TEXT_ENCODINGS:
				raise error(
					f"`{named}` is not an encoding situ validates",
					attr.span,
					label = "unknown encoding",
					notes = [
						"section 8.6 names `ascii` and `utf8`",
						"an encoding nobody checks is worse than none declared: "
						"the schema would claim something the code never tests",
					],
				)


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
			if isinstance(region, ast.Sealed):
				_check_sealing_codec(region, codecs[region.codec],
				                     bound.get(region.codec))


def _check_sealing_codec(region: ast.Sealed, codec: ast.CodecDecl,
		impl: ast.ImplDecl | None) -> None:
	"""What a codec must be before it may seal (open questions 11 and 12).

	Two refusals, and both are about the stage gate meaning what it says.

	Section 14.3's gate exists so that a sealed interior is unreachable before
	its tag verifies. A codec that does not authenticate has no tag to verify,
	so the gate would be ceremony over nothing -- the caller passes `verified`,
	nothing checked anything, and the type system carries a promise the
	cryptography never made.

	And a *derived* implementation may not seal at all. Situ generates
	table-driven code, whose access pattern depends on the data it processes;
	over the plaintext of a sealed region that is a cache-timing channel, and
	section 14.6 forbids exactly that for `[secret]` bytes. Situ cannot promise
	constant time and will not pretend to, so sealing takes a tier-1 extern
	implementation, where the timing properties are the supplier's to state.
	"""
	if not codec.authenticated:
		raise error(
			f"`{codec.name}` does not authenticate, so it cannot seal a region",
			region.span,
			label = "no tag to verify",
			notes = [
				"section 14.3's gate hands out the interior only once a tag has "
				"verified; a codec without one makes that a promise nothing keeps",
				f"declare `authenticated;` in `codec {codec.name}` if it really "
				f"does, or use `coded({codec.name})` for a transform that does not",
			],
		)

	if impl is not None and impl.kind is ast.ImplKind.DERIVED:
		raise error(
			f"`{codec.name}` has a derived implementation, so it cannot seal",
			region.span,
			label = "generated code, over secret bytes",
			notes = [
				"a generated implementation is table driven, and a table indexed "
				"by secret data is a cache-timing channel -- which section 14.6 "
				"forbids for `[secret]` bytes and this would reintroduce",
				"situ cannot promise constant time, so it declines rather than "
				"pretending: bind an extern implementation whose timing is "
				f"stated, with `impl {codec.name} extern \"...\";`",
			],
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
# Registers (section 15)
# ---------------------------------------------------------------------------


def check_registers(schema: ast.Schema) -> None:
	"""The rules of 15.1 and 15.2 that need only names and numbers.

	A schema is one target or the other. Section 15.1 says so and means it: the
	surface API looks the same and the codegen is entirely different, so a file
	that mixed them would generate two kinds of accessor with no way for a
	reader to tell which model a given struct follows.
	"""
	target    = _target_of(schema)
	registers = [decl for decl in schema.structs() if decl.register is not None]
	buffers   = [decl for decl in schema.structs() if decl.register is None]

	if registers and target is not ast.TargetKind.MMIO:
		raise error(
			f"`register {registers[0].name}` needs `target mmio`",
			registers[0].span,
			label = "a register is a bus transaction, not bytes in a buffer",
			notes = ["`target mmio` makes `volatile` implicit and `access_width` "
			         "mandatory, and changes what in-place mutation means "
			         "(project.md section 15.1)"],
		)

	if target is ast.TargetKind.MMIO and buffers:
		raise error(
			f"`struct {buffers[0].name}` is a buffer layout under `target mmio`",
			buffers[0].span,
			label = "a schema may not mix the two targets",
			notes = ["the surface API looks the same and the generated code is "
			         "entirely different, so which model applies has to be a "
			         "property of the file (project.md section 15.1)",
			         "put the buffer layouts in their own schema and `import` "
			         "the type definitions"],
		)

	for decl in registers:
		_check_one_register(decl)


def _target_of(schema: ast.Schema) -> ast.TargetKind | None:
	for decl in schema.decls:
		if isinstance(decl, ast.TargetDirective):
			return decl.kind
	return None


def _check_one_register(decl: ast.StructDecl) -> None:
	register = decl.register
	assert register is not None

	if register.access_width > register.width:
		raise error(
			f"register `{decl.name}` is narrower than its access width",
			decl.span,
			label = f"width = {register.width}, access_width = "
			        f"{register.access_width}",
			notes = ["the bus cannot reach more of the register than there is"],
		)

	for member in decl.members:
		if isinstance(member, ast.Field):
			_check_register_field(decl, member)
		elif not isinstance(member, ast.Reserved):
			raise error(
				f"a register holds fields, not {type(member).__name__.lower()}",
				member.span,
				label = "not a register field",
				notes = ["a register is a fixed-width word of bit fields "
				         "(project.md section 15.2)"],
			)


def _check_register_field(decl: ast.StructDecl, member: ast.Field) -> None:
	modes = [attr for attr in member.attrs if attr.name in _ACCESS_MODE_NAMES]

	if len(modes) > 1:
		raise _redeclaration("access mode", modes[1].name, modes[0], modes[1], [
			"a field is reached one way; two modes have no combined meaning",
		])

	if member.array is not None:
		raise error(
			f"register field `{member.name}` may not be an array",
			member.span,
			label = "arrays are not addressable over a bus",
			notes = ["a register is one word; declare several registers, or a "
			         "`register_block`, instead"],
		)

	mode = next((ACCESS_MODES[attr.name] for attr in modes), ast.AccessMode.RW)
	on_read = _side_effect_attr(member, "on_read")

	if on_read is not None and not mode.readable:
		raise error(
			f"`{member.name}` is `{mode.value}` but declares `on_read`",
			on_read.span,
			label = "the bus does not let this field be read",
			notes = ["an effect on an access that cannot happen describes "
			         "nothing"],
		)

	on_write = _side_effect_attr(member, "on_write")
	if on_write is not None and not mode.writable:
		raise error(
			f"`{member.name}` is `{mode.value}` but declares `on_write`",
			on_write.span,
			label = "the bus does not let this field be written",
			notes = ["an effect on an access that cannot happen describes "
			         "nothing"],
		)


ACCESS_MODES = {mode.value: mode for mode in ast.AccessMode}
_ACCESS_MODE_NAMES = frozenset(ACCESS_MODES)


def _side_effect_attr(member: ast.Field, name: str) -> ast.Attr | None:
	for attr in member.attrs:
		if attr.name == name:
			return attr
	return None


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
