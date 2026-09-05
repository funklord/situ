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
from situc.types import pinned_shown

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
		# `append` is part of the directive, not decoration: it says the
		# message may grow at its end, which is what lets a coverage run to
		# EOF. Dropped here, and nothing noticed because PNG is the tree's
		# first `target file append` -- an unparser that loses a word can
		# only be caught by a schema that uses it (26.244).
		grows = " append" if decl.append else ""
		return [f"target {decl.kind.value}{grows};"]

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
		if decl.declared_max_bytes is not None:
			lines.append(f"\tmax_bytes = {decl.declared_max_bytes};")
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

	if isinstance(decl, ast.Invariant):
		return [f"invariant {decl.derived} == {expr_to_source(decl.expr)};"]

	if isinstance(decl, ast.Relation):
		params = ", ".join(f"{param.name}: {param.type_name}"
		                   for param in decl.params)
		return [f"relation {decl.name}({params})"
		        f"{_attrs_to_source(decl.attrs)} {{",
		        *[f"\tmust {expr_to_source(must.expr)};" for must in decl.body],
		        "}"]

	raise TypeError(f"cannot unparse {type(decl).__name__}")


def _codec_lines(decl: ast.CodecDecl) -> list[str]:
	"""A property signature, rendered as the section 13.2 vocabulary.

	Every property is written out, including the ones that were defaulted. A
	signature is the entire interface to the lattice, so a round-tripped one
	that dropped a silent default would say something different from what it
	came from.
	"""
	if decl.pipeline:
		# `codec framed = crc32 |> interleave_16 |> manchester_802_3;`. A
		# pipeline declares no properties at all -- they compose from the
		# stages (13.4) -- so rendering one as a property body states the
		# composed answer as though it had been written, and drops the stages
		# that produced it. `framed` came back with `expansion = +0` and no
		# stages, which is a different codec.
		return [f"codec {decl.name} = {' |> '.join(decl.pipeline)};"]

	lines = [f"codec {decl.name} {{"]
	# Before the properties, because that is where the schemas write them and
	# because they are the primitive's sizes rather than the lattice's
	# properties: `expansion` says nothing about the tag beside the ciphertext
	# (0038).
	if decl.tag_bytes is not None:
		lines.append(f"\ttag_bytes = {decl.tag_bytes};")
	if decl.nonce_bytes is not None:
		lines.append(f"\tnonce_bytes = {decl.nonce_bytes};")
	if decl.kernel is not None:
		lines.append(f"\tkernel = {_kernel_to_source(decl.kernel)};")

	lines += [f"\t{codec_expansion(decl)};",
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


def _kernel_to_source(kernel: ast.Kernel) -> str:
	"""`stuffing(worst_case = 2, per = 1, unit = byte, code = slip)`.

	The family and its arguments, which are what name the code generated for
	a codec rather than merely its signature. Dropping them left 38 of
	`std/kernels.situ`'s codecs round-tripping into signatures with no kernel
	-- declarations that say what a transform costs and no longer say what it
	is.
	"""
	return f"{kernel.family.value}({_args_to_source(kernel.args)})"


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
	# The width belongs to the backing type, because that is what it
	# describes: how wide one value of this enum is (0052). Dropping it
	# round-trips a byte-run enum into a scalar one whose arms are strings.
	width = "" if decl.width is None else f"[{decl.width}]"
	lines = [f"enum {decl.name} : {decl.backing.name}{width} {{"]
	for member in decl.members:
		lines.append(f"\t{member.name} = {expr_to_source(member.value)},")
	if decl.default is not None:
		lines.append(f"\tdefault = {decl.default.value},")
	lines.append("}")
	return lines


def _struct_lines(decl: ast.StructDecl) -> list[str]:
	if decl.register is not None:
		return _register_lines(decl, decl.register)

	header = f"struct {decl.name}{_attrs_to_source(decl.attrs)} {{"
	lines  = [header]
	lines.extend(member_lines(decl.members, depth=1))
	lines.append("}")
	return lines


def _register_lines(decl: ast.StructDecl, register: ast.RegisterInfo) -> list[str]:
	"""A register renders as one, not as the struct it lowered to.

	A `register_block` does not come back: it declares scoped defaults and
	nothing else, so its registers reparse identically with the defaults
	written out on each. The round-trip property is over the tree.
	"""
	where = "" if register.address is None else f" @ 0x{register.address:02X}"
	lines = [f"register {decl.name}{where} {{",
	         f"	width        = {register.width};",
	         f"	access_width = {register.access_width};"]

	if register.volatile:
		lines.append("	volatile;")
	if register.no_rmw:
		lines.append("	no_rmw;")

	lines.append("")
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
			             f"{_until_to_source(member.until)}"
			             f"{_covers_to_source(member.covers)}"
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
		parts = [_radix_to_source(getattr(member, "radix", None)),
		         member.type_ref.name, " ", member.name,
		         _array_to_source(member.array),
		         _until_to_source(getattr(member, "until", None)),
		         _while_to_source(getattr(member, "repeat", None))]
		if member.located is not None:
			parts.append(f" at {expr_to_source(member.located)}")
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
		# A preamble is a reserved run whose content is stated rather than
		# governed by a policy (0052), so it prints as what it was written
		# as. Printing it as `reserved` would round-trip to a run checked
		# for zeros -- the schema saying one thing and the tree another.
		if member.pinned is not None:
			return (f"preamble {member.type_ref.name}"
			        f"{_array_to_source(member.array)} = "
			        f'"{pinned_shown(member.pinned)}"'
			        f"{_attrs_to_source(member.attrs)};")
		return (f"reserved {member.type_ref.name}{_array_to_source(member.array)}"
		        f"{_attrs_to_source(member.attrs)};")

	if isinstance(member, ast.TagField):
		prefix = (f" prefix({member.prefix})"
		          if member.prefix is not None else "")
		# After `prefix`, which is where the parser reads it: the codec runs
		# over the prefix as well as this message's bytes (0053). Dropped
		# here at first, and the round trip caught it -- the same way it
		# caught `target file append`, and for the same reason: a clause no
		# schema used was a clause nothing round-tripped.
		codec = f" is {member.codec}" if member.codec else ""
		return (f"{member.kind.value} {member.type_ref.name}"
		        f"{_region_name(member.name, member.kind.value)}"
		        f"{_array_to_source(member.array)}{_covers_to_source(member.covers)}"
		        f"{prefix}{codec}{_attrs_to_source(member.attrs)};")

	if isinstance(member, ast.Pad):
		return f"pad_to({member.to}){_attrs_to_source(member.attrs)};"

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


def expr_to_source(expr: ast.Expr, *, explicit: bool = False) -> str:
	"""An expression as situ source.

	`explicit` parenthesises every operator, and exists because this text is
	not only read back by situ. `layout` carries a `while` predicate and a
	computed size as *source*, and each backend rewrites the leaf names and
	hands the rest to a host compiler -- which reparses it under its own
	precedence table.

	Situ's table is C's. Python's and Rust's are not: they bind `&`, `^` and
	`|` tighter than the comparisons, where situ and C bind them looser. So
	`kind & 3 == 2` is `kind & (3 == 2)` here and `(kind & 3) == 2` there --
	one schema, two meanings, in the four backends whose agreement is the
	whole claim. Python adds a second: it *chains* comparisons, so `a > b < c`
	is `(a > b) and (b < c)` and not `(a > b) < c`.

	Measured rather than reasoned: comparing situ's grouping against Python's
	reading of the same flat text finds 39 operator pairs that disagree. No
	committed schema writes one -- which is why nothing had noticed, and is
	26.37's observation about this language's operators over again.

	Every operator, rather than the pairs that are known to differ. A rule
	that parenthesises only where two named tables disagree has to be right
	about four languages and stay right; parentheses everywhere are correct
	in any language that has them, and the cost is a few characters in
	generated code nobody edits.
	"""
	return _expr(expr, parent_binding=0, explicit=explicit, wrap=False)


def _expr(expr: ast.Expr, parent_binding: int, explicit: bool = False,
		wrap: bool = True) -> str:
	if isinstance(expr, ast.IntLiteral):
		return expr.text

	if isinstance(expr, ast.StringLiteral):
		return f'"{_escape(expr.value)}"'

	if isinstance(expr, ast.NameRef):
		return expr.name

	if isinstance(expr, ast.Remaining):
		return "remaining"

	if isinstance(expr, ast.Access):
		return f"{_expr(expr.base, _ATOM_BINDING, explicit)}.{expr.name}"

	if isinstance(expr, ast.Index):
		inner = ("" if expr.index is None
		         else expr_to_source(expr.index, explicit=explicit))
		return f"{_expr(expr.base, _ATOM_BINDING, explicit)}[{inner}]"

	if isinstance(expr, ast.Call):
		args = ", ".join(expr_to_source(arg, explicit=explicit)
		                 for arg in expr.args)
		return f"{expr.name}({args})"

	if isinstance(expr, ast.Unary):
		rendered = f"{expr.op}{_expr(expr.operand, _UNARY_BINDING, explicit)}"
		if explicit:
			return f"({rendered})" if wrap else rendered
		return _wrap(rendered, _UNARY_BINDING, parent_binding)

	if isinstance(expr, ast.Binary):
		binding  = _BINDING[expr.op]
		left     = _expr(expr.left, binding, explicit)
		# The right operand of a left-associative operator needs a parenthesis
		# at equal binding, or `a - (b - c)` would reparse as `(a - b) - c`.
		right    = _expr(expr.right, binding + 1, explicit)
		rendered = f"{left} {expr.op} {right}"
		if explicit:
			# Not the outermost pair. Every *nested* operator is grouped, so
			# no host's precedence can regroup it -- and the outer one is
			# redundant by construction, because whatever encloses the whole
			# expression already delimits it. Rust says so: `-D unused-parens`
			# rejects `min(((a / 2) + 1))` and the suite compiles with
			# `-D warnings`.
			return f"({rendered})" if wrap else rendered
		return _wrap(rendered, binding, parent_binding)

	raise TypeError(f"cannot unparse {type(expr).__name__}")


def _wrap(rendered: str, binding: int, parent_binding: int) -> str:
	return f"({rendered})" if binding < parent_binding else rendered


#: `decimal`/`hex`, by the radix the parser recorded (8.6.2).
RADIX_KEYWORDS = {10: "decimal", 16: "hex"}


def _radix_to_source(radix: int | None) -> str:
	return f"{RADIX_KEYWORDS[radix]} " if radix in RADIX_KEYWORDS else ""


def _until_to_source(until: "ast.Until | None") -> str:
	"""`until "\\r\\n"`, and the cap where one was written.

	Absent entirely until an attribute-placement check found it: a delimited
	`decimal` field reprinted as a plain binary scalar, which parses, means
	something else, and says so nowhere. `quoted` and `escape` are not here
	because the grammar carries them as attributes, which are printed already.
	"""
	if until is None:
		return ""

	body = _escape(until.delimiter.decode("latin-1"))
	cap  = f" max {expr_to_source(until.cap)}" if until.cap is not None else ""
	return f' until "{body}"{cap}'


def _while_to_source(repeat: "ast.While | None") -> str:
	"""`while (cond)`, and the cap where one was written.

	Parenthesised for the reason the parser gives: the parentheses are what
	stop a following `max` reading as part of the condition. Absent until the
	round trip was compared over the tree rather than over the dump -- a
	`while` run reprinted as a bare array is a member the frame ends instead
	of the data, which parses and means something else.
	"""
	if repeat is None:
		return ""

	cap = f" max {expr_to_source(repeat.cap)}" if repeat.cap is not None else ""
	return f" while ({expr_to_source(repeat.predicate)}){cap}"


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
		elif " " <= char <= "~":
			out.append(char)
		else:
			# Everything else is a byte with no printable spelling -- a SLIP
			# delimiter, a control character in a magic.
			#
			# The lexer would accept it raw: non-ASCII is refused outside a
			# string literal and permitted inside one. What refuses it is the
			# rule a level up, that situ source is ASCII, and the practical
			# consequence that several tools here read and write `.situ` with
			# the ascii codec and would fail on the byte rather than on
			# anything a reader could act on.
			out.append(f"\\x{ord(char):02X}")
	return "".join(out)
