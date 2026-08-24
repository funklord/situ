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
	check_enum_members_are_spellable(schema)
	check_unique_member_names(schema)
	check_unique_attributes(schema)
	check_attribute_names(schema)
	check_types_resolve(schema)
	check_variant_exhaustiveness(schema)
	check_codec_bindings(schema)
	check_tag_coverage(schema)
	check_tag_prefixes(schema)
	check_coded_coverage(schema)
	check_attribute_places(schema)
	check_nonce_references(schema)
	check_codec_sizes(schema)
	check_registers(schema)
	check_no_recursive_types(schema)
	check_delimiters(schema)
	check_tlv_grammar(schema)
	check_index_bases(schema)
	check_repeats(schema)
	check_versions(schema)
	check_invariants(schema)
	check_relations(schema)


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
	# written as digits is as wide as the number (section 8.6.2). A
	# fixed-width one declares an array size instead and has no `until` to
	# reach this check.
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


# ---------------------------------------------------------------------------
# The item grammar of a `tlv` region (section 9.5)
# ---------------------------------------------------------------------------


def check_tlv_grammar(schema: ast.Schema) -> None:
	"""A tlv region's grammar has to be one a walk can follow.

	Every refusal here is a region whose own description does not let an item
	be found: a dispatch selecting on a part that was never decoded, a named
	tag whose wire type the dispatch rejects, a value declared to carry its own
	extent by a type that does not.

	None of this was checked while the three arguments carrying the grammar
	were held as source text. A `switch (wire)` naming no part of the tag
	parsed, compiled, and described nothing.
	"""
	varints = {decl.name for decl in schema.varints()}

	for struct in schema.structs():
		for member in _walk_members(struct.members):
			if isinstance(member, ast.Tlv):
				_check_one_tlv(member, varints)


def _check_one_tlv(region: ast.Tlv, varints: set[str]) -> None:
	_check_tag_decode(region)
	_check_identity(region)
	_check_value_size(region, varints)
	_check_known_tags(region)


def _check_identity(region: ast.Tlv) -> None:
	"""Which decoded part a `known` key matches has to be decidable.

	Inferred where only one part could be meant and declared where more than
	one could. The wrong choice here is undetectable at runtime -- an accessor
	matching a wire type where a field number was meant finds an item, just not
	that one -- which is what invariant 9 refuses to take a silent default on.
	See docs/decisions/0023-tlv-tag-identity.md.
	"""
	names = [part.name for part in region.tag_decode]

	if region.identity is not None:
		if region.part(region.identity) is None:
			raise error(
				f"`tag_identity` names `{region.identity}`, which the tag does"
				f" not decode",
				region.span,
				label = f"no `{region.identity}` part here",
				notes = [f"`tag_decode` produces {', '.join(f'`{n}`' for n in names)}"
				         if names else
				         "this region declares no `tag_decode` at all, so a"
				         " `known` key matches the raw tag"],
			)
		return

	if len(names) > 1 and region.known:
		listed = ", ".join(f"`{name}`" for name in names)
		raise error(
			f"`{region.name}` does not say which part of the tag names an item",
			region.span,
			label = f"{len(names)} parts, and a `known` map",
			notes = [f"`tag_decode` produces {listed}",
			         "add `tag_identity = <part>` to say which one a `known`"
			         " key matches",
			         "guessing would be undetectable: an accessor matching the"
			         " wrong part still finds an item"],
		)


def _check_tag_decode(region: ast.Tlv) -> None:
	"""Each part is named once and reads the raw tag and nothing else."""
	seen: dict[str, ast.TagPart] = {}
	for part in region.tag_decode:
		if part.name in seen:
			raise _redeclaration("tag part", part.name, seen[part.name], part,
			                     ["a tag decodes each part once: two"
			                      " definitions give the dispatch two"
			                      " selectors of the same name"])
		seen[part.name] = part

		for name in paths_in(part.value):
			if name == "tag":
				continue
			raise error(
				f"`{name}` is not in scope in a tag decode",
				part.value.span,
				label = f"`{name}` is not the raw tag",
				notes = ["a `tag_decode` part is an expression over `tag`,"
				         " the raw tag just read",
				         "nothing else has been read yet: the parts are what"
				         " decide where the value ends"],
			)


def _check_value_size(region: ast.Tlv, varints: set[str]) -> None:
	"""The dispatch selects on a decoded part, and each arm sizes a value."""
	sizes = region.value_size
	if sizes is None:
		return

	if region.part(sizes.selector) is None:
		known = ", ".join(f"`{part.name}`" for part in region.tag_decode)
		raise error(
			f"`{sizes.selector}` is not a part of the decoded tag",
			sizes.span,
			label = f"no `{sizes.selector}` here",
			notes = ([f"`tag_decode` produces {known}"] if known else
			         ["this region declares no `tag_decode`, so the dispatch"
			          " has nothing to select on"]),
		)

	seen: dict[int, ast.ValueCase] = {}
	default: ast.ValueCase | None = None
	for case in sizes.cases:
		if case.label is None:
			if default is not None:
				raise error(
					"a `value_size` dispatch has at most one `default`",
					case.span, label="a second `default` here")
			default = case
			continue
		if case.label in seen:
			raise error(
				f"wire type {case.label} is dispatched twice",
				case.span,
				label = "already sized above",
				notes = ["two arms for one wire type give the walk two answers"
				         " for where the value ends"],
			)
		seen[case.label] = case

		if isinstance(case.rule, ast.FixedValue) and case.rule.size == 0:
			raise error(
				f"wire type {case.label} sizes its value at zero bytes",
				case.rule.span,
				label = "a zero-length value",
				notes = ["an item that occupies only its tag is legal;"
				         " say so with `0` on the *tag* type, not here",
				         "a walk over zero-extent values does not advance"],
			)

	for case in sizes.cases:
		if isinstance(case.rule, ast.PrefixedValue) \
				and case.rule.length_type not in varints \
				and not is_scalar_name(case.rule.length_type):
			raise error(
				f"unknown length type `{case.rule.length_type}`",
				case.rule.span,
				label = "not a declared varint type or a scalar type",
				notes = ["`prefixed(T)` reads a length in `T` and then that"
				         " many bytes"],
			)


def _check_known_tags(region: ast.Tlv) -> None:
	"""Each tag is named once, and the dispatch can size what it names."""
	sizes = region.value_size
	by_tag: dict[int, ast.KnownTag] = {}
	by_name: dict[str, ast.KnownTag] = {}

	for tag in region.known:
		if tag.tag in by_tag:
			raise _redeclaration("tag", str(tag.tag), by_tag[tag.tag], tag,
			                     ["a tag names one item; which of the two an"
			                      " accessor would read is not decidable"])
		by_tag[tag.tag] = tag

		if tag.name in by_name:
			raise _redeclaration("known tag", tag.name, by_name[tag.name], tag,
			                     ["the name is what the generated accessor is"
			                      " called, and two cannot share one"])
		by_name[tag.name] = tag

		if sizes is None or tag.wire is None:
			continue

		rule = region.rule_for(tag.wire)
		if rule is None or isinstance(rule, ast.RejectValue):
			accepted = ", ".join(str(label) for label in sorted(region.wire_types))
			raise error(
				f"`{tag.name}` declares a wire type the dispatch rejects",
				tag.span,
				label = f"wire type {tag.wire} has no size",
				notes = [f"`value_size` sizes wire types {accepted}" if accepted
				         else "`value_size` sizes no wire type at all",
				         "an item with this tag could be named and never read"],
			)


def check_index_bases(schema: ast.Schema) -> None:
	"""`base` on an `indexed` region names something the parser can reach.

	The region is the default and needs no check: it is where the table
	already is. `message` is a fixed point. A member has to exist and to be
	declared before the region, for the same reason a size expression may only
	name an earlier field -- the base has to be readable at the moment the
	table is walked (decision 0024).
	"""
	for struct in schema.structs():
		for member in _walk_members(struct.members):
			if isinstance(member, ast.Indexed) \
					and member.base is ast.IndexBase.MEMBER:
				_check_one_index_base(struct, member)


def _check_one_index_base(struct: ast.StructDecl, region: ast.Indexed) -> None:
	named   = region.base_member or ""
	earlier = [str(getattr(held, "name", ""))
	           for held in _before(struct.members, region)
	           if getattr(held, "name", None)]

	if named in earlier:
		return

	later = any(getattr(held, "name", None) == named
	            for held in _flatten(struct.members))
	if later:
		raise error(
			f"`base` names `{named}`, which is declared after this region",
			region.span,
			label = f"`{named}` comes later",
			notes = ["the base has to be readable at the moment the table is"
			         " walked, which is the rule a size expression follows too",
			         f"move `{named}` before the region, or measure from"
			         " `region` or `message`"],
		)

	options = ", ".join(f"`{held}`" for held in earlier)
	raise error(
		f"`base` names `{named}`, which is not a member of `{struct.name}`",
		region.span,
		label = f"no `{named}` here",
		notes = ([f"declared before this region: {options}"] if options else
		         ["nothing is declared before this region"]) +
		        ["`region` measures from the table itself and `message` from"
		         " the start of the message"],
	)


def check_repeats(schema: ast.Schema) -> None:
	"""`while (cond)` runs a predicate over the element just read.

	The predicate reads that element's own fields and nothing else. Not the
	enclosing struct's: its later members are placed *after* this run, so
	asking about one would be circular, and its earlier members are a
	temptation worth refusing -- a condition mixing both scopes reads as
	though it were evaluated once, and it is evaluated per element.
	"""
	structs = {decl.name: decl for decl in schema.structs()}

	for struct in schema.structs():
		for member in _walk_members(struct.members):
			if not isinstance(member, (ast.Field, ast.Reserved)):
				continue
			repeat = getattr(member, "repeat", None)
			if repeat is None:
				continue
			_check_one_repeat(struct, member, repeat, structs)


def _strings_in(expr: ast.Expr) -> list[ast.StringLiteral]:
	"""Every string literal in an expression, for refusing them by name."""
	if isinstance(expr, ast.StringLiteral):
		return [expr]
	if isinstance(expr, ast.Binary):
		return _strings_in(expr.left) + _strings_in(expr.right)
	if isinstance(expr, ast.Unary):
		return _strings_in(expr.operand)
	if isinstance(expr, ast.Call):
		return [one for arg in expr.args for one in _strings_in(arg)]
	return []


def _check_one_repeat(struct: ast.StructDecl, member: ast.Field | ast.Reserved,
		repeat: ast.While, structs: Structs) -> None:
	name = getattr(member, "name", "a reserved member")

	if member.array is None:
		raise error(
			f"`{name}` is a single value, so there is no run to end",
			repeat.span,
			label = "`while` here",
			notes = ["a condition says how far a run of elements goes",
			         f"`T {name}[] while (...)` frames a run of them"],
		)

	if member.until is not None:
		raise error(
			f"`{name}` says twice where its run ends",
			repeat.span,
			label = "a condition and a delimiter",
			notes = ["`until` ends a run at a terminator standing where an "
			         "element would start; `while` ends it after an element "
			         "that fails a test",
			         "a run that stopped at whichever came first would be two "
			         "formats depending on the data"],
		)

	element = structs.get(member.type_ref.name)
	if element is None:
		raise error(
			f"`{name}` repeats `{member.type_ref.name}`, which is not a struct",
			repeat.span,
			label = "`while` here",
			notes = ["the condition reads a field of the element, so the "
			         "element has to have fields"],
		)

	# The condition compares *values*, and both gaps this closes were the
	# same acceptance from two sides. A string literal in the predicate
	# passed the front end and the C backend emitted a comparison against
	# the literal's address, calling a getter no delimited member has --
	# generated code that does not compile, found by writing an argv schema
	# whose run ends at `--` (26.124). And a reference to a delimited run
	# passed `_find_member`, though a byte run has no value to compare: that
	# is 26.113's rule, met in the condition language.
	for literal in _strings_in(repeat.predicate):
		raise error(
			"a run condition compares values, and a string is not one",
			literal.span,
			label = "no value to compare",
			notes = ["a delimited member is bytes, not a value: comparing it "
			         "to text is a job for the generated `_eq` helper, in "
			         "code that walks the run (project.md 26.124)",
			         "a condition may compare the element's integer fields "
			         "against numbers"],
		)

	for path in paths_in(repeat.predicate):
		field = path.rpartition(".")[2]
		found = _find_member(element, field)
		if found is not None and (
				getattr(found, "until", None) is not None
				or getattr(found, "array", None) is not None):
			raise error(
				f"`{field}` is a run of bytes, which has no value a "
				"condition can compare",
				repeat.span,
				label = "not a single value",
				notes = ["the condition is evaluated over the element's "
				         "*values* -- its integer fields -- and a byte run "
				         "is not one (project.md 26.113)",
				         "end the run on a field that carries a value, or "
				         "with `until` and a terminator element"],
			)
		if found is None:
			raise error(
				f"`{member.type_ref.name}` has no field `{field}`",
				repeat.span,
				label = "not a field of the element",
				notes = [
					f"the condition is evaluated against one `"
					f"{member.type_ref.name}`, not against `{struct.name}`",
					"a member of the enclosing struct is either placed after "
					"this run, which would be circular, or before it and "
					"unchanging, which reads as though the condition were "
					"evaluated once rather than per element",
				],
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
		_check_versioned_shape(tagged, {held.name for held in schema.enums()})


def _check_versioned_shape(
		tagged: Sequence[tuple[ast.Field | ast.Reserved, int | None]],
		enums: set[str]) -> None:
	"""What `[since]` is a claim about, and what no backend keeps it for.

	19.4's whole promise is that the accessor *reports* rather than guesses:
	a member the message is too old to carry has no value to return, so the
	getter hands back an error in all four languages. That is written for a
	single scalar and only for one -- a run's accessors are a length, a count
	and an index, and not one of them consults the version field. `u16
	data[n] [since = 2]` compiled in every backend and handed a version 1
	message as many elements as `n` said, out of bytes that belong to
	whatever follows.

	So it is refused here rather than accepted and ignored. Invariant 5 --
	never silently downgrade -- and an attribute that four backends drop on
	the floor is the loudest possible case of it. Implementing it means
	gating the length, the count, the index, the struct's extent and the
	framing helper in four backends, which is a construct's worth of work and
	not a patch.
	"""
	for member, since in tagged:
		if since is None or not isinstance(member, ast.Field):
			continue

		# An enum is a scalar wearing a name: one number, one width, and the
		# same out-parameter accessor the plain scalars get.
		run = member.array is not None or member.until is not None
		if not run and (member.type_ref.is_scalar
		                or member.type_ref.name in enums):
			continue

		what = "a run" if run else f"a `{member.type_ref.name}`"
		raise error(
			f"`{member.name}` is {what}, and `[since]` is only kept for a "
			f"single scalar",
			member.span,
			label = f"`since = {since}` here",
			notes = [
				"a versioned member reports its absence rather than guessing "
				"(19.4), and this one answers with a length, a count and an "
				"index, or with a sub-view -- none of which any backend gates "
				"on the version",
				"accepting it emits accessors a version 1 message does not "
				"have the bytes for, which is the silent downgrade invariant "
				"5 forbids",
				"put it behind a variant, or keep the version gate on a "
				"scalar beside it",
			],
		)


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


def check_relations(schema: ast.Schema) -> None:
	"""A relation is two named views and what must hold between them (26.95).

	Everything asked here is decidable from names and structure, which is what
	keeps it in phase 1: both parameters name real structs, every path is
	rooted at one of them, and every component of a path names a member the
	struct at that point actually has. Whether the two sides of a comparison
	are *comparable* is a resolved-layout question and is asked later.
	"""
	structs  = {decl.name: decl for decl in schema.structs()}
	imported = any(isinstance(decl, ast.ImportDirective) for decl in schema.decls)

	for relation in schema.relations():
		params: dict[str, ast.RelationParam] = {}
		for param in relation.params:
			if param.name in params:
				raise error(
					f"`{param.name}` is already a parameter of this relation",
					param.span,
					label = "duplicate parameter name",
					notes = ["the body tells the two messages apart by name, so "
					         "they need different ones -- `request` and "
					         "`response` rather than two of either"],
				)
			params[param.name] = param

			if not imported and param.type_name not in structs:
				raise error(
					f"unknown struct `{param.type_name}`",
					param.span,
					label = "no such struct",
					notes = ["a relation parameter is a view of a struct "
					         "declared in this schema"],
				)

		if not relation.body:
			raise error(
				f"`{relation.name}` states nothing about its two messages",
				relation.span,
				label = "empty relation body",
				notes = ["a relation with no `must` generates a predicate that "
				         "is true of every pair, which is the same as not "
				         "having written it"],
			)

		for must in relation.body:
			_check_must(relation, must, params, structs, imported)

		_check_exchange_policy(relation)


#: What an exchange may state about its own timing (26.98). Shape in the
#: schema and the value overridable at invocation, per decision 0032: both
#: endpoints must agree on the shape, and only the number is a deployment's.
POLICY_ATTRS = {"timeout_ms", "retries"}


def _check_exchange_policy(relation: ast.Relation) -> None:
	"""A retransmission policy is stated whole or not at all.

	Half of one is the dangerous shape. `retries = 3` with no timeout says
	retransmit three times and never says when, so a generator would have to
	invent an interval -- and a timeout situ chose rather than the protocol
	is exactly the "no behaviour the schema did not state" non-goal.
	"""
	stated = {attr.name: attr for attr in relation.attrs
	          if attr.name in POLICY_ATTRS}
	if not stated:
		return

	missing = POLICY_ATTRS - set(stated)
	if missing:
		name = next(iter(missing))
		raise error(
			f"`{relation.name}` states half a retransmission policy",
			relation.span,
			label = f"`{name}` is missing",
			notes = [
				"an exchange that retries without saying when, or waits "
				"without saying how often, leaves the other half for the "
				"generator to invent",
				"situ supplies no default here: a timeout it chose rather "
				"than the protocol is behaviour the schema did not state",
			],
		)

	for name, attr in stated.items():
		value = getattr(attr.value, "value", None)
		if not isinstance(value, int):
			raise error(
				f"`{name}` needs a number",
				attr.span,
				label = "expected an integer",
				notes = ["a deployment may override the value at `situc` "
				         "invocation; the schema states the default"],
			)
		if value <= 0:
			raise error(
				f"`{name}` is {value}",
				attr.span,
				label = "expected a positive number",
				notes = ["a zero timeout retransmits without waiting and a "
				         "zero retry count is an exchange with no policy, "
				         "which is what stating none already says"],
			)


def _check_must(relation: ast.Relation, must: ast.Must,
		params: dict[str, ast.RelationParam], structs: Structs,
		imported: bool) -> None:
	"""One constraint: rooted at the parameters, and naming members they have."""
	offered = ", ".join(f"`{name}`" for name in params)
	named: set[str] = set()

	for path in paths_in(must.expr):
		root, _, rest = path.partition(".")
		param = params.get(root)

		if param is None:
			raise error(
				f"`{root}` is not a message this relation was given",
				must.span,
				label = "unknown parameter",
				notes = [
					f"the parameters are {offered}",
					"a relation reads only the two views it is handed: it holds "
					"no state and cannot reach a message it was not passed",
				],
			)

		named.add(root)

		if not rest:
			raise error(
				f"`{root}` is a whole message, not a value",
				must.span,
				label = "expected a field of it",
				notes = [f"name a member: `{root}.some_field`"],
			)

		if not imported:
			_check_path_members(root, rest, param, must, structs)

	if len(named) < 2:
		raise error(
			"this `must` reads one message, so it is not a relation",
			must.span,
			label = "names only one of the two",
			notes = [
				"a constraint within a single message belongs on the member as "
				"`[must_eq]`, or beside the schema as a `require`",
				"put there it is checked whenever that message is validated; "
				"put here it is checked only when somebody happens to evaluate "
				"a pair, which is strictly less often",
			],
		)


def _check_path_members(root: str, rest: str, param: ast.RelationParam,
		must: ast.Must, structs: Structs) -> None:
	"""Walk `head.msg` through the struct types, refusing the first name absent.

	Resolution stops without complaint wherever the table runs out -- a scalar,
	or a type this file does not declare -- because a name that cannot be
	looked up is not a name that has been shown wrong.
	"""
	struct: ast.StructDecl | None = structs.get(param.type_name)
	walked = root

	for component in rest.split("."):
		if struct is None:
			return

		member = _find_member(struct, component)
		if member is None:
			raise error(
				f"`{struct.name}` has no member `{component}`",
				must.span,
				label = f"in `{walked}.{component}`",
				notes = [f"`{param.name}` is a view of `{param.type_name}`"],
			)

		walked = f"{walked}.{component}"
		type_ref = getattr(member, "type_ref", None)
		struct = (None if type_ref is None or type_ref.is_scalar
		          else structs.get(type_ref.name))


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
		elif isinstance(decl, ast.Relation):
			# Not because an expression could confuse the two -- a relation
			# name is never referenced from one -- but because it becomes
			# `situ_rel_<name>` in four languages, and because one word
			# meaning two things in a schema is what the naming rule is for.
			named.append(("relation", decl.name, decl))
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


#: Words a generated C++ enumerator may not be. Rust escapes a keyword with
#: `r#` and decision 0025 renames a *class* and aliases the schema's name back,
#: but an enumerator has neither escape nor alias: `enum class k { public = 0 }`
#: does not compile, and renaming it silently would mean a C++ caller writing a
#: name the schema does not contain.
#:
#: Only the ones a protocol plausibly reaches for. A schema naming a member
#: `reinterpret_cast` has other problems.
CPP_KEYWORDS = frozenset({
	"alignas", "alignof", "and", "asm", "auto", "bitand", "bitor", "bool",
	"break", "case", "catch", "char", "class", "compl", "concept", "const",
	"consteval", "constexpr", "continue", "decltype", "default", "delete",
	"do", "double", "else", "enum", "explicit", "export", "extern", "false",
	"float", "for", "friend", "goto", "if", "inline", "int", "long", "mutable",
	"namespace", "new", "noexcept", "not", "nullptr", "operator", "or",
	"private", "protected", "public", "register", "requires", "return",
	"short", "signed", "sizeof", "static", "struct", "switch", "template",
	"this", "throw", "true", "try", "typedef", "typeid", "typename", "union",
	"unsigned", "using", "virtual", "void", "volatile", "while", "xor",
})


def check_enum_members_are_spellable(schema: ast.Schema) -> None:
	"""An enum member's name has to be an identifier in every backend.

	Found by writing `examples/ble`, whose address types are `public` and
	`random` in the kernel's own constants: C++ emitted
	`enum class address_kind { public = 0, ... }` and the header did not
	compile, with nothing said at generation time (26.36). Section 17.0's rule
	is that a construct situ cannot represent is an error rather than a
	surprise later, and a name one backend cannot spell is exactly that.

	Refused rather than renamed, because renaming is the worse answer here:
	decision 0025 could alias a class back to the schema's name and an
	enumerator has no such alias, so the rename would reach a caller. The
	schema is where the choice belongs, and every such name has an ordinary
	alternative -- the kernel itself writes `ADDR_LE_DEV_PUBLIC`.
	"""
	for decl in schema.enums():
		for member in decl.members:
			if member.name not in CPP_KEYWORDS:
				continue
			raise error(
				f"`{member.name}` is a C++ keyword and cannot name an enum "
				"member",
				member.span,
				label = "not spellable in one of the backends",
				notes = [
					"Rust escapes a keyword with `r#` and a class can be "
					"renamed and aliased back (decision 0025); an enumerator "
					"has neither, so the generated C++ would not compile",
					f"give it a name that is not a keyword -- "
					f"`{member.name}_device`, `{member.name}_mode`, or "
					"whatever the format's own documentation calls it",
				],
			)


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


def check_attribute_names(schema: ast.Schema) -> None:
	"""An attribute situ has never heard of is refused, not dropped.

	The placement table (26.60) settles where a *known* attribute may sit, and
	says nothing about one that does not exist: `_attribute_place` returns
	`None` for a name it has no row for, which is the same answer it gives for
	a name that is correctly placed. So `[wibble = 16]` compiled, and the
	emitted C was byte-identical to the schema with it deleted -- measured on
	three invented attributes at once, not inferred.

	That is the failure 14.5 and 17.0 refuse everywhere else, and the other
	half of the language already refuses it: `require utterly_made_up(s)` is
	rejected as "not a builtin" with the six builtins listed. An attribute is
	the same kind of claim and gets the same treatment.

	The vocabulary is `ATTRIBUTE_NAMES`, which the parser already keeps -- for
	bracket disambiguation rather than for validation, which is exactly why
	nothing was checking spelling against it.
	"""
	from situc.parser import ATTRIBUTE_NAMES

	for decl in schema.decls:
		if isinstance(decl, ast.StructDecl):
			_check_attr_names(decl.attrs, ATTRIBUTE_NAMES)
			_check_member_attr_names(decl.members, ATTRIBUTE_NAMES)


def _check_member_attr_names(members: tuple[ast.Member, ...],
		known: frozenset[str]) -> None:
	for member in members:
		if isinstance(member, (ast.Field, ast.Reserved, ast.TagField)):
			_check_attr_names(member.attrs, known)
		_check_member_attr_names(nested(member), known)


def _check_attr_names(attrs: tuple[ast.Attr, ...],
		known: frozenset[str]) -> None:
	for attr in attrs:
		if attr.name in known:
			continue

		notes = ["nothing reads it, so the generated code is byte-identical "
		         "to the schema without it",
		         "a schema that states what the generated code does not "
		         "enforce is worse than one stating nothing (project.md "
		         "section 17.0)"]
		near  = _nearest(attr.name, set(known))
		if near is not None:
			notes.insert(0, f"`[{near}]` exists; did you mean that?")

		raise error(
			f"unknown attribute `{attr.name}`",
			attr.span,
			label = "not an attribute situ knows",
			notes = notes,
		)


#: Attribute names the parser accepts -- they are listed in ATTRIBUTE_NAMES so
#: that bracket disambiguation does not change meaning as phases land -- but
#: which nothing downstream reads yet. Accepting one silently is worse than
#: refusing it: the schema makes a claim and the generated code neither checks
#: nor records it.
#:
#: This mechanism existed for `encoding` and `nul_terminated`, emptied when
#: both were implemented, and was deleted -- leaving its own comment stranded
#: above `TEXT_ENCODINGS`, which is how the next two were found. They are
#: worse than the originals, because `[size = N]` is not merely unread: the
#: advisor used to *recommend* it, so a reader who took the advice got a
#: schema that compiled, changed nothing, and produced the same suggestion on
#: the next run.
UNIMPLEMENTED_ATTRS: dict[str, str] = {
	# Found by sweeping the placement table's remaining names (26.60): both
	# are in the parser's vocabulary and read by nothing at all.
	#
	# The only nonce anything consults is a sealed region's `nonce = ref`
	# argument, which names the field and does the work. `[nonce]` beside it
	# said the same thing to nobody -- `examples/packet` and
	# `examples/keystore` both carried one, and removing it changed not a
	# byte of any backend's output nor a line of the capability map.
	"nonce":   "a nonce is named by `sealed(codec, nonce = field)`, and this "
	           "attribute is read by nothing",
	# And the only `trusted` in the compiler is a status string `capmap`
	# prints for a tier-1 codec, derived from whether the codec has a
	# binding. No schema ever set it, which is why it cost nothing to find.
	"trusted": "a codec's trust is derived from its `impl`, and this "
	           "attribute is read by nothing",
	# The third of the same kind, and found the same way. `covers(a, b)` is a
	# *clause* on a `coded` region (14.1a), parsed by `parse_covers` and read
	# off the region node; the attribute spelling is in `ATTRIBUTE_NAMES` only
	# so that bracket disambiguation stays stable, and nothing reads it. It
	# was inert in all five positions measured -- generated C and capability
	# map both.
	"covers":  "coverage is named by `coded(...) covers(...)`, and this "
	           "attribute is read by nothing",
}

#: What `[encoding = ...]` may say. Section 8.6 names these two, and an
#: unknown one is refused rather than ignored: a schema declaring `utf16` and
#: getting no check would be worse off than one declaring nothing.
TEXT_ENCODINGS = frozenset({"ascii", "utf8"})


#: Attributes whose *place* has been established, by reading what reads them.
#:
#: An attribute was checked for spelling and never for place, so `[equalize]`
#: on a plain field or `[rw]` outside a register was accepted, dropped, and
#: produced output byte-identical to the schema without it. That is the shape
#: 14.5 refuses everywhere else -- a construct whose meaning is silently
#: nothing -- and 17.0's principle applied to attributes: a schema that states
#: what the generated code does not enforce is worse than one stating nothing.
#:
#: Each entry below was settled by finding the code that consumes the
#: attribute and reading its guard, not by inference from the name. The
#: families still unchecked are named in `UNPLACED_ATTRS`, so the remaining
#: hole is a list rather than a comment.

#: SystemRDL access modes (15.2). `layout._access_mode` returns `None` unless
#: the struct is a register *and* the member is a field, so every one of these
#: is inert anywhere else.
ACCESS_MODE_ATTRS = frozenset({
	"rw", "ro", "wo", "w1c", "w0c", "w1s", "w0s", "rc", "rs", "wo_once",
	"rsvd",
})

#: Read from `decl.attrs` and never from a member's.
STRUCT_ONLY_ATTRS = {
	"allow_straddle":       "a struct, where a bit field may cross a byte",
	"allow_host_dependent": "a struct, whose layout the host decides",
	"version":              "a struct, naming the field its `[since]` members "
	                        "are counted against",
}


def _attribute_place(struct: ast.StructDecl,
		member: ast.Member, attr: ast.Attr) -> str | None:
	"""Where `attr` would have meant something, if not here.

	`None` when the placement is fine or is not one this table has settled.
	"""
	if attr.name in ACCESS_MODE_ATTRS:
		if struct.register is not None and isinstance(member, ast.Field):
			return None
		return ("a field of a `register` struct -- outside one there is no bus "
		        "to have an access mode on")

	if attr.name in STRUCT_ONLY_ATTRS:
		return STRUCT_ONLY_ATTRS[attr.name]

	# `[on_read = clear]` and `[on_write = trigger]` are SystemRDL side
	# effects: `_read_effect` and `_side_effect` read them, and a bus is what
	# makes a read or a write an event at all. Outside a register there is
	# nothing to trigger.
	if attr.name in ("on_read", "on_write"):
		if struct.register is not None and isinstance(member, ast.Field):
			return None
		return ("a field of a `register` struct -- outside one a read is not "
		        "an event that can have an effect")

	# Bit order decides how a *packed* field's bits sit in its byte. A
	# whole-byte scalar has `endian` for the question it does have, and
	# nothing reads this on one.
	if attr.name == "bit_order":
		width = _declared_bits(member)
		if width is None or width % 8:
			return None
		return ("a bit-packed field -- a whole-byte scalar's ordering is "
		        "`endian`, not `bit_order`")

	if attr.name == "equalize":
		return None if isinstance(member, ast.Variant) else (
			"a `variant`, whose arms it pads to the largest")

	if attr.name == "allow_unverified_read":
		return None if isinstance(member, ast.Sealed) else (
			"a `sealed` region, whose stage gate it waives")

	if attr.name == "minimal":
		return None if getattr(member, "radix", None) is not None else (
			"a radix-encoded number, whose leading zeros it forbids")

	# `_reserved_policy` reads these three, and reads them from a `reserved`
	# member. `must_be_zero` is deliberately absent: it is that function's
	# default, so writing it on a reserved member changes no byte and is still
	# not meaningless -- it says out loud what the silence already meant.
	if attr.name in ("preserve", "unknown", "must_be_one"):
		return None if isinstance(member, ast.Reserved) else (
			"a `reserved` member, whose policy it sets")

	if attr.name == "encoding":
		return None if getattr(member, "array", None) is not None else (
			"a byte array or a delimited run -- a single scalar has no text "
			"to have an encoding")

	# `[size = N]` pins a member's footprint while its extent expression keeps
	# saying how much of it is meaningful (0039). Everything it is refused on
	# is a member that already has an answer to "how many bytes is this":
	# a scalar's is its type's, a literal array's is written in the brackets,
	# `[remaining]` runs to the end of the frame, and `until` and `while` put
	# the answer after the brackets rather than inside them. Two things saying
	# one thing is the ambiguity 17.0 refuses, not a redundancy to tolerate.
	if attr.name == "size":
		array = getattr(member, "array", None)
		if array is None:
			return ("an array member -- a scalar's footprint is its type's, "
			        "and there is nothing for a pin to disagree with")
		if getattr(member, "until", None) is not None \
				or getattr(member, "repeat", None) is not None:
			return ("an array sized by an expression -- `until` and `while` "
			        "already say where the member stops")
		if array.size is None or isinstance(array.size, ast.Remaining):
			return ("an array sized by an expression -- `[remaining]` runs to "
			        "the end of the frame and says so")
		from situc.parser import evaluate_literal
		if evaluate_literal(array.size) is not None:
			return ("an array sized by an expression -- a literal length is "
			        "already a fixed footprint, so a pin either repeats it or "
			        "contradicts it")

	# A tag narrower than its codec produces, said out loud (0038). Only a
	# `tag` or `checksum` has a width a codec has an opinion about, and
	# `check_codec_sizes` reads it from nowhere else -- so anywhere else it
	# would be the silent nothing 14.5 refuses.
	if attr.name == "truncated":
		return None if isinstance(member, ast.TagField) else (
			"a `tag` or `checksum`, saying its width is deliberately less "
			"than the one its codec produces")

	if attr.name == "self_as":
		return None if isinstance(member, ast.TagField) else (
			"a `tag` or `checksum`, saying what its own bytes read as while "
			"it is computed")

	# Register *settings*, parsed by `parse_register_setting` from the
	# register body. Both are in the attribute vocabulary so that bracket
	# disambiguation does not change meaning, which is not the same as a
	# member ever carrying one.
	#
	# `no_rmw` was in `UNIMPLEMENTED_ATTRS` saying "read-modify-write
	# suppression is not honoured by this build", and that was measurably
	# false: with `no_rmw;` in the body `ctrl_reg.enable` is
	# `mutate=RewriteRequired` and no single-bit setter is emitted, and
	# without it neither holds -- `access_width` alone does not do it. So the
	# feature is 15.3 working, and the message sent an author away from a
	# safety property whose whole purpose is turning an unsafe
	# read-modify-write into a compile error. Misplaced is not unimplemented.
	if attr.name in ("volatile", "no_rmw"):
		return ("a `register` body, beside `width` -- it is a setting rather "
		        "than a member attribute")

	# `Scope.narrow` applies `[endian]` to a member's own scalar, and 8.3
	# scopes it "per struct, and per field" -- a struct *directive* is
	# `decl.attrs` and never reaches here. So the only member that has a byte
	# order to override is one whose scalar has more than one byte: measured
	# inert on `u8`, on a delimited `u8[]`, on a `reserved u8` and on a
	# struct-typed member, and read on `u16` and on `u16[4]`.
	#
	# A struct-typed member is the one worth naming: `[endian = little]` on it
	# looks like it should reach the members inside and does not, because the
	# inner struct's scope was narrowed from its own declaration. Silently
	# accepting that is how a schema ends up believing it swapped an interior
	# it did not.
	if attr.name == "endian":
		width = _declared_bits(member)
		if width is not None and width > 8:
			return None
		return ("a scalar of more than one byte -- a single byte has no byte "
		        "order, and a struct-typed member does not pass one inward")

	# A bound or an equality is a claim about *a value*, and the generated
	# `validate` compares one. An array has no single value to compare and a
	# delimited run's size cap is `until ... max N`, which is syntax rather
	# than this attribute -- both measured inert.
	if attr.name in ("min", "max", "must_eq"):
		# A *text number* is the exception, and it is the whole reason this
		# rule cannot key on the brackets alone: `decimal u32 magic[6]` is a
		# six-character number with one value, not six numbers, so the
		# brackets are a width and the bound is read. cpio constrains its
		# magic exactly that way (26.113) and was refused by the first
		# version of this rule.
		if getattr(member, "radix", None) is not None:
			return None
		if getattr(member, "array", None) is not None:
			return ("a scalar field -- an array has no single value to bound, "
			        "and a size cap is spelled `max N` after `until`")
		if getattr(member, "until", None) is not None:
			return ("a scalar field -- a delimited run's cap is spelled "
			        "`max N` after `until`, which is syntax rather than this")

	return None


#: Every attribute `_attribute_place` has a rule for. One source of truth, so
#: that moving a name out of `UNPLACED_ATTRS` and forgetting to add it here is
#: the exhaustiveness test failing rather than a silent gap.
PLACED_ATTRS = (ACCESS_MODE_ATTRS | frozenset(STRUCT_ONLY_ATTRS) | frozenset({
	"equalize", "allow_unverified_read", "minimal",
	"preserve", "unknown", "must_be_one", "encoding", "self_as", "volatile",
	"on_read", "on_write", "bit_order", "endian", "min", "max", "must_eq",
	"no_rmw", "truncated", "size",
}))

#: Attributes whose place is not yet settled, so that the hole is a list here
#: rather than a paragraph somewhere else. Adding one is a row in
#: `_attribute_place` plus a refusing test and an accepting control -- the
#: control being the half that matters, since a table that refuses a valid
#: schema is worse than the silence it replaces.
#:
#: `quoted`, `escape`, `timeout_ms` and `retries` are absent because
#: `check_delimiters` and `_check_exchange_policy` already place them.
UNPLACED_ATTRS = frozenset({
	"case_insensitive", "must_be_zero", "non_canonical", "nul_terminated",
	"secret", "trim",
})


def _declared_bits(member: ast.Member) -> int | None:
	"""How many bits a member's declared type occupies, if it says.

	Read from the spelling rather than from a placement, because this check
	runs on the AST: `bit` is one, `u12` is twelve, and a name that is not a
	width at all answers None. That is enough to tell a packed field from a
	whole-byte one, which is all `bit_order` needs.
	"""
	ref = getattr(member, "type_ref", None)
	name = getattr(ref, "name", None)
	if name == "bit":
		return 1
	if isinstance(name, str) and len(name) > 1 and name[0] in "ui" \
			and name[1:].isdigit():
		return int(name[1:])
	return None


def check_attribute_places(schema: ast.Schema) -> None:
	"""An attribute has to sit where something reads it (14.5, 17.0)."""
	for struct in schema.structs():
		for member in _walk_members(struct.members):
			for attr in getattr(member, "attrs", ()):
				where = _attribute_place(struct, member, attr)
				if where is None:
					continue

				raise error(
					f"`[{attr.name}]` means nothing here",
					attr.span,
					label = f"belongs on {where}",
					notes = ["nothing reads it in this position, so the "
					         "generated code is byte-identical to the schema "
					         "without it",
					         "remove it, or move it where it is read: a schema "
					         "that states what the generated code does not "
					         "enforce is worse than one that states nothing "
					         "(project.md section 14.5)"],
				)


def _check_attr_list(attrs: tuple[ast.Attr, ...]) -> None:
	seen: dict[str, ast.Attr] = {}
	for attr in attrs:
		previous = seen.get(attr.name)
		if previous is not None:
			raise _redeclaration("attribute", attr.name, previous, attr)
		seen[attr.name] = attr

		reason = UNIMPLEMENTED_ATTRS.get(attr.name)
		if reason is not None:
			raise error(
				f"`{attr.name}` is not implemented",
				attr.span,
				label = reason,
				notes = [
					"the name is in the parser's vocabulary so that bracket "
					"disambiguation does not change meaning as phases land, "
					"which is not the same as the attribute doing anything",
					"remove it: a schema that states what the generated code "
					"does not enforce is worse than one that states nothing",
				],
			)

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

	It used to step aside entirely when a schema imported another file,
	because the missing name might legitimately live there. Imports resolve
	now (17.0a): `imports.expand` splices the named file's declarations in
	before any of these checks run, so by here every name a schema can see is
	present and an unknown one is a typo again.
	"""

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
				notes = ["declare the signature first; an implementation binds "
				         "to a contract, not the other way round"],
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
					notes = ["declare it with `codec " + region.codec
					         + " { ... }`",
					         "a codec's properties are what the lattice reads; "
					         "without them nothing can be said about the "
					         "region"],
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


def _coverable_spans(members: tuple[ast.Member, ...]) -> dict[str, ast.Member]:
	"""Everything in a struct a `coded ... covers(...)` may name.

	Wider than a tag's namespace on purpose. A tag covers *regions* because its
	coverage is an authentication boundary and a region is what draws one; a
	transform's coverage is only a range of bytes, and a plain field names one
	just as well. Header protection is the case that needs it -- QUIC masks the
	first byte, which is a field, not a region anybody would wrap.

	Recurses into `authenticated` because its members flatten into the
	enclosing struct and stay addressable, and stops at `coded` and `sealed`
	because their interiors are transform output: naming one would be asking a
	transform to run over bytes that do not exist until another has (13.3).
	"""
	found: dict[str, ast.Member] = {}
	for member in members:
		name = getattr(member, "name", None)
		if isinstance(member, ast.Authenticated):
			if name is not None:
				found[name] = member
			found.update(_coverable_spans(member.members))
		elif isinstance(member, (ast.Coded, ast.Sealed)):
			if name is not None:
				found[name] = member
		elif name is not None:
			found[name] = member
	return found


def check_coded_coverage(schema: ast.Schema) -> None:
	"""`covers(...)` on a `coded` region names spans that exist (14.1a)."""
	for struct in schema.structs():
		spans = _coverable_spans(struct.members)

		for region in _coded_regions(struct.members):
			covers = tuple(getattr(region, "covers", ()))

			# A transform that changes length may not reach outside its own
			# region. Everything it covers is already placed at a fixed
			# offset, and a codec that returns more or fewer bytes than it was
			# given would move members the layout has already committed to --
			# so the decoded form would not correspond to the struct at all.
			# Header protection, the case the clause exists for, is a mask and
			# preserves length (14.1a).
			decl = next((one for one in schema.codecs()
			             if one.name == region.codec), None)
			if covers and decl is not None \
					and decl.expansion is not ast.Expansion.PRESERVING:
				raise error(
					f"`{region.name}` covers other spans, but `{region.codec}`"
					f" does not preserve length",
					region.span,
					label = f"its expansion is `{decl.expansion.value}`",
					notes = ["a covered span sits at an offset the layout has "
					         "already fixed, and a transform that returns a "
					         "different number of bytes would move it",
					         "`covers` is for a mask or a scramble -- something "
					         "that rewrites bytes in place (project.md section "
					         "14.1a)"],
				)

			for name in covers:
				if name == region.name:
					raise error(
						f"`{region.name}` covers itself",
						region.span,
						label = "a region already transforms its own bytes",
						notes = ["`covers` names what the transform runs over "
						         "*beyond* this region; listing the region "
						         "itself is either a no-op or a second pass "
						         "over the same bytes, and neither is what the "
						         "clause means (project.md section 14.1a)"],
					)

				if name in spans:
					continue

				known = ", ".join(sorted(spans)) or "nothing in this struct"
				raise error(
					f"`{region.name}` covers unknown span `{name}`",
					region.span,
					label = "no such field or region in this struct",
					notes = [f"nameable here: {known}",
					         "unlike a tag, a coded region may cover a plain "
					         "field: its coverage is a range of bytes rather "
					         "than an authentication boundary",
					         "the interior of another coded or sealed region "
					         "cannot be named -- those bytes are transform "
					         "output and do not exist until it has run"],
				)


def check_tag_prefixes(schema: ast.Schema) -> None:
	"""`prefix(...)` names a struct this message does not contain (14.2a).

	TCP's and UDP's checksums cover a pseudo-header built from the IP layer's
	addresses, which is why the kernel's `csum_tcpudp_nofold` takes `saddr`
	and `daddr` as arguments rather than reading them out of the datagram.

	Situ describes byte layouts, and a pseudo-header is one. What it cannot do
	is fill one in from this message, so the clause names a declared struct
	and the generated code says how many bytes the caller supplies and in what
	shape. Computing the sum was already the caller's (14.1); this widens
	which bytes are covered and nothing else.
	"""
	structs = {decl.name: decl for decl in schema.structs()}

	for struct in schema.structs():
		for tag in tag_fields(struct.members):
			if tag.prefix is None:
				continue

			if tag.prefix == struct.name:
				raise error(
					f"`{tag.name}` names its own struct as its prefix",
					tag.span,
					label = f"`{struct.name}` is the message this covers",
					notes = ["a prefix is bytes the message does not contain; "
					         "to cover more of this struct, name the regions "
					         "in `covers(...)`"],
				)

			if tag.prefix not in structs:
				known = ", ".join(f"`{name}`" for name in sorted(structs)
				                  if name != struct.name) or "none"
				raise error(
					f"`{tag.name}` has an unknown prefix `{tag.prefix}`",
					tag.span,
					label = "no such struct in this file",
					notes = [f"structs declared here: {known}",
					         "a prefix is a layout the caller builds and hands "
					         "over, so it has to be one this schema describes "
					         "-- situ has no import resolution to find it "
					         "elsewhere (17.0a)"],
				)


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


def _attr(attrs: tuple[ast.Attr, ...], name: str) -> ast.Attr | None:
	return next((attr for attr in attrs if attr.name == name), None)


def _check_tag_is_outside_its_coverage(tag: ast.TagField, covers: tuple[str, ...],
		by_name: dict[str, ast.Authenticated | ast.Sealed]) -> None:
	"""A tag may not sit inside the bytes it authenticates, unless it says
	what its own bytes read as while it is computed.

	Computing it would otherwise need its own value as input, which is not
	recoverable at run time and so is an error here. That is true of a
	cryptographic tag and false of the checksum family: RFC 1071 defines the
	Internet checksum over the header *including* the checksum field, taken as
	zero, and IPv4, ICMP, TCP and UDP all carry one. GPT's header CRC zeroes
	its own field the same way and tar's header sum uses spaces, which is why
	`self_as` carries a value rather than being a flag.
	"""
	inside = next((name for name in covers
	               if (region := by_name.get(name)) is not None
	               and any(held is tag for held in tag_fields(region.members))),
	              None)
	filler = _attr(tag.attrs, "self_as")

	if inside is not None and filler is None:
		raise error(
			f"`{tag.name}` is inside the region it covers",
			tag.span,
			label = f"declared inside `{inside}`",
			notes = ["computing it would take its own bytes as input",
			         f"move it out of `{inside}`, or narrow its `covers` "
			         "clause to regions that do not contain it",
			         "a checksum defined over its own field -- the Internet "
			         "checksum, a GPT header CRC -- declares what those bytes "
			         "read as instead: `[self_as = 0]`"],
		)

	if inside is None and filler is not None:
		raise error(
			f"`{tag.name}` is not inside the region it covers",
			filler.span,
			label = "`self_as` has nothing to stand in for",
			notes = ["it says what the tag's *own* bytes read as while the "
			         "algorithm runs, which is a question only a tag inside "
			         "its own coverage has"],
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

	And one nonce field may not feed two sealed regions. 14.8 has claimed
	that refusal since the survey was written, and it was never implemented
	(26.127): a schema with two regions sealed under one nonce built in every
	backend. Under one key, a repeated nonce is the worst failure an AEAD
	has -- GCM gives up the authentication key, not just the two plaintexts
	-- and with key selection not yet expressible (14.8), one field feeding
	two regions is that failure written into the format. When a `key = ...`
	argument exists, regions under provably distinct keys are the case that
	relaxes this.
	"""
	for struct in schema.structs():
		fed: dict[str, ast.Member] = {}
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
				earlier = fed.get(name)
				if earlier is not None:
					raise error(
						f"`{name}` seeds two sealed regions",
						region.span,
						label = "sealed with the same nonce",
						notes = [
							"under one key, a repeated nonce is the worst "
							"failure an AEAD has: GCM yields the "
							"authentication key, not just the plaintexts",
							"give each region its own nonce field; a key "
							"per region is not yet expressible "
							"(project.md section 14.8)",
						],
					)
				fed[name] = region
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


def _declared_byte_width(member: ast.Member) -> int | None:
	"""How many bytes a member occupies, where the schema says so literally.

	`None` where it does not: an array sized by an expression, a `[remaining]`
	run, a type whose width is not in its name. A size the compiler cannot see
	is one it cannot check a declared width against, and guessing would be
	worse than the silence 0038 already permits.
	"""
	from situc.parser import evaluate_literal

	bits = _declared_bits(member)
	if bits is None or bits % 8:
		return None

	array = getattr(member, "array", None)
	if array is None:
		return bits // 8

	count = evaluate_literal(array.size) if array.size is not None else None
	if count is None or count < 0:
		return None
	return (bits // 8) * count


def check_codec_sizes(schema: ast.Schema) -> None:
	"""A declared tag or nonce is the width its codec says (decision 0038).

	`tag u8[16]` beside `codec aes_gcm_128 { authenticated; ... }` had nothing
	relating the sixteen to the codec, so `tag u8[1]` compiled and a
	deliberate truncation read exactly like a typo. Truncation is real --
	OSCORE uses eight bytes on constrained links -- so it is made sayable with
	`[truncated]` rather than banned.

	Silence on either side checks nothing: an extern codec's implementation is
	somebody else's, and an author who does not know its tag width must still
	be able to declare it.
	"""
	codecs = {decl.name: decl for decl in schema.codecs()}

	for struct in schema.structs():
		regions = auth_regions(struct.members)
		by_name = {region.name: region for region in regions}

		for tag in tag_fields(struct.members):
			width = _declared_byte_width(tag)
			if width is None:
				continue
			for name in coverage_of(tag, regions):
				region = by_name.get(name)
				codec  = codecs.get(getattr(region, "codec", "") or "")
				if codec is not None and codec.tag_bytes is not None:
					_check_tag_width(tag, width, codec)

		for region in regions:
			if not isinstance(region, ast.Sealed):
				continue
			codec = codecs.get(region.codec)
			if codec is None or codec.nonce_bytes is None:
				continue

			reference = _argument(region.args, "nonce")
			if reference is None:
				continue
			field = _nonce_field(struct, region, _reference_name(reference))
			if field is None:
				continue

			width = _declared_byte_width(field)
			if width is None or width == codec.nonce_bytes:
				continue

			# No exemption either way. A nonce is an *input* rather than a
			# result, so a narrower one is not a truncation of anything -- it
			# is simply a different nonce, and the primitive will read past it
			# or pad it without either side saying so.
			raise error(
				f"`{field.name}` is {width} bytes and `{codec.name}` takes a "
				f"{codec.nonce_bytes}-byte nonce",
				field.span,
				label = f"expected {codec.nonce_bytes} bytes",
				notes = ["a nonce is an input rather than a result, so a "
				         "different width is a different nonce rather than a "
				         "truncation of one",
				         "widen the field, or correct `nonce_bytes` on the "
				         "codec (project.md section 14.8, decision 0038)"],
			)


def _nonce_field(struct: ast.StructDecl, region: ast.Member,
		name: str | None) -> ast.Field | None:
	if name is None:
		return None
	for field in _fields(_before(struct.members, region)):
		if field.name == name:
			return field
	return None


def _check_tag_width(tag: ast.TagField, width: int,
		codec: ast.CodecDecl) -> None:
	assert codec.tag_bytes is not None
	if width == codec.tag_bytes:
		return

	truncated = any(attr.name == "truncated" for attr in tag.attrs)

	if width > codec.tag_bytes:
		# No exemption. Nothing an author could write makes a primitive
		# produce more authentication than it produces.
		raise error(
			f"`{tag.name}` is {width} bytes and `{codec.name}` produces "
			f"{codec.tag_bytes}",
			tag.span,
			label = f"wider than the tag `{codec.name}` produces",
			notes = ["a tag cannot be wider than what computes it: the extra "
			         "bytes would authenticate nothing",
			         "narrow the field, or correct `tag_bytes` on the codec "
			         "(decision 0038)"],
		)

	if not truncated:
		raise error(
			f"`{tag.name}` is {width} bytes and `{codec.name}` produces "
			f"{codec.tag_bytes}",
			tag.span,
			label = "narrower than the tag it holds",
			notes = ["a truncated tag is legitimate -- OSCORE uses eight "
			         "bytes on constrained links -- so say so with "
			         "`[truncated]` and this is accepted",
			         "without it a deliberate truncation and a typo are the "
			         "same text (decision 0038)"],
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
			for arm in variant.arms:
				_check_arm_member_is_not_located(arm)


def _check_arm_member_is_not_located(arm: ast.VariantArm) -> None:
	"""`at expr` on an arm member is refused, because it was ignored.

	Every backend's arm path resolves the member's offset by summing what
	precedes it, and none consults `located` -- the C emitter hard-coded the
	offset with the expression nowhere in it, and the other three matched.
	The layout recorded the `at` faithfully, so a schema saying "the
	positional's text starts at 0, dispatch byte included" parsed, did
	nothing, and said nothing about doing nothing (26.124's argv exercise,
	reaching for exactly that).

	Refused rather than implemented, for now: an arm member re-addressed over
	the discriminant is overlap, and overlap is a canonicity question that
	deserves a decision rather than an emitter patch. The refusal converts
	four silent wrongs into one loud one, and implementing later is additive.
	"""
	member = arm.member
	if member is None or getattr(member, "located", None) is None:
		return
	raise error(
		"`at` on a variant arm member is not implemented",
		member.span,
		label = "the arm's offset ignores it",
		notes = ["every backend places an arm member after the discriminant "
		         "and none consults `at`, so the schema would state what the "
		         "generated code does not do (project.md 26.124)",
		         "move the member out of the variant, or leave its offset to "
		         "the arm"],
	)


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
