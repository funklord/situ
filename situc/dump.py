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

	if isinstance(decl, ast.ConstDecl):
		return [_indent(depth, f"const {decl.name} = {expr_to_source(decl.value)}")]

	if isinstance(decl, ast.EnumDecl):
		return _enum(decl, depth)

	if isinstance(decl, ast.StructDecl):
		return _struct(decl, depth)

	if isinstance(decl, ast.Requirement):
		return [_indent(depth, f"{decl.kind.value} {expr_to_source(decl.expr)}")]

	raise TypeError(f"cannot dump {type(decl).__name__}")


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

	if isinstance(member, ast.Reserved):
		head  = f"reserved {member.type_ref.name}{_array(member.array)}"
		lines = [_indent(depth, head)]
		lines.extend(_attrs(member.attrs, depth + 1))
		return lines

	raise TypeError(f"cannot dump {type(member).__name__}")


def _array(array: ast.ArraySpec | None) -> str:
	if array is None:
		return ""
	return f"[{expr_to_source(array.size)}]" if array.size is not None else "[]"


def _attrs(attrs: tuple[ast.Attr, ...], depth: int) -> list[str]:
	lines = []
	for attr in attrs:
		rendered = (attr.name if attr.value is None
		            else f"{attr.name} = {expr_to_source(attr.value)}")
		lines.append(_indent(depth, f"attr {rendered}"))
	return lines
