"""Flatten namespaces away, immediately after parsing.

A namespace scopes type names and nothing else, so it has no business surviving
into the layout solver or the lattice. Every declaration inside one comes out
with a qualified name -- `outer::Header` -- and every reference to it inside the
same namespace is rewritten to match. After this pass a namespaced schema is
indistinguishable from one whose author happened to write long names, and no
later pass learns that namespaces exist.

`::` rather than `.` is what makes that work. A path is already `Type.field.sub`
and a dot already means two things there; a third would make the head of a path
ambiguous, and every pass that splits a path on its first dot would have to
learn the rule. With `::` the head of `outer::Header.seq` is still one token's
worth of name, and those passes are untouched.

Unqualified names resolve in the current namespace and nowhere else. There is
deliberately no fallback to the enclosing file: a schema that silently picked
the wrong `Header` would produce a layout that looks right and is not, and
guessing between two candidates is what section 17.0 forbids.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from situc import ast
from situc.diagnostics import error

SEPARATOR = "::"


def qualify(namespace: str, name: str) -> str:
	return f"{namespace}{SEPARATOR}{name}"


def namespace_of(name: str) -> str:
	"""The namespace a qualified name sits in, or the empty string."""
	head, separator, _ = name.rpartition(SEPARATOR)
	return head if separator else ""


def flatten(schema: ast.Schema) -> ast.Schema:
	"""Replace every namespace with its contents, qualified."""
	decls: list[ast.Decl] = []

	for decl in schema.decls:
		if not isinstance(decl, ast.NamespaceDecl):
			decls.append(decl)
			continue

		decls.extend(_flatten_one(decl))

	schema.decls = decls
	return schema


def _flatten_one(namespace: ast.NamespaceDecl) -> list[ast.Decl]:
	declared = set(_declared_names(namespace.decls))

	if not declared:
		raise error(
			f"namespace `{namespace.name}` declares no types",
			namespace.span,
			label = "nothing to scope",
			notes = ["a namespace exists to keep two types of the same name "
			         "apart; one holding only directives or requirements says "
			         "nothing"],
		)

	def qualified(name: str) -> str:
		return qualify(namespace.name, name) if name in declared else name

	return [rewrite(decl, qualified) for decl in namespace.decls]


def unqualify(decl: ast.Decl, namespace: str) -> ast.Decl:
	"""The inverse, for rendering a flattened schema back as source.

	Only the unparser needs this. Flattening is what every other pass sees, and
	reconstructing the blocks is the one place the original shape matters.
	"""
	prefix = namespace + SEPARATOR

	def stripped(name: str) -> str:
		return name[len(prefix):] if name.startswith(prefix) else name

	return rewrite(decl, stripped)


def _declared_names(decls: list[ast.Decl]) -> list[str]:
	found = []
	for decl in decls:
		if isinstance(decl, (ast.StructDecl, ast.EnumDecl, ast.VarintDecl,
		                     ast.EndianMarkerDecl, ast.CodecDecl, ast.ConstDecl,
		                     ast.Relation)):
			found.append(decl.name)
	return found


def rewrite(decl: ast.Decl, name_of: Callable[[str], str]) -> ast.Decl:
	"""Rename a declaration and every reference it makes, by one rule.

	The declaration's own name goes through the same function as the names it
	refers to, so qualifying and unqualifying are the same walk with different
	arguments rather than two walks that have to agree.
	"""
	def expr_of(expr: ast.Expr | None) -> ast.Expr | None:
		return None if expr is None else _rewrite_expr(expr, name_of)

	if isinstance(decl, ast.StructDecl):
		return replace(decl,
		               name    = name_of(decl.name),
		               members = tuple(_rewrite_member(member, name_of)
		                               for member in decl.members),
		               attrs   = _rewrite_attrs(decl.attrs, name_of))

	if isinstance(decl, (ast.EnumDecl, ast.VarintDecl, ast.EndianMarkerDecl,
	                     ast.CodecDecl)):
		return replace(decl, name = name_of(decl.name))

	if isinstance(decl, ast.ImplDecl):
		return replace(decl, codec = name_of(decl.codec))

	if isinstance(decl, ast.ConstDecl):
		value = expr_of(decl.value)
		assert value is not None
		return replace(decl, name = name_of(decl.name), value = value)

	if isinstance(decl, ast.Requirement):
		expr = expr_of(decl.expr)
		assert expr is not None
		return replace(decl, expr = expr)

	if isinstance(decl, ast.Invariant):
		# `derived` is a path rather than an expression -- an invariant names
		# the one field it maintains -- so the head is qualified here while
		# the right-hand side goes through the expression rewriter like every
		# other. Only the head, for the reason `_rewrite_expr` gives: `s` is
		# the name being scoped and `total` is a field of whatever it
		# resolves to.
		#
		# Falling through to the directive case below left an invariant
		# inside a namespace naming a struct that flattening had renamed, so
		# `check_invariants` refused it with "unknown struct" -- a construct
		# that could not be written at all rather than one that read wrong.
		head, dot, rest = decl.derived.partition(".")
		expr = expr_of(decl.expr)
		assert expr is not None
		return replace(decl,
		               derived = name_of(head) + dot + rest,
		               expr    = expr)

	if isinstance(decl, ast.Relation):
		# The name and the parameter *types* are namespace-scoped. The body is
		# deliberately not rewritten: its paths are rooted at parameter names,
		# which are local to the relation and mean nothing outside it. Passing
		# them through `name_of` would qualify `request` into
		# `outer::request` and produce a relation whose body referred to
		# nothing -- the one case where leaving an expression alone is the
		# correct rewrite.
		return replace(
			decl,
			name   = name_of(decl.name),
			params = tuple(replace(param, type_name = name_of(param.type_name))
			               for param in decl.params),
		)

	# A directive inside a namespace refers to nothing and scopes nothing; it
	# keeps its file-wide meaning, which is the one it already had.
	return decl


def _rewrite_member(member: ast.Member,
		name_of: Callable[[str], str]) -> ast.Member:

	def expr_of(expr: ast.Expr | None) -> ast.Expr | None:
		return None if expr is None else _rewrite_expr(expr, name_of)

	def array_of(array: ast.ArraySpec | None) -> ast.ArraySpec | None:
		if array is None or array.size is None:
			return array
		return replace(array, size = _rewrite_expr(array.size, name_of))

	def type_of(type_ref: ast.TypeRef) -> ast.TypeRef:
		if type_ref.is_scalar:
			return type_ref
		return replace(type_ref, name = name_of(type_ref.name))

	if isinstance(member, ast.Field):
		return replace(member,
		               type_ref = type_of(member.type_ref),
		               array    = array_of(member.array),
		               pin      = expr_of(member.pin),
		               attrs    = _rewrite_attrs(member.attrs, name_of))

	if isinstance(member, ast.Reserved):
		return replace(member,
		               type_ref = type_of(member.type_ref),
		               array    = array_of(member.array),
		               attrs    = _rewrite_attrs(member.attrs, name_of))

	if isinstance(member, ast.TagField):
		array = array_of(member.array)
		assert array is not None
		return replace(member,
		               type_ref = type_of(member.type_ref),
		               array    = array,
		               attrs    = _rewrite_attrs(member.attrs, name_of))

	if isinstance(member, ast.MarkerField):
		return replace(member, attrs = _rewrite_attrs(member.attrs, name_of))

	if isinstance(member, ast.Opaque):
		size = expr_of(member.size)
		assert size is not None
		return replace(member, size = size,
		               attrs = _rewrite_attrs(member.attrs, name_of))

	if isinstance(member, ast.Variant):
		discriminant = _rewrite_expr(member.discriminant, name_of)
		arms = tuple(replace(
			arm,
			value  = expr_of(arm.value),
			member = None if arm.member is None
			         else _rewrite_member(arm.member, name_of),
		) for arm in member.arms)
		return replace(member, discriminant = discriminant, arms = arms,
		               attrs = _rewrite_attrs(member.attrs, name_of))

	if isinstance(member, (ast.Coded, ast.Sealed)):
		return replace(member,
		               codec   = name_of(member.codec),
		               args    = _rewrite_attrs(member.args, name_of),
		               members = tuple(_rewrite_member(inner, name_of)
		                               for inner in member.members),
		               attrs   = _rewrite_attrs(member.attrs, name_of))

	if isinstance(member, (ast.Authenticated, ast.PositionalBlock)):
		return replace(member, members = tuple(_rewrite_member(inner, name_of)
		                                       for inner in member.members))

	if isinstance(member, ast.Indexed):
		return replace(member,
		               args    = _rewrite_attrs(member.args, name_of),
		               members = tuple(_rewrite_member(inner, name_of)
		                               for inner in member.members))

	if isinstance(member, ast.Tlv):
		return replace(member,
		               args  = _rewrite_attrs(member.args, name_of),
		               attrs = _rewrite_attrs(member.attrs, name_of))

	return member


def _rewrite_attrs(attrs: tuple[ast.Attr, ...], name_of: Callable[[str], str]) -> tuple[ast.Attr, ...]:
	return tuple(
		attr if attr.value is None
		else replace(attr, value = _rewrite_expr(attr.value, name_of))
		for attr in attrs)


def _rewrite_expr(expr: ast.Expr, name_of: Callable[[str], str]) -> ast.Expr:
	"""Qualify the head of every reference to a name this namespace declares.

	Only the head: `MsgType.hello` names an enum and then a member of it, and
	the member belongs to the enum rather than to the namespace. The same holds
	for `Header.seq` in a requirement -- `Header` is the name being scoped and
	`seq` is a field of whatever it resolves to.
	"""
	if isinstance(expr, ast.NameRef):
		return replace(expr, name = name_of(expr.name))

	if isinstance(expr, ast.Access):
		return replace(expr, base = _rewrite_expr(expr.base, name_of))

	if isinstance(expr, ast.Index):
		return replace(expr,
		               base  = _rewrite_expr(expr.base, name_of),
		               index = None if expr.index is None
		                       else _rewrite_expr(expr.index, name_of))

	if isinstance(expr, ast.Unary):
		return replace(expr, operand = _rewrite_expr(expr.operand, name_of))

	if isinstance(expr, ast.Binary):
		return replace(expr,
		               left  = _rewrite_expr(expr.left, name_of),
		               right = _rewrite_expr(expr.right, name_of))

	if isinstance(expr, ast.Call):
		return replace(expr, args = tuple(_rewrite_expr(arg, name_of)
		                                  for arg in expr.args))

	return expr
