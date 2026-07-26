"""Render an AST back to situ source.

Exists so "dump-ast round-trips example 5.1 exactly" (project.md section 26.1)
is a property that can be tested rather than eyeballed: parsing, unparsing and
reparsing must reach the same tree.

Output follows the project's own conventions -- tabs for structural indent,
spaces for alignment within a level -- because the same renderer will be the
starting point for `situc doc`.
"""

from __future__ import annotations

from situc import ast

# Loosest first, matching parser.PRECEDENCE. A child is parenthesised only when
# it binds more loosely than its parent, so the output stays readable and still
# reparses to an identical tree.
_BINDING = {
	"||": 0, "&&": 1, "|": 2, "^": 3, "&": 4,
	"==": 5, "!=": 5,
	"<": 6, ">": 6, "<=": 6, ">=": 6,
	"<<": 7, ">>": 7,
	"+": 8, "-": 8,
	"*": 9, "/": 9, "%": 9,
}

_UNARY_BINDING = 10
_ATOM_BINDING  = 99


def unparse(schema: ast.Schema) -> str:
	lines: list[str] = []
	previous: str | None = None

	for decl in schema.decls:
		group = _group(decl)
		# One-line declarations of the same kind cluster; blocks always stand
		# apart, and so does a change of kind.
		if previous is not None and (group != previous or group == "block"):
			lines.append("")
		lines.extend(decl_lines(decl))
		previous = group

	return "\n".join(lines) + "\n"


def _group(decl: ast.Decl) -> str:
	if isinstance(decl, (ast.TargetDirective, ast.EndianDirective,
	                     ast.BitOrderDirective, ast.ImportDirective)):
		return "directive"
	if isinstance(decl, ast.ConstDecl):
		return "const"
	if isinstance(decl, ast.Requirement):
		return "requirement"
	return "block"


def decl_lines(decl: ast.Decl) -> list[str]:
	if isinstance(decl, ast.TargetDirective):
		return [f"target {decl.kind.value};"]

	if isinstance(decl, ast.EndianDirective):
		return [f"endian {decl.endian.value};"]

	if isinstance(decl, ast.BitOrderDirective):
		return [f"bit_order {decl.bit_order.value};"]

	if isinstance(decl, ast.ImportDirective):
		return [f'import "{_escape(decl.path)}";']

	if isinstance(decl, ast.ConstDecl):
		return [f"const {decl.name} = {expr_to_source(decl.value)};"]

	if isinstance(decl, ast.EnumDecl):
		return _enum_lines(decl)

	if isinstance(decl, ast.StructDecl):
		return _struct_lines(decl)

	if isinstance(decl, ast.EndianMarkerDecl):
		return [
			f"endian_marker {decl.name} : {decl.backing.name} {{",
			f"\tlittle = {expr_to_source(decl.little)},",
			f"\tbig = {expr_to_source(decl.big)},",
			"}",
		]

	if isinstance(decl, ast.Requirement):
		return [f"{decl.kind.value} {expr_to_source(decl.expr)};"]

	raise TypeError(f"cannot unparse {type(decl).__name__}")


def _enum_lines(decl: ast.EnumDecl) -> list[str]:
	lines = [f"enum {decl.name} : {decl.backing.name} {{"]
	for member in decl.members:
		lines.append(f"\t{member.name} = {expr_to_source(member.value)},")
	if decl.default is not None:
		lines.append(f"\tdefault = {decl.default.value},")
	lines.append("}")
	return lines


def _struct_lines(decl: ast.StructDecl) -> list[str]:
	header = f"struct {decl.name}{_attrs_to_source(decl.attrs)} {{"
	lines  = [header]
	lines.extend(member_lines(decl.members, depth=1))
	lines.append("}")
	return lines


def member_lines(members: tuple[ast.Member, ...], depth: int) -> list[str]:
	indent = "\t" * depth
	lines: list[str] = []

	for member in members:
		if isinstance(member, ast.PositionalBlock):
			lines.append(f"{indent}positional {{")
			lines.extend(member_lines(member.members, depth + 1))
			lines.append(f"{indent}}}")
		else:
			lines.append(indent + member_to_source(member))

	return lines


def member_to_source(member: ast.Member) -> str:
	if isinstance(member, ast.Field):
		parts = [member.type_ref.name, " ", member.name, _array_to_source(member.array)]
		if member.pin is not None:
			parts.append(f" @ {expr_to_source(member.pin)}")
		parts.append(_attrs_to_source(member.attrs))
		parts.append(";")
		return "".join(parts)

	if isinstance(member, ast.MarkerField):
		return f"endian_marker {member.name}{_attrs_to_source(member.attrs)};"

	if isinstance(member, ast.Reserved):
		return (f"reserved {member.type_ref.name}{_array_to_source(member.array)}"
		        f"{_attrs_to_source(member.attrs)};")

	raise TypeError(f"cannot unparse {type(member).__name__}")


def _array_to_source(array: ast.ArraySpec | None) -> str:
	if array is None:
		return ""
	return f"[{expr_to_source(array.size)}]" if array.size is not None else "[]"


def _attrs_to_source(attrs: tuple[ast.Attr, ...]) -> str:
	if not attrs:
		return ""
	rendered = ", ".join(
		attr.name if attr.value is None else f"{attr.name} = {expr_to_source(attr.value)}"
		for attr in attrs
	)
	return f" [{rendered}]"


def expr_to_source(expr: ast.Expr) -> str:
	return _expr(expr, parent_binding=0)


def _expr(expr: ast.Expr, parent_binding: int) -> str:
	if isinstance(expr, ast.IntLiteral):
		return expr.text

	if isinstance(expr, ast.StringLiteral):
		return f'"{_escape(expr.value)}"'

	if isinstance(expr, ast.NameRef):
		return expr.name

	if isinstance(expr, ast.Remaining):
		return "remaining"

	if isinstance(expr, ast.Access):
		return f"{_expr(expr.base, _ATOM_BINDING)}.{expr.name}"

	if isinstance(expr, ast.Index):
		inner = "" if expr.index is None else expr_to_source(expr.index)
		return f"{_expr(expr.base, _ATOM_BINDING)}[{inner}]"

	if isinstance(expr, ast.Call):
		args = ", ".join(expr_to_source(arg) for arg in expr.args)
		return f"{expr.name}({args})"

	if isinstance(expr, ast.Unary):
		rendered = f"{expr.op}{_expr(expr.operand, _UNARY_BINDING)}"
		return _wrap(rendered, _UNARY_BINDING, parent_binding)

	if isinstance(expr, ast.Binary):
		binding  = _BINDING[expr.op]
		left     = _expr(expr.left, binding)
		# The right operand of a left-associative operator needs a parenthesis
		# at equal binding, or `a - (b - c)` would reparse as `(a - b) - c`.
		right    = _expr(expr.right, binding + 1)
		rendered = f"{left} {expr.op} {right}"
		return _wrap(rendered, binding, parent_binding)

	raise TypeError(f"cannot unparse {type(expr).__name__}")


def _wrap(rendered: str, binding: int, parent_binding: int) -> str:
	return f"({rendered})" if binding < parent_binding else rendered


def _escape(text: str) -> str:
	out = []
	for char in text:
		if char in '\\"':
			out.append("\\" + char)
		elif char == "\n":
			out.append("\\n")
		elif char == "\t":
			out.append("\\t")
		elif char == "\r":
			out.append("\\r")
		elif char == "\0":
			out.append("\\0")
		else:
			out.append(char)
	return "".join(out)
