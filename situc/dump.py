"""Render an AST as a stable structural dump.

This is what `situc dump-ast` prints. Spans are deliberately excluded: the dump
has to be comparable between a schema and the same schema reparsed from
unparsed source, which is how the phase 1 round-trip property is tested. It is
also the debugging aid called for in project.md section 21, so it is line-based
and diffable rather than nested punctuation.
"""

from __future__ import annotations

from situc import ast
from situc.unparse import expr_to_source


def dump(schema: ast.Schema) -> str:
	lines = ["schema"]
	for decl in schema.decls:
		lines.extend(_decl(decl, depth=1))
	return "\n".join(lines) + "\n"


def _indent(depth: int, text: str) -> str:
	return "\t" * depth + text


def _decl(decl: ast.Decl, depth: int) -> list[str]:
	if isinstance(decl, ast.TargetDirective):
		return [_indent(depth, f"target {decl.kind.value}")]

	if isinstance(decl, ast.EndianDirective):
		return [_indent(depth, f"endian {decl.endian.value}")]

	if isinstance(decl, ast.BitOrderDirective):
		return [_indent(depth, f"bit_order {decl.bit_order.value}")]

	if isinstance(decl, ast.ImportDirective):
		return [_indent(depth, f"import {decl.path!r}")]

	if isinstance(decl, ast.StrictnessDirective):
		return [_indent(depth, f"strictness {decl.strictness.value}")]

	if isinstance(decl, ast.ConstDecl):
		return [_indent(depth, f"const {decl.name} = {expr_to_source(decl.value)}")]

	if isinstance(decl, ast.EnumDecl):
		return _enum(decl, depth)

	if isinstance(decl, ast.StructDecl):
		return _struct(decl, depth)

	if isinstance(decl, ast.VarintDecl):
		detail = f"encoding={decl.encoding.value} max_bits={decl.max_bits}"
		if decl.transform is not None:
			detail += f" transform={decl.transform.value}"
		detail += f" minimal={'yes' if decl.minimal else 'no'}"
		return [_indent(depth, f"varint_type {decl.name} {detail}")]

	if isinstance(decl, ast.EndianMarkerDecl):
		return [
			_indent(depth, f"endian_marker {decl.name} : {decl.backing.name}"),
			_indent(depth + 1, f"little = {expr_to_source(decl.little)}"),
			_indent(depth + 1, f"big = {expr_to_source(decl.big)}"),
		]

	if isinstance(decl, ast.CodecDecl):
		return _codec(decl, depth)

	if isinstance(decl, ast.ImplDecl):
		symbol = f" {decl.symbol!r}" if decl.symbol is not None else ""
		return [_indent(depth, f"impl {decl.codec} {decl.kind.value}{symbol}")]

	if isinstance(decl, ast.Requirement):
		return [_indent(depth, f"{decl.kind.value} {expr_to_source(decl.expr)}")]

	# An invariant is a declaration like any other and this dumper did not
	# know it, so `situc dump-ast` -- a debugging aid, and phase 1's own
	# deliverable -- died with a Python traceback on any schema carrying one.
	# `test/schema/edges.situ` has carried one since invariants landed, and
	# nothing ran the subcommand over it (26.35).
	if isinstance(decl, ast.Invariant):
		return [_indent(depth,
		                f"invariant {decl.derived} == {expr_to_source(decl.expr)}")]

	if isinstance(decl, ast.Relation):
		params = ", ".join(f"{param.name}: {param.type_name}"
		                   for param in decl.params)
		return [_indent(depth, f"relation {decl.name}({params})"),
		        *_attrs(decl.attrs, depth + 1),
		        *[_indent(depth + 1, f"must {expr_to_source(must.expr)}")
		          for must in decl.body]]

	# Every kind the parser can produce has a case above. This is not one, so
	# it is a construct that arrived without its dump -- which is a compiler
	# bug rather than a schema error, and says so.
	raise TypeError(f"cannot dump {type(decl).__name__}: every declaration the "
	                "parser produces needs a case in situc/dump.py")


def _codec(decl: ast.CodecDecl, depth: int) -> list[str]:
	"""Every property, including the defaulted ones.

	The signature is the entire interface between an algorithm and the lattice
	(section 13.1), so the dump shows all of it: a property that is absent
	because it defaulted is exactly as load-bearing as one that was written.
	"""
	from situc.unparse import codec_expansion, codec_granularity

	flags = [flag for flag in ("systematic", "authenticated", "invertible",
	                           "deterministic", "error_propagating")
	         if getattr(decl, flag)]

	return [
		_indent(depth, f"codec {decl.name}"),
		_indent(depth + 1, f"expansion = {codec_expansion(decl)}"),
		_indent(depth + 1, f"granularity = {codec_granularity(decl)}"),
		_indent(depth + 1, f"seekable = {decl.seekable.value}"),
		_indent(depth + 1, f"flags = {', '.join(flags) or 'none'}"),
	]


def _enum(decl: ast.EnumDecl, depth: int) -> list[str]:
	# The effective default is shown rather than the written one, because
	# "unknown values are rejected" is the fact a reader needs and it holds
	# whether or not the schema spelled it out (project.md section 8.7).
	lines = [_indent(depth, f"enum {decl.name} : {decl.backing.name} "
	                        f"default={decl.effective_default.value}")]
	for member in decl.members:
		lines.append(_indent(depth + 1, f"{member.name} = {expr_to_source(member.value)}"))
	return lines


def _struct(decl: ast.StructDecl, depth: int) -> list[str]:
	lines = [_indent(depth, f"struct {decl.name}")]
	lines.extend(_attrs(decl.attrs, depth + 1))
	lines.extend(_members(decl.members, depth + 1))
	return lines


def _members(members: tuple[ast.Member, ...], depth: int) -> list[str]:
	lines: list[str] = []
	for member in members:
		lines.extend(_member(member, depth))
	return lines


def _member(member: ast.Member, depth: int) -> list[str]:
	if isinstance(member, ast.PositionalBlock):
		lines = [_indent(depth, "positional")]
		lines.extend(_members(member.members, depth + 1))
		return lines

	if isinstance(member, ast.Field):
		head  = f"field {member.name} : {member.type_ref.name}{_array(member.array)}"
		lines = [_indent(depth, head)]
		if member.pin is not None:
			lines.append(_indent(depth + 1, f"pin {expr_to_source(member.pin)}"))
		lines.extend(_attrs(member.attrs, depth + 1))
		return lines

	if isinstance(member, ast.MarkerField):
		lines = [_indent(depth, f"endian_marker {member.name}")]
		lines.extend(_attrs(member.attrs, depth + 1))
		return lines

	if isinstance(member, ast.Opaque):
		return [_indent(depth, f"opaque {member.name} [{expr_to_source(member.size)}]")]

	if isinstance(member, ast.Tlv):
		lines = [_indent(depth, f"tlv {member.name} unknown={member.unknown.value} "
		                        f"duplicates={member.duplicates.value} "
		                        f"ordered={'yes' if member.ordered else 'no'}")]
		lines.extend(_tlv_grammar(member, depth + 1))
		return lines

	if isinstance(member, ast.Indexed):
		described = f"indexed {member.name} base={member.base.value}"
		if member.base_member is not None:
			described += f"({member.base_member})"
		lines = [_indent(depth, described)]
		lines.extend(_members(member.members, depth + 1))
		return lines

	if isinstance(member, ast.Variant):
		lines = [_indent(depth,
		                 f"variant {member.name} switch "
		                 f"{expr_to_source(member.discriminant)}")]
		for arm in member.arms:
			label = ("default" if arm.value is None
		         else f"case {expr_to_source(arm.value)}")
			if arm.member is None:
				policy = "error" if arm.is_error else "opaque"
				lines.append(_indent(depth + 1, f"{label} -> {policy}"))
			else:
				lines.append(_indent(depth + 1, label))
				lines.extend(_member(arm.member, depth + 2))
		return lines

	if isinstance(member, ast.Reserved):
		head  = f"reserved {member.type_ref.name}{_array(member.array)}"
		lines = [_indent(depth, head)]
		lines.extend(_attrs(member.attrs, depth + 1))
		return lines

	if isinstance(member, ast.Coded):
		lines = [_indent(depth, f"coded {member.name} codec={member.codec}")]
		lines.extend(_attrs(member.attrs, depth + 1))
		lines.extend(_members(member.members, depth + 1))
		return lines

	if isinstance(member, ast.Sealed):
		lines = [_indent(depth, f"sealed{_named(member.name, 'sealed')} "
		                        f"codec={member.codec}")]
		lines.extend(_attrs(member.attrs, depth + 1))
		lines.extend(_members(member.members, depth + 1))
		return lines

	if isinstance(member, ast.Authenticated):
		lines = [_indent(depth, f"authenticated{_named(member.name, 'authenticated')}")]
		lines.extend(_attrs(member.attrs, depth + 1))
		lines.extend(_members(member.members, depth + 1))
		return lines

	if isinstance(member, ast.TagField):
		# Inferred coverage is shown as what it infers rather than as absence:
		# the dump is read to answer "which bytes does this cover", and
		# "covers=" would leave that question with the reader.
		covers = ", ".join(member.covers) if member.covers else "<inferred>"
		head   = (f"{member.kind.value}{_named(member.name, member.kind.value)}"
		          f" : {member.type_ref.name}"
		          f"{_array(member.array)} covers={covers}")
		lines  = [_indent(depth, head)]
		lines.extend(_attrs(member.attrs, depth + 1))
		return lines

	raise TypeError(f"cannot dump {type(member).__name__}")


def _named(name: str, default: str) -> str:
	"""A region's name, left out where it is the one the parser infers."""
	return "" if name == default else f" {name}"


def _array(array: ast.ArraySpec | None) -> str:
	if array is None:
		return ""
	return f"[{expr_to_source(array.size)}]" if array.size is not None else "[]"


def _tlv_grammar(region: ast.Tlv, depth: int) -> list[str]:
	"""The item grammar: how a tag decodes, how a value is sized, what is named.

	Printed because it is now structure rather than the source text of three
	arguments. A reader asking what situ made of a `switch (wire)` gets an
	answer here instead of having to read the schema back.
	"""
	lines = [_indent(depth, f"tag part {part.name} = {expr_to_source(part.value)}")
	         for part in region.tag_decode]

	if region.value_size is not None:
		lines.append(_indent(depth, f"value_size switch {region.value_size.selector}"))
		for case in region.value_size.cases:
			label = "default" if case.label is None else f"case {case.label}"
			lines.append(_indent(depth + 1, f"{label}: {_value_rule(case.rule)}"))

	for tag in region.known:
		described = f"known {tag.tag} {tag.name}"
		if tag.wire is not None:
			described += f" wire={tag.wire}"
		if tag.type_name is not None:
			described += f" type={tag.type_name}{'[]' if tag.repeated else ''}"
		lines.append(_indent(depth, described))

	return lines


def _value_rule(rule: ast.ValueRule) -> str:
	if isinstance(rule, ast.FixedValue):
		return f"{rule.size} bytes"
	if isinstance(rule, ast.PrefixedValue):
		return f"prefixed({rule.length_type})"
	if isinstance(rule, ast.SelfDelimiting):
		return "self_delimiting"
	return "error"


def _attrs(attrs: tuple[ast.Attr, ...], depth: int) -> list[str]:
	lines = []
	for attr in attrs:
		rendered = (attr.name if attr.value is None
		            else f"{attr.name} = {expr_to_source(attr.value)}")
		lines.append(_indent(depth, f"attr {rendered}"))
	return lines
