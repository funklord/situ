"""Render an AST back to situ source.

Exists so "dump-ast round-trips example 5.1 exactly" (project.md section 26.1)
is a property that can be tested rather than eyeballed: parsing, unparsing and
reparsing must reach the same tree.

Output follows the project's own conventions -- tabs for structural indent,
spaces for alignment within a level -- because the same renderer will be the
starting point for `situc doc`.
"""

from __future__ import annotations

from situc import ast, namespaces

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
	"""Render a schema as source that reparses to the same tree.

	Namespaces were flattened away at parse time, so they are reconstructed
	here from the qualified names: a run of declarations sharing one becomes a
	block again, with the qualification stripped back off inside it. That is
	what keeps the round-trip property true for a namespaced schema rather than
	true only for a flat one.
	"""
	lines: list[str] = []
	previous: str | None = None

	for namespace, decls in _by_namespace(schema.decls):
		if namespace:
			if lines:
				lines.append("")
			lines.append(f"namespace {namespace} {{")
			lines.extend(_indent(_render(
				[namespaces.unqualify(decl, namespace) for decl in decls])))
			lines.append("}")
			previous = "block"
			continue

		for decl in decls:
			group = _group(decl)
			# One-line declarations of the same kind cluster; blocks always
			# stand apart, and so does a change of kind.
			if previous is not None and (group != previous or group == "block"):
				lines.append("")
			lines.extend(decl_lines(decl))
			previous = group

	return "\n".join(lines) + "\n"


def _render(decls: list[ast.Decl]) -> list[str]:
	lines: list[str] = []
	for index, decl in enumerate(decls):
		if index:
			lines.append("")
		lines.extend(decl_lines(decl))
	return lines


def _indent(lines: list[str]) -> list[str]:
	return [f"\t{line}" if line else line for line in lines]


def _by_namespace(decls: list[ast.Decl]) -> list[tuple[str, list[ast.Decl]]]:
	"""Consecutive declarations grouped by the namespace they were written in.

	Consecutive, not gathered: a schema that opened a namespace twice wrote two
	blocks, and rendering them as one would change the source it came from more
	than it has to.
	"""
	grouped: list[tuple[str, list[ast.Decl]]] = []

	for decl in decls:
		namespace = namespaces.namespace_of(getattr(decl, "name", ""))
		if grouped and grouped[-1][0] == namespace:
			grouped[-1][1].append(decl)
		else:
			grouped.append((namespace, [decl]))

	return grouped


def _group(decl: ast.Decl) -> str:
	if isinstance(decl, (ast.TargetDirective, ast.EndianDirective,
	                     ast.BitOrderDirective, ast.ImportDirective,
	                     ast.StrictnessDirective)):
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

	if isinstance(decl, ast.StrictnessDirective):
		return [f"strictness = {decl.strictness.value};"]

	if isinstance(decl, ast.ConstDecl):
		return [f"const {decl.name} = {expr_to_source(decl.value)};"]

	if isinstance(decl, ast.EnumDecl):
		return _enum_lines(decl)

	if isinstance(decl, ast.StructDecl):
		return _struct_lines(decl)

	if isinstance(decl, ast.VarintDecl):
		lines = [f"varint_type {decl.name} {{",
		         f"\tencoding = {decl.encoding.value};"]
		if decl.transform is not None:
			lines.append(f"\ttransform = {decl.transform.value};")
		lines.append(f"\tmax_bits = {decl.max_bits};")
		if decl.minimal:
			lines.append("\tminimal;")
		lines.append("}")
		return lines

	if isinstance(decl, ast.EndianMarkerDecl):
		return [
			f"endian_marker {decl.name} : {decl.backing.name} {{",
			f"\tlittle = {expr_to_source(decl.little)},",
			f"\tbig = {expr_to_source(decl.big)},",
			"}",
		]

	if isinstance(decl, ast.CodecDecl):
		return _codec_lines(decl)

	if isinstance(decl, ast.ImplDecl):
		if decl.kind is ast.ImplKind.EXTERN:
			return [f'impl {decl.codec} extern "{_escape(decl.symbol or "")}";']
		return [f"impl {decl.codec} derived;"]

	if isinstance(decl, ast.Requirement):
		return [f"{decl.kind.value} {expr_to_source(decl.expr)};"]

	raise TypeError(f"cannot unparse {type(decl).__name__}")


def _codec_lines(decl: ast.CodecDecl) -> list[str]:
	"""A property signature, rendered as the section 13.2 vocabulary.

	Every property is written out, including the ones that were defaulted. A
	signature is the entire interface to the lattice, so a round-tripped one
	that dropped a silent default would say something different from what it
	came from.
	"""
	lines = [f"codec {decl.name} {{", f"\t{codec_expansion(decl)};",
	         f"\tgranularity = {codec_granularity(decl)};"]

	if decl.seekable is ast.Seekable.NONE:
		lines.append("\tnot seekable;")
	else:
		lines.append(f"\tseekable = {decl.seekable.value};")

	for flag in ("systematic", "authenticated", "invertible", "deterministic",
	             "error_propagating"):
		if getattr(decl, flag):
			lines.append(f"\t{flag};")

	lines.append("}")
	return lines


def codec_expansion(decl: ast.CodecDecl) -> str:
	if decl.expansion is ast.Expansion.PRESERVING:
		return "length_preserving"
	if decl.expansion is ast.Expansion.FIXED_ADD:
		return f"expansion = +{decl.expansion_add}"
	if decl.expansion is ast.Expansion.UNBOUNDED:
		return "expansion = unbounded"

	assert decl.ratio is not None
	return f"expansion = {decl.expansion.value}({decl.ratio[0]}, {decl.ratio[1]})"


def codec_granularity(decl: ast.CodecDecl) -> str:
	"""`block(16)`, or the bare form.

	`block(any)` carries no size, so it renders as `block` and reparses to the
	same signature. The round-trip property is over the tree, not the text.
	"""
	if decl.granularity_size is None:
		return decl.granularity.value
	return f"{decl.granularity.value}({decl.granularity_size})"


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
		elif isinstance(member, ast.Indexed):
			lines.append(f"{indent}indexed({_args_to_source(member.args)}) {{")
			lines.extend(member_lines(member.members, depth + 1))
			lines.append(f"{indent}}}")
		elif isinstance(member, ast.Coded):
			lines.append(f"{indent}coded {member.name}"
			             f"({_codec_args(member.codec, member.args)})"
			             f"{_attrs_to_source(member.attrs)} {{")
			lines.extend(member_lines(member.members, depth + 1))
			lines.append(f"{indent}}}")
		elif isinstance(member, ast.Sealed):
			lines.append(f"{indent}sealed{_region_name(member.name, 'sealed')}"
			             f"({_codec_args(member.codec, member.args)})"
			             f"{_attrs_to_source(member.attrs)} {{")
			lines.extend(member_lines(member.members, depth + 1))
			lines.append(f"{indent}}}")
		elif isinstance(member, ast.Authenticated):
			lines.append(f"{indent}authenticated"
			             f"{_region_name(member.name, 'authenticated')}"
			             f"{_attrs_to_source(member.attrs)} {{")
			lines.extend(member_lines(member.members, depth + 1))
			lines.append(f"{indent}}}")
		elif isinstance(member, ast.Variant):
			lines.extend(_variant_lines(member, depth))
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

	if isinstance(member, ast.Opaque):
		return (f"opaque {member.name} [{expr_to_source(member.size)}]"
		        f"{_attrs_to_source(member.attrs)};")

	if isinstance(member, ast.Tlv):
		return f"tlv {member.name} ({_args_to_source(member.args)});"

	if isinstance(member, ast.Reserved):
		return (f"reserved {member.type_ref.name}{_array_to_source(member.array)}"
		        f"{_attrs_to_source(member.attrs)};")

	if isinstance(member, ast.TagField):
		return (f"{member.kind.value} {member.type_ref.name}"
		        f"{_region_name(member.name, member.kind.value)}"
		        f"{_array_to_source(member.array)}{_covers_to_source(member.covers)}"
		        f"{_attrs_to_source(member.attrs)};")

	raise TypeError(f"cannot unparse {type(member).__name__}")


def _region_name(name: str, default: str) -> str:
	"""A region's name, omitted where it is the one the parser would infer."""
	return "" if name == default else f" {name}"


def _covers_to_source(covers: tuple[str, ...]) -> str:
	return f" covers({', '.join(covers)})" if covers else ""


def _codec_args(codec: str, args: tuple[ast.Attr, ...]) -> str:
	rendered = _args_to_source(args)
	return f"{codec}, {rendered}" if rendered else codec


def _variant_lines(variant: ast.Variant, depth: int) -> list[str]:
	indent = "\t" * depth
	lines  = [f"{indent}variant {variant.name} switch "
	          f"({expr_to_source(variant.discriminant)})"
	          f"{_attrs_to_source(variant.attrs)} {{"]

	for arm in variant.arms:
		label = ("default" if arm.value is None
		         else f"case {expr_to_source(arm.value)}")
		if arm.is_error:
			lines.append(f"{indent}\t{label}: error;")
		elif arm.is_opaque:
			lines.append(f"{indent}\t{label}: opaque;")
		else:
			assert arm.member is not None
			lines.append(f"{indent}\t{label}: {member_to_source(arm.member)}")

	lines.append(f"{indent}}}")
	return lines


def _args_to_source(args: tuple[ast.Attr, ...]) -> str:
	rendered = []
	for arg in args:
		if arg.raw is not None:
			rendered.append(f"{arg.name} = {arg.raw}")
		elif arg.value is None:
			rendered.append(arg.name)
		else:
			rendered.append(f"{arg.name} = {expr_to_source(arg.value)}")
	return ", ".join(rendered)


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
