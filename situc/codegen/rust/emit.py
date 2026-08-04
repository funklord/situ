"""The Rust backend (section 26.18).

Ordered last of the four because adoption is furthest out, not because it
matters least: Rust expresses the capability system more naturally than any
other target situ has, and most of that costs nothing to emit because the
language already does it.

  * **Invalidation (12.3) is the borrow checker.** A view holds a slice. A
    write through `&mut` while a read view is outstanding does not compile, so
    the generation counter the C runtime carries is not needed here -- the
    check happens before the program runs.
  * **An error cannot be dropped**, because `Result` is `#[must_use]`.
  * **A gate cannot be constructed.** Its field is private to the module, so
    nothing outside can make one and the only thing that does is the open that
    checks the tag.

Two types per struct rather than one generic over mutability: `Foo` reads and
`FooMut` reads and writes. That is what the ecosystem does, it keeps the
generated code obvious, and a reader who wants only to parse never has to hold
a `&mut` they do not need.
"""

from __future__ import annotations

import re

from dataclasses import dataclass, field

from situc import ast
from situc.capability import Axis
from situc.codegen.c.names import c_name
from situc.diagnostics import Diagnostic
from situc.expr import evaluate
from situc.layout import (
	BITS_PER_BYTE, Arm, IndexTable, Placement, TlvGrammar, ValueRule,
)
from situc.names import (
	UnknownName, expand_calls, over_fields, render_delimiter, rust_spelling,
)
from situc.propagate import Resolved
from situc.invariant import derived as derived_by
from situc.invariant import expression as invariant_expression
from situc.resolve import ResolvedSchema, ResolvedStruct
from situc.traverse import (
	Check, Member, arm_members, arm_of, covered_run, data_sized, decode_bound,
	dynamic_frame_owner, offset_plan,
	readable_names,
	region_extent,
	decode_counts_bits,
	decodes_here, classify, classify_check, declares_its_own_length,
	extent_parts, frameable,
	extern_symbol, has_computable_extent, index_entry_bytes, indexed_elements,
	is_run,
	local_name,
	element_bytes, is_counted_run, matched_values, obligation,
	preceding_parts,
	obligations, own_entries, own_members,
)
from situc.types import ScalarType, lookup
from situc.unparse import expr_to_source as unparse_expr

WORD_WIDTHS = (8, 16, 32, 64)


@dataclass
class Generated:
	"""One module per schema."""

	module: str
	basename: str
	warnings: list[Diagnostic] = field(default_factory=list)

	def files(self) -> dict[str, str]:
		return {f"{self.basename}.rs": self.module}


def generate(schema: ast.Schema, resolved: ResolvedSchema, basename: str,
		prefix: str = "situ", materialize: bool = False) -> Generated:
	return Generated(module=Emitter(schema, resolved, basename,
	                                materialize).module(),
	                 basename=basename)


#: Rust's reserved words. A schema is free to name a field `type` or `match`;
#: Rust is not. Raw identifiers take all of these except the four below, which
#: cannot be raw and have to be renamed instead.
KEYWORDS = frozenset({
	"as", "break", "const", "continue", "dyn", "else", "enum", "extern",
	"false", "fn", "for", "if", "impl", "in", "let", "loop", "match", "mod",
	"move", "mut", "pub", "ref", "return", "static", "struct", "trait", "true",
	"type", "unsafe", "use", "where", "while", "async", "await", "abstract",
	"become", "box", "do", "final", "macro", "override", "priv", "try",
	"typeof", "unsized", "virtual", "yield",
})

#: The ones `r#` cannot rescue.
UNESCAPABLE = frozenset({"crate", "self", "Self", "super"})


def _self_as(attrs: tuple[ast.Attr, ...]) -> int | None:
	"""What a self-covering tag's own bytes read as, or None (14.2)."""
	for attr in attrs:
		if attr.name == "self_as" and isinstance(attr.value, ast.IntLiteral):
			return int(attr.value.value)
	return None


def _reader(endian: ast.Endian | None) -> str:
	"""Which runtime read a scalar goes through.

	Three answers rather than two, and this backend gave two -- in two
	different ways. A field asked `endian is not LITTLE` and so read `native`
	big-endian; an indexed table's entry asked `endian is BIG` and so read the
	same schema's `native` little-endian. One backend disagreeing with itself
	about what a schema means is the shape invariant 21 is about, one level
	below "which of two errors fires".
	"""
	if endian is ast.Endian.NATIVE:
		return "read_ne"
	return "read_le" if endian is ast.Endian.LITTLE else "read_be"


def _writer(endian: ast.Endian | None) -> str:
	if endian is ast.Endian.NATIVE:
		return "write_ne"
	return "write_le" if endian is ast.Endian.LITTLE else "write_be"


def _ident(name: str) -> str:
	"""A schema name as a Rust identifier.

	Raw identifiers carry the keywords, which is the whole reason they exist:
	`r#type` is a field called `type`, and decision 0013 says the casing and
	the naming are the author's rather than the backend's to negotiate.
	"""
	safe = c_name(name)
	if safe in UNESCAPABLE:
		return f"{safe}_"
	return f"r#{safe}" if safe in KEYWORDS else safe


def _pascal(name: str) -> str:
	"""Rust type names are PascalCase, and a schema's are the author's.

	Decision 0013 says casing is the author's and collisions are the
	compiler's; this converts rather than demanding, so a snake_case schema
	does not produce a module full of warnings nobody asked for.
	"""
	return "".join(part.capitalize() or "_" for part in c_name(name).split("_"))


class Emitter:
	def __init__(self, schema: ast.Schema, resolved: ResolvedSchema,
			basename: str, materialize: bool = False) -> None:
		self.schema   = schema
		self.resolved = resolved
		self.basename = basename
		self.enums    = {decl.name: decl for decl in schema.enums()}
		self.codecs   = {decl.name: decl for decl in schema.codecs()}
		self.markers  = {decl.name: decl for decl in schema.markers()}
		self.structs  = set(resolved.structs)
		#: Emit the second accessor family (decision 0022): the consumer's
		#: choice rather than the schema's, and off unless asked for.
		self.materialize = materialize

	def module(self) -> str:
		body: list[str] = list(self._codec_externs())
		for decl in self.schema.enums():
			body.extend(self._enum(decl))
		for name in sorted(self.structs):
			body.extend(self._struct(self.resolved.structs[name]))

		# The body first, so the imports can follow what it uses. An unused
		# import is a warning, and a generated file that warns on sight
		# teaches a reader to ignore warnings from generated files -- which
		# was the reasoning for guarding `Dirty` and stopped there. A schema
		# of nothing but registers uses none of the four, and the module did
		# not build under `-D warnings`.
		# Comments stripped first: `Dirty` appears in a doc comment on every
		# struct that explains what a dirty bit is, so a bare substring
		# search imported it for schemas that never touch one -- and
		# `unused_imports` is an error under `-D warnings`.
		text = "\n".join(line for line in body
		                 if not line.lstrip().startswith(("//", "///", "//!")))
		used = [name for name, token in (
			("self", "situ_rt::"),
			("Dirty", "Dirty"),
			("Error", "Error::"),
			("Result", "Result<"),
		) if token in text]

		lines = [
			f"//! Generated by situc from {self.basename}.situ -- do not edit.",
			"//!",
			"//! The operations below are the ones this schema's capability",
			"//! vectors support. Where one is missing, a comment says why.",
			"",
			"#![allow(dead_code)]",
			"",
		]
		if used:
			lines.append("use crate::situ_rt::{" + ", ".join(used) + "};")
			lines.append("")

		return "\n".join([*lines, *body]) + "\n"

	# -- enums ---------------------------------------------------------

	def _needs_dirty(self) -> bool:
		"""Whether anything in this schema marks or clears a dirty bit."""
		return any(entry.placement.covered_by or entry.placement.derived_by
		           for struct in self.resolved.structs.values()
		           for entry in struct.entries)

	def _enum(self, decl: ast.EnumDecl) -> list[str]:
		values  = self.resolved.layout.env.enums[decl.name]
		backing = decl.backing.scalar
		assert backing is not None

		name = _pascal(decl.name)
		lines = [
			f"/// enum {decl.name} : {decl.backing.name} --"
			f" unknown values are {decl.effective_default.value}.",
			"#[derive(Debug, Clone, Copy, PartialEq, Eq)]",
			f"#[repr({self._rust_type(backing)})]",
			f"pub enum {name} {{",
		]
		lines.extend(f"\t{_pascal(member.name)} = {values[member.name]},"
		             for member in decl.members)
		lines.extend([
			"}",
			"",
			f"impl {name} {{",
			"\t/// Whether a value names a member (section 8.7).",
			f"\tpub fn is_known(raw: {self._rust_type(backing)}) -> bool {{",
			"\t\tmatches!(raw, "
			+ " | ".join(str(values[member.name]) for member in decl.members)
			+ ")",
			"\t}",
			"}",
			"",
		])
		return lines

	# -- structs -------------------------------------------------------

	def _struct(self, struct: ResolvedStruct) -> list[str]:
		layout = struct.layout

		if layout.register is not None:
			return self._register(struct)

		# The index types first, at module scope: Rust has no struct
		# declaration inside an `impl`, so they cannot live beside the methods
		# that use them.
		types: list[str] = []
		for held in own_members(struct):
			if held.repeat_while is not None or held.delimiter is not None:
				types.extend(self._run_index_type(struct, held))
			if held.kind == "tlv" and held.tlv_grammar is not None \
					and held.tlv_grammar.walkable:
				types.extend(self._tlv_item_type(struct, held,
				                                 held.tlv_grammar))

		name  = _pascal(struct.name)
		fixed = layout.is_fixed_size
		lines = [
			f"/// struct {struct.name}: {layout.size_bytes} bytes, fixed.",
			"///",
			"/// The lifetime is the buffer's. Section 12.3's invalidation rule",
			"/// is the borrow checker here: a write through `{name}Mut` while",
			"/// this is outstanding does not compile.",
			"pub struct " + name + "<'a> {",
			"\tbytes: &'a [u8],",
			"}",
			"",
			f"pub struct {name}Mut<'a> {{",
			"\tbytes: &'a mut [u8],",
			"}",
			"",
			f"impl<'a> {name}<'a> {{",
			(f"\tpub const SIZE: usize = {layout.size_bytes};" if fixed
			 else f"\tpub const SIZE_MIN: usize = {layout.size_bytes};"),
			"",
			"\t/// The one bounds check. Everything below trusts it.",
			"\t///",
			("\t/// A slice carries its own length, so a variable-length struct"
			 if not fixed else "\t/// The extent is the struct's own."),
			("\t/// needs no second parameter saying where the frame ends."
			 if not fixed else "\t///"),
			f"\tpub fn new(bytes: &'a [u8]) -> Result<Self> {{",
			f"\t\tif bytes.len() < Self::{'SIZE' if fixed else 'SIZE_MIN'} {{",
			"\t\t\treturn Err(Error::Bounds);",
			"\t\t}",
			"\t\tOk(Self { bytes })",
			"\t}",
		]

		for entry in own_entries(struct):
			lines.extend(self._getter(struct, entry))

		lines.extend(self._region_runs(struct))
		lines.extend(self._nested_text_values(struct))
		lines.extend(self._arm_accessors(struct))
		lines.extend(self._extent_method(struct))
		lines.extend(self._required(struct))
		lines.extend(self._validate(struct))
		lines.extend(self._gate_opens(struct))
		lines.extend(["}", ""])
		lines.extend(self._gates(struct))
		lines.extend(self._offsets(struct))

		lines.extend([
			f"impl<'a> {name}Mut<'a> {{",
			(f"\tpub const SIZE: usize = {layout.size_bytes};" if fixed
			 else f"\tpub const SIZE_MIN: usize = {layout.size_bytes};"),
			"",
			f"\tpub fn new(bytes: &'a mut [u8]) -> Result<Self> {{",
			f"\t\tif bytes.len() < Self::{'SIZE' if fixed else 'SIZE_MIN'} {{",
			"\t\t\treturn Err(Error::Bounds);",
			"\t\t}",
			"\t\tOk(Self { bytes })",
			"\t}",
			"",
			"\t/// A read-only view of the same bytes.",
			f"\tpub fn as_ref(&self) -> {name}<'_> {{",
			f"\t\t{name} {{ bytes: self.bytes }}",
			"\t}",
		])

		lines.extend(self._dirty_constants(struct))

		for entry in own_entries(struct):
			lines.extend(self._setter(struct, entry))

		lines.extend(self._covered_nested_setters(struct))
		lines.extend(self._invariants(struct))
		lines.extend(["}", ""])
		return [*types, *lines]

	def _covered_nested_setters(self, struct: ResolvedStruct) -> list[str]:
		"""A covered write for a field of a *nested* struct, on the parent.

		`own_entries` drops a dotted path and a nested struct's fields have
		one, so a covered field inside one never reached the branch that marks
		the bit -- and the only way to write it was the nested type's own
		setter, which marks nothing. The map says `auth = Covered(t)` about
		that field and 14.2 says a covered write leaves `t` stale (26.35).

		The nested type keeps its plain setter, which is right: the type may
		be used where nothing covers it, and it cannot know.
		"""
		lines: list[str] = []

		for entry in struct.entries:
			placement = entry.placement
			scalar    = placement.scalar

			if "." not in local_name(struct, placement):
				continue
			if not placement.covered_by or scalar is None:
				continue
			if placement.kind != "field" or placement.array_count is not None:
				continue
			if placement.sized_by is not None:
				continue
			if entry.vector.get(Axis.MUTATE).base not in ("InPlaceFixed",
			                                             "InPlaceSlack"):
				continue

			# A sealed member is not written from out here: the interior is
			# behind the gate of 14.3, and this wrote it at its plaintext
			# offset from the outer view. C has demanded the gate since the
			# machinery landed; this backend's gate holds a shared slice, so
			# there is no write path through it to offer instead.
			if placement.sealed_by is not None:
				lines.extend([
					"",
					f"\t// No setter for `{placement.path}`: it is sealed, so"
					" the write",
					"\t// goes through the gate (14.3) -- and `Gate` borrows the"
					" bytes",
					"\t// immutably, so this backend has no gated setter to"
					" offer.",
				])
				continue

			name  = _ident(f"set_{c_name(local_name(struct, placement))}")
			what  = ", ".join(placement.covered_by)
			rtype = self._field_type(placement, writing=True)

			# The offset may be the message's, and a field of a nested struct
			# behind a variable-length member has a *frame-relative* one --
			# measured from the nested struct rather than from these bytes.
			owner = dynamic_frame_owner(struct, placement)
			start = self._offset_expression(struct, owner or placement)
			if placement.offset_bits is None or owner is not None:
				if start is None:
					lines.extend([
						"",
						f"\t// No {name}(): this backend cannot resolve where",
						f"\t// `{placement.path}` starts.",
					])
					continue
				at: str | None = (
					f"{self._unparen(start)} + {placement.offset_bytes}"
					if owner is not None else self._unparen(start))
			else:
				at = None

			# ...and the offset is the message's, so the frame is not known
			# to hold it. `write_be` indexes the slice, so this is a *panic*
			# rather than a wrong write -- which is the safe end of that
			# spectrum and is still a generated setter killing the caller's
			# process over bytes the caller did not choose.
			width = max(1, scalar.bits // BITS_PER_BYTE)
			guard = ([] if at is None else [
				f"\t\tif self.bytes.len().saturating_sub({at}) < {width} {{",
				"\t\t\treturn;",
				"\t\t}",
			])
			lines.extend([
				"",
				f"\t/// `{placement.path}` is under {what}, so writing it here",
				"\t/// marks the bit. Its own type's setter cannot: the type may",
				"\t/// sit where nothing covers it.",
				f"\tpub fn {name}(&mut self, dirty: &mut Dirty,"
				f" value: {rtype}) {{",
				*guard,
				f"\t\t{self._store(placement, scalar, at)}",
				f"\t\tdirty.mark({self._dirty_bits(struct, placement)});",
				"\t}",
			])

		return lines

	# -- reads ---------------------------------------------------------

	def _indexed_region(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""An offset table, then the elements it reaches (section 9.3).

		The region answered `REGION` in the shared classifier and this backend
		emitted "not in the static subset yet" -- the fallthrough note, for the
		last construct no backend reached into.
		"""
		table = placement.index_table
		if table is None:
			return []

		start = self._offset_expression(struct, placement)
		if start is None:
			return ["", f"\t// {placement.path}: this backend cannot resolve"
			        " where the region starts."]

		name    = c_name(local_name(struct, placement))
		width   = table.entry_bits // BITS_PER_BYTE
		element = self.resolved.structs.get(table.element or "")
		reader  = _reader(placement.endian)

		# A literal count needs no read; a field-driven one is the driver's own
		# accessor, which already lands in `usize`.
		count = self._count_expression(struct, placement)
		if count is None:
			fixed = table.count_fixed
			if fixed is None:
				return ["", f"\t// {placement.path}: this backend cannot resolve"
				        " how many entries",
				        "\t// the table holds, so nothing below could be"
				        " bounded."]
			count = str(fixed)

		lines = [
			"",
			f"\t/// `{placement.path}` is an offset table of {width}-byte"
			f" entries, then",
			"\t/// the elements it reaches. Element N is one read of entry N"
			" plus a",
			"\t/// base, whatever the elements weigh -- which is why `access`"
			" stays",
			"\t/// Random through a region whose elements need not be the same"
			" size.",
			"\t///",
			"\t/// Insertion is not an operation here: every offset after the",
			"\t/// insertion point would have to move.",
			f"\tpub fn {_ident(f'{name}_count')}(&self) -> usize {{",
			f"\t\t{count}",
			"\t}",
			"",
			"\t/// The offset held in entry `index`, as written -- measured"
			" from",
			f"\t/// {self._index_base_noun(table)}.",
			f"\tpub fn {_ident(f'{name}_offset')}(&self, index: usize)"
			f" -> Result<usize> {{",
			f"\t\tlet at = {start} + index * {width};",
			"",
			f"\t\tif index >= self.{_ident(f'{name}_count')}() {{",
			"\t\t\treturn Err(Error::Bounds);",
			"\t\t}",
			f"\t\tif at + {width} > self.bytes.len() {{",
			"\t\t\treturn Err(Error::Bounds);",
			"\t\t}",
			"",
			f"\t\tOk(situ_rt::{reader}(self.bytes, at, {width}) as usize)",
			"\t}",
		]
		lines.extend(self._index_element(struct, placement, table, name,
		                                 element, start))
		return lines

	def _index_base_noun(self, table: IndexTable) -> str:
		if table.base == "message":
			return "the start of the *message*"
		if table.base == "member":
			return f"the start of `{table.base_member}`"
		return "the start of this region"

	def _index_element(self, struct: ResolvedStruct, placement: Placement,
			table: IndexTable, name: str, element: ResolvedStruct | None,
			start: str) -> list[str]:
		"""`at`: the element an entry reaches, as a borrowed view over it."""
		if element is None or (not element.layout.is_fixed_size
		                       and self._extent_expression(element) is None):
			held = table.element or placement.type_name
			return ["", f"\t// No `{name}_at`: one `{held}` has no extent this"
			        " backend can",
			        "\t// compute, so an entry gives a position and not a view."
			        " The offsets",
			        "\t// are still readable above."]

		if table.base == "message":
			return ["", f"\t// No `{name}_at`: these offsets are measured from"
			        " the message, and",
			        "\t// a struct here borrows the frame it was framed"
			        " against rather than",
			        "\t// the whole buffer -- so the element is not this"
			        " view's to hand out.",
			        "\t// The offsets are readable above."]

		inner  = _pascal(element.name)
		origin = (self._index_member_base(struct, table)
		          if table.base == "member" else start)

		return [
			"",
			"\t/// Element `index`, whose offset is measured from",
			f"\t/// {self._index_base_noun(table)}.",
			f"\tpub fn {_ident(f'{name}_at')}(&self, index: usize)"
			f" -> Result<{inner}<'_>> {{",
			f"\t\tlet start = {origin} + self.{_ident(f'{name}_offset')}(index)?;",
			"",
			"\t\tif start > self.bytes.len() {",
			"\t\t\treturn Err(Error::Bounds);",
			"\t\t}",
			*self._index_element_extent(element, inner),
			"\t}",
		]

	def _index_member_base(self, struct: ResolvedStruct,
			table: IndexTable) -> str:
		found = self.resolved.find(f"{struct.name}.{table.base_member}")
		if found is None:
			return "0"
		return self._offset_expression(struct, found.placement) or "0"

	def _index_element_extent(self, element: ResolvedStruct,
			inner: str) -> list[str]:
		"""Narrow to one element, measuring it first where it varies."""
		if element.layout.is_fixed_size:
			return [
				f"\t\tif start + {inner}::SIZE > self.bytes.len() {{",
				"\t\t\treturn Err(Error::Bounds);",
				"\t\t}",
				f"\t\tOk({inner} {{ bytes: &self.bytes[start..start"
				f" + {inner}::SIZE] }})",
			]

		return [
			"",
			"\t\t// The extent is in the element's own bytes, so it takes a"
			" view to",
			"\t\t// read and a view is what it decides. Measure over the rest"
			" of the",
			"\t\t// region, then narrow.",
			f"\t\tlet probe = {inner} {{ bytes: &self.bytes[start..] }};",
			"\t\tlet size  = probe.extent();",
			"\t\tif start + size > self.bytes.len() {",
			"\t\t\treturn Err(Error::Bounds);",
			"\t\t}",
			f"\t\tOk({inner} {{ bytes: &self.bytes[start..start + size] }})",
		]

	def _tlv_region(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""A run of tag-length-value items, walked as the schema describes them.

		The region answered `REGION` in the shared classifier and this backend
		emitted "not in the static subset yet", which reads as a missing
		feature rather than the fallthrough it was.

		The accessors return the item rather than filling one out: a cursor is
		a value here, and an out-parameter would be C's shape wearing Rust's
		syntax.
		"""
		grammar = placement.tlv_grammar
		if grammar is None or not grammar.walkable:
			return ["", f"\t// No accessors for {placement.path}: the region"
			        " says how its items are",
			        "\t// tagged and not how long their values are, so a walk"
			        " has nowhere to",
			        "\t// put the second item."]

		start = self._offset_expression(struct, placement)
		if start is None:
			return ["", f"\t// {placement.path}: this backend cannot resolve"
			        " where the region starts."]

		name = c_name(local_name(struct, placement))
		item = _pascal(struct.name) + _pascal(name) + "Item"

		lines = list(self._tlv_read(placement, grammar, item, name))
		lines.extend(self._tlv_cursor(item, name, start))
		lines.extend(self._tlv_by_name(grammar, item, name))
		return lines

	def _tlv_item_type(self, struct: ResolvedStruct, placement: Placement,
			grammar: TlvGrammar) -> list[str]:
		"""The item struct, at module scope: Rust has no nested types.

		Emitted beside the struct's own types rather than inside the `impl`,
		which is where the rest of this backend puts a type it needs.
		"""
		name  = c_name(local_name(struct, placement))
		item  = _pascal(struct.name) + _pascal(name) + "Item"
		parts = [f"\tpub {_ident(part.name)}: u32,\t// {part.source}"
		         for part in grammar.tag_decode]

		return [
			"",
			f"/// One item of `{placement.path}`, and where the next one starts.",
			"///",
			"/// The decoded parts are named by the schema; a backend inventing",
			"/// its own would be describing protobuf rather than this region.",
			"#[derive(Clone, Copy, Debug, Default)]",
			f"pub struct {item} {{",
			"\tpub at: usize,",
			"\tpub next: usize,",
			"\tpub tag: u64,",
			*parts,
			"\tpub value_at: usize,",
			"\tpub value_len: usize,",
			"}",
		]

	def _tlv_tag_bytes(self, placement: Placement) -> int:
		declared = next((decl for decl in self.schema.varints()
		                 if decl.name == placement.tlv_tag_varint), None)
		bits = declared.max_bits if declared is not None else 64
		return (bits + 6) // 7

	def _tlv_read(self, placement: Placement, grammar: TlvGrammar,
			item: str, name: str) -> list[str]:
		"""Read the item at `at`: its tag, its parts, and where its value ends."""
		max_tag = self._tlv_tag_bytes(placement)
		decoded = [f"\t\t\t{_ident(part.name)}: ({part.source}) as u32,"
		           for part in grammar.tag_decode]

		# `at` only moves a second time where a length prefix sits between the
		# tag and the value. Declaring it `mut` regardless is an unused-mut
		# warning, and this backend builds under `-D warnings`.
		moves = (grammar.selector is None
		         or any(rule.kind == "prefixed" for rule in grammar.rules))
		binding = "let mut at" if moves else "let at"

		lines = [
			"",
			f"\t/// Read the item at `at`. `Error::Bounds` where the region",
			"\t/// ends or an item runs past it; `Error::Constraint` for a wire",
			"\t/// type this schema does not describe.",
			f"\tpub fn {name}_read(&self, at: usize) -> Result<{item}> {{",
			"\t\tif at >= self.bytes.len() {",
			"\t\t\treturn Err(Error::Bounds);",
			"\t\t}",
			"",
			f"\t\tlet (tag, used) = situ_rt::varint_get(self.bytes, at,"
			f" {max_tag})",
			"\t\t\t.ok_or(Error::Bounds)?;",
			"\t\tlet start = at;",
			f"\t\t{binding} = at + used;",
			"\t\tlet size;",
			"",
		]

		lines.extend(self._tlv_value_extent(grammar, max_tag, placement.endian))
		lines.extend([
			"",
			"\t\tif size > self.bytes.len() - at {",
			"\t\t\treturn Err(Error::Bounds);",
			"\t\t}",
			"",
			f"\t\tOk({item} {{",
			"\t\t\tat: start,",
			*decoded,
			"\t\t\ttag,",
			"\t\t\tvalue_at: at,",
			"\t\t\tvalue_len: size,",
			"\t\t\tnext: at + size,",
			"\t\t})",
			"\t}",
		])
		return lines

	def _tlv_value_extent(self, grammar: TlvGrammar, max_tag: int,
			endian: ast.Endian | None) -> list[str]:
		"""Where the value ends, dispatched as the schema dispatches it."""
		if grammar.selector is None:
			return self._tlv_prefixed_size(grammar.length_type or "u8",
			                               "\t\t", endian)

		selector = next((part for part in grammar.tag_decode
		                 if part.name == grammar.selector), None)
		chosen = selector.source if selector else "tag"
		lines  = [f"\t\tmatch ({chosen}) as u32 {{"]
		for rule in grammar.rules:
			if rule.label is None:
				continue
			lines.append(f"\t\t\t{rule.label} => {{")
			lines.extend(self._tlv_one_rule(rule, max_tag, endian))
			lines.append("\t\t\t}")

		default = next((rule for rule in grammar.rules if rule.label is None),
		               None)
		lines.append("\t\t\t_ => {")
		if default is None or default.kind == "error":
			lines.extend([
				"\t\t\t\t// `default: error`: a wire type this schema does not",
				"\t\t\t\t// describe, so where the value ends is not knowable.",
				"\t\t\t\treturn Err(Error::Constraint);",
			])
		else:
			lines.extend(self._tlv_one_rule(default, max_tag, endian))
		lines.extend(["\t\t\t}", "\t\t}"])
		return lines

	def _tlv_one_rule(self, rule: ValueRule, max_tag: int,
			endian: ast.Endian | None) -> list[str]:
		if rule.kind == "fixed":
			return [f"\t\t\t\tsize = {rule.size};"]
		if rule.kind == "error":
			return ["\t\t\t\treturn Err(Error::Constraint);"]
		if rule.kind == "self_delimiting":
			# The value carries its own extent, so reading it is measuring it.
			return [
				f"\t\t\t\tlet (_, used) = situ_rt::varint_get(self.bytes, at,"
				f" {max_tag})",
				"\t\t\t\t\t.ok_or(Error::Bounds)?;",
				"\t\t\t\tsize = used;",
			]
		return self._tlv_prefixed_size(rule.length_type or "u8", "\t\t\t\t",
		                               endian)

	def _tlv_prefixed_size(self, length_type: str, indent: str,
			endian: ast.Endian | None) -> list[str]:
		"""`prefixed(T)`: a length in T, then that many bytes."""
		declared = next((decl for decl in self.schema.varints()
		                 if decl.name == length_type), None)

		if declared is not None:
			width = (declared.max_bits + 6) // 7
			return [
				f"{indent}let (length, used) = situ_rt::varint_get(self.bytes,"
				f" at, {width})",
				f"{indent}\t.ok_or(Error::Bounds)?;",
				f"{indent}at += used;",
				f"{indent}if length > (self.bytes.len() - at) as u64 {{",
				f"{indent}\treturn Err(Error::Bounds);",
				f"{indent}}}",
				f"{indent}size = length as usize;",
			]

		scalar = lookup(length_type)
		width  = (scalar.bits + 7) // 8 if scalar is not None else 1
		reader = _reader(endian)
		return [
			f"{indent}if self.bytes.len() - at < {width} {{",
			f"{indent}\treturn Err(Error::Bounds);",
			f"{indent}}}",
			f"{indent}size = situ_rt::{reader}(self.bytes, at, {width})"
			f" as usize;",
			f"{indent}at += {width};",
		]

	def _tlv_cursor(self, item: str, name: str, start: str) -> list[str]:
		"""`first`, `next`, `count`, and the value's bytes."""
		return [
			"",
			"\t/// The first item, or `Error::Bounds` if the region is empty.",
			f"\tpub fn {name}_first(&self) -> Result<{item}> {{",
			f"\t\tself.{name}_read({start})",
			"\t}",
			"",
			"\t/// The item after this one.",
			f"\tpub fn {name}_next(&self, item: &{item}) -> Result<{item}> {{",
			f"\t\tself.{name}_read(item.next)",
			"\t}",
			"",
			"\t/// This item's value. A slice rather than an offset pair, which",
			"\t/// is the borrow the other three backends cannot express.",
			f"\tpub fn {name}_value(&self, item: &{item}) -> &[u8] {{",
			"\t\t&self.bytes[item.value_at..item.value_at + item.value_len]",
			"\t}",
			"",
			"\t/// How many items are present. A walk: nothing in the region",
			"\t/// records a count.",
			f"\tpub fn {name}_count(&self) -> usize {{",
			"\t\tlet mut n = 0usize;",
			f"\t\tlet mut held = self.{name}_first();",
			"",
			"\t\twhile let Ok(item) = held {",
			"\t\t\tn += 1;",
			f"\t\t\theld = self.{name}_next(&item);",
			"\t\t}",
			"\t\tn",
			"\t}",
		]

	def _tlv_by_name(self, grammar: TlvGrammar, item: str,
			name: str) -> list[str]:
		"""`find`, and one accessor per tag the schema names."""
		if not grammar.known:
			return []

		keyed = (f"item.{_ident(grammar.identity)}" if grammar.identity
		         else "item.tag as u32")
		named = (f"the part `{grammar.identity}` decodes to" if grammar.identity
		         else "the raw tag")

		lines = [
			"",
			f"\t/// The first item whose tag is `tag`, matched against {named}",
			"\t/// (decision 0023). O(n): the region is walked from the start,",
			"\t/// which is what `access = Sequential` costs.",
			f"\tpub fn {name}_find(&self, tag: u32) -> Result<{item}> {{",
			f"\t\tlet mut held = self.{name}_first();",
			"",
			"\t\twhile let Ok(item) = held {",
			f"\t\t\tif {keyed} == tag {{",
			"\t\t\t\treturn Ok(item);",
			"\t\t\t}",
			f"\t\t\theld = self.{name}_next(&item);",
			"\t\t}",
			"\t\tErr(held.unwrap_err())",
			"\t}",
		]

		for known in grammar.known:
			described = f"tag {known.tag}"
			if known.wire is not None:
				described += f", wire type {known.wire}"
			if known.type_name is not None:
				described += f", carrying {known.type_name}"
				described += "[]" if known.repeated else ""
			lines.extend([
				"",
				f"\t/// `{known.name}`: {described}.",
				f"\tpub fn {_ident(known.name)}(&self) -> Result<{item}> {{",
				f"\t\tself.{name}_find({known.tag})",
				"\t}",
			])

		return lines

	def _reads_varint(self, placement: Placement) -> bool:
		"""Whether this backend can read the varint this member is.

		A member sized by one it cannot has no length either, so the refusal
		reaches the offset chain rather than naming an accessor nobody wrote.
		Both encodings are read now; the check stays because the next one to
		arrive should reach the same place rather than find it removed.
		"""
		return any(decl.name == placement.varint
		           for decl in self.schema.varints())

	def _offsets(self, struct: ResolvedStruct) -> list[str]:
		"""Every dynamic offset in this struct, resolved in one pass.

		A scan makes reaching member N a rescan of the N-1 before it, and the
		per-member offset does that on every call. This is that sum once. The
		memory is the caller's, which is why it is behind `--materialize`.
		"""
		if not self.materialize:
			return []

		members = [entry.placement for entry in own_entries(struct)]
		dynamic = [held for held in members
		           if held.offset_bits is None and held.located is None]
		if not dynamic:
			return []

		plan = offset_plan(struct, members,
		                   lambda held: self._length_expression(struct, held)
		                   is not None or held.is_fixed_size)
		if plan is None:
			return ["",
			        f"\t// No offset cache for {struct.name}: a member has no",
			        "\t// length this can compute, so the offsets after it",
			        "\t// cannot be resolved in one pass any more than one at",
			        "\t// a time."]

		steps: list[str] = []
		for step in plan:
			if step.kind == "record":
				assert step.placement is not None
				steps.append(f"\t\t\t{_ident(c_name(local_name(struct, step.placement)))}"
				             ": at,")
			elif step.placement is None:
				steps.append(f"\t\tat += {step.size};")
			else:
				length = self._length_expression(struct, step.placement,
				                                 running="at")
				steps.append(f"\t\tat += {length};")

		# The record steps become struct fields at the end rather than
		# assignments as they are reached: a Rust struct is built whole, so the
		# running total is captured into a local at each point instead.
		body: list[str] = []
		captured: list[str] = []
		for step in plan:
			if step.kind == "record":
				assert step.placement is not None
				local = _ident(c_name(local_name(struct, step.placement)))
				body.append(f"\t\tlet {local} = at;")
				captured.append(local)
			elif step.placement is None:
				body.append(f"\t\tat += {step.size};")
			else:
				length = self._length_expression(struct, step.placement,
				                                 running="at")
				body.append(f"\t\tat += {length};")

		name   = _pascal(struct.name) + "Offsets"
		fields = [f"\tpub {_ident(c_name(local_name(struct, held)))}: usize,"
		          for held in dynamic]

		return [
			"",
			f"/// Where each dynamically-placed member of `{struct.name}`"
			" starts.",
			"///",
			"/// The per-member offset resolves one by summing what precedes",
			"/// it, so it rescans every delimited member ahead of the one",
			"/// asked for, on every call. This is that sum once, for all of",
			"/// them.",
			"#[derive(Clone, Copy, Debug, Default)]",
			f"pub struct {name} {{",
			*fields,
			"}",
			"",
			f"impl<'a> {_pascal(struct.name)}<'a> {{",
			f"\tpub fn resolve_offsets(&self) -> {name} {{",
			"\t\t#[allow(unused_mut)]",
			"\t\tlet mut at = 0usize;",
			"",
			*body,
			"",
			f"\t\t{name} {{ " + ", ".join(captured) + " }",
			"\t}",
			"}",
		]

	def _opaque(self, struct: ResolvedStruct, placement: Placement) -> list[str]:
		"""Treat-as-bytes, the whole of what an `opaque` region supports (9.4).

		It reached the fallthrough note, which claims the language does not
		support the construct.
		"""
		name   = _ident(c_name(local_name(struct, placement)))
		start  = self._offset_expression(struct, placement)
		length = self._length_expression(struct, placement)
		if start is None or length is None:
			return ["", f"\t// {placement.path}: this backend cannot resolve"
			        " where the region is."]

		at = self._unparen(start)
		return [
			"",
			f"\t/// `{placement.path}`: bytes and nothing more. An opaque",
			"\t/// region has no interior to address -- that is what it trades",
			"\t/// for carrying anything at all (9.4).",
			f"\tpub fn {name}(&self) -> &[u8] {{",
			f"\t\tlet at = {at};",
			f"\t\t&self.bytes[at..at + ({length})]",
			"\t}",
		]

	def _tag(self, struct: ResolvedStruct, placement: Placement) -> list[str]:
		"""A tag's bytes, the span it covers, and its dirty bit (14.2).

		This backend emitted the dirty constant and the setters that mark it,
		and then said "not in the static subset yet" about the tag itself -- so
		a caller could be told a write left the tag stale and had no way to
		reach the tag, ask whether it was stale, or say it no longer was.
		"""
		name  = _ident(c_name(local_name(struct, placement)))
		count = placement.array_count or 0
		start = self._offset_expression(struct, placement)
		if start is None:
			return ["", f"\t// {placement.path}: this backend cannot resolve"
			        " where the tag sits."]

		at    = self._unparen(start)
		# Empty where it does not fit: a tag sits after everything it covers,
		# so its offset is a sum of lengths the message chose, and slicing
		# past the end panics -- an abort in `no_std` (26.27).
		fits  = self._fits(struct, placement, count)
		lines = [
			"",
			f"\t/// `{placement.path}`: {count} bytes. The algorithm is the",
			"\t/// caller's to run -- situ says which bytes it covers and when",
			"\t/// the result has gone stale, not how to compute it.",
			*([] if fits is None else [
				"\t///",
				"\t/// Empty where the member does not fit: its offset is a sum",
				"\t/// of lengths the message chose, and `validate` reports",
				"\t/// such a message as malformed.",
			]),
			f"\tpub fn {name}(&self) -> &[u8] {{",
			*([f"\t\t&self.bytes[{at}..{at} + {count}]"] if fits is None else [
				f"\t\tif !({fits}) {{",
				"\t\t\treturn &[];",
				"\t\t}",
				f"\t\t&self.bytes[{at}..{at} + {count}]",
			]),
			"\t}",
		]

		run = covered_run(struct, placement)
		if run is not None:
			first, last = run
			lines.extend([
				"",
				f"\t/// The bytes `{placement.name}` covers:"
				f" `{'`, `'.join(placement.tag_covers)}`.",
				"\t///",
				"\t/// Write the result over the slice above and then clear the",
				"\t/// dirty bit. A gap in the coverage is reported rather than",
				"\t/// papered over with a range covering bytes the tag does",
				"\t/// not.",
				f"\tpub fn {name}_covered(&self) -> Result<(usize, usize)> {{",
				f"\t\tlet start = {self._unparen(self._offset_expression(struct, first) or '0')};",
				f"\t\tlet end   = {self._region_end(struct, last)};",
				"",
				"\t\tif end < start || end > self.bytes.len() {",
				"\t\t\treturn Err(Error::Bounds);",
				"\t\t}",
				"\t\tOk((start, end - start))",
				"\t}",
			])

		filler = _self_as(placement.attrs)
		if filler is not None:
			# A checksum defined over its own field runs the algorithm with
			# those bytes taken as a constant. They are still there, so what
			# the compiler hands out is where they are and what they read as
			# (14.2); substituting them is the caller's loop.
			lines.extend([
				"",
				f"\t/// What `{placement.name}`'s own bytes read as while it is",
				"\t/// computed, and where they are. Sum the covered span,",
				"\t/// substituting this value for those bytes. RFC 1071 is the",
				"\t/// case this exists for.",
				f"\tpub const SELF_AS_{c_name(placement.name).upper()}: u8 ="
				f" {filler:#04x};",
				"",
				f"\tpub fn {name}_self_span(&self) -> Result<(usize, usize)> {{",
				f"\t\tlet at = {self._unparen(self._offset_expression(struct, placement) or '0')};",
				f"\t\tlet n  = {placement.size_bits // BITS_PER_BYTE};",
				"",
				"\t\tif at + n > self.bytes.len() {",
				"\t\t\treturn Err(Error::Bounds);",
				"\t\t}",
				"\t\tOk((at, n))",
				"\t}",
			])

		held = obligation(self.schema, struct, placement.name)
		if held is not None:
			lines.extend([
				"",
				f"\t/// Whether `{placement.name}` no longer matches the bytes",
				"\t/// it covers, and how to say it does again.",
				f"\tpub fn {name}_is_dirty(dirty: &Dirty) -> bool {{",
				# The constants are on the `Mut` struct, this backend putting
				# the write side there; the query belongs with the reader.
				f"\t\tdirty.is_stale({_pascal(struct.name)}Mut::"
				f"DIRTY_{name.upper()})",
				"\t}",
				"",
				f"\tpub fn {name}_finalize(dirty: &mut Dirty) {{",
				f"\t\tdirty.clear({_pascal(struct.name)}Mut::"
				f"DIRTY_{name.upper()})",
				"\t}",
			])

		return lines

	def _region_end(self, struct: ResolvedStruct, region: Placement) -> str:
		"""Where a region stops, taken from where the next member starts."""
		if region.is_fixed_size and region.offset_bits is not None:
			return str(region.offset_bytes + region.size_bits // BITS_PER_BYTE)

		members = [entry.placement for entry in own_entries(struct)]
		index   = next((i for i, held in enumerate(members)
		                if held.path == region.path), None)
		if index is not None and index + 1 < len(members):
			found = self._offset_expression(struct, members[index + 1])
			if found is not None:
				return self._unparen(found)
		return "self.bytes.len()"

	def _holds(self, at: str, width: int, empty: str) -> list[str]:
		"""Guard lines: leave early unless `width` bytes sit at `at`.

		Two shapes, because one of them warns. `-D warnings` is on the
		generated Rust (invariant 23), and `self.bytes.len() < 0` is a useless
		comparison to rustc -- so a constant offset folds into a single
		comparison and only a computed one needs the subtraction that avoids
		overflowing back under the length.
		"""
		if at.isdigit():
			return [f"\t\tif self.bytes.len() < {int(at) + width} {{",
			        f"\t\t\treturn {empty};",
			        "\t\t}"]
		return [f"\t\tif self.bytes.len() < {at}"
		        f" || self.bytes.len() - {at} < {width} {{",
		        f"\t\t\treturn {empty};",
		        "\t\t}"]

	def _fixed_text_number(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""Digits in a field of declared width, padded (section 8.6.2).

		The bracket is a width in bytes and not a count, which is what this
		read it as -- so it reported "element type u16 has no fixed size"
		about a type that plainly has one.
		"""
		scalar = placement.scalar
		if scalar is None:
			return []

		name  = _ident(c_name(local_name(struct, placement)))
		width = placement.array_count or 0
		limit = placement.radix_max or 0
		start = self._offset_expression(struct, placement)
		if start is None:
			return ["", f"\t// {placement.path}: this backend cannot resolve"
			        " where the digits start."]

		rtype = self._rust_type(scalar)
		at    = self._unparen(start)

		return [
			"",
			f"\t/// `{placement.path}`: {width} digits, padded, holding"
			f" 0..{limit}.",
			"\t///",
			f"\t/// The range is the field's rather than {scalar.name}'s:"
			f" {width} bytes",
			f"\t/// cannot hold what {scalar.name} can, and a check against"
			" the type",
			"\t/// would accept a value the field cannot represent.",
			f"\tpub fn {name}_digits(&self) -> &[u8] {{",
			# Empty where the digits are not all here. The acquiring check
			# guarantees the fixed part of a frame *acquired through it*, and
			# an extent function builds a struct over whatever is left rather
			# than through it -- so a nested struct at the end of a short
			# message reached this with nothing behind it, and the slice
			# panicked where the runtime's own reads return zero.
			*self._holds(at, width, "&[]"),
			f"\t\t&self.bytes[{at}..{at} + {width}]",
			"\t}",
			"",
			f"\tpub fn {name}(&self) -> Result<{rtype}> {{",
			f"\t\tsitu_rt::parse_uint(self.{name}_digits(),"
			f" {placement.radix}, {limit})",
			f"\t\t\t.map(|v| v as {rtype})",
			"\t\t\t.ok_or(Error::Constraint)",
			"\t}",
		]

	def _varint_field(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""Decode a varint field, and say how wide it turned out to be.

		It classified as `NOTHING` and this backend emitted nothing at all --
		not an accessor and not a note. The member after it said its offset
		could not be resolved, which is the safe half of the same gap.
		"""
		declared = next((decl for decl in self.schema.varints()
		                 if decl.name == placement.varint), None)
		if declared is None:
			return []

		name  = c_name(local_name(struct, placement))
		start = self._offset_expression(struct, placement)
		if start is None:
			return ["", f"\t// {placement.path}: this backend cannot resolve"
			        " where it starts."]

		width  = declared.max_bytes
		signed = declared.transform is ast.VarintTransform.ZIGZAG
		rtype  = "i64" if signed else "u64"
		decoded = "situ_rt::zigzag_decode(raw)" if signed else "raw"
		big    = declared.encoding is ast.VarintEncoding.BE128

		read = (f"situ_rt::varint_be_get(self.bytes, at, {width},"
		        f" {declared.terminal_bits})" if big else
		        f"situ_rt::varint_get(self.bytes, at, {width})")
		encoded = (f"situ_rt::varint_be_len(raw, {width},"
		           f" {declared.terminal_bits})" if big else
		           "situ_rt::varint_len(raw)")

		minimal = ([
			"",
			"\t\t// `minimal` is declared, so a padded encoding is a second",
			"\t\t// encoding of one value and this schema does not admit it.",
			f"\t\tif used != {encoded} {{",
			"\t\t\treturn Err(Error::Constraint);",
			"\t\t}",
		] if declared.minimal else [])

		return [
			"",
			f"\t/// `{placement.path}`: a `{placement.varint}`, 1 to {width}"
			f" bytes, and",
			"\t/// how many is in the bytes themselves.",
			"\t///",
			"\t/// `Error::Bounds` where the frame ends mid-value;"
			" `Error::Constraint`",
			"\t/// where a padded encoding is refused. Everything after it in"
			" this",
			"\t/// frame is measured through `_len`, which is what",
			"\t/// `offset = Dynamic` costs here -- a read, not a scan.",
			f"\tpub fn {_ident(name)}(&self) -> Result<{rtype}> {{",
			f"\t\tlet at = {start};",
			"",
			"\t\tif at >= self.bytes.len() {",
			"\t\t\treturn Err(Error::Bounds);",
			"\t\t}",
			"",
			f"\t\tlet (raw, {'used' if declared.minimal else '_'}) = {read}",
			"\t\t\t.ok_or(Error::Bounds)?;",
			*minimal,
			"",
			f"\t\tOk({decoded})",
			"\t}",
			"",
			f"\t/// How many bytes `{placement.path}` occupies. Zero where it",
			"\t/// cannot be read at all, which keeps every offset derived from",
			"\t/// it inside the frame.",
			f"\tpub fn {_ident(name + '_len')}(&self) -> usize {{",
			f"\t\tlet at = {start};",
			"",
			"\t\tif at >= self.bytes.len() {",
			"\t\t\treturn 0;",
			"\t\t}",
			f"\t\tmatch {read} {{",
			"\t\t\tSome((_, used)) => used,",
			"\t\t\tNone => 0,",
			"\t\t}",
			"\t}",
			"",
			"\t/// The same value where an error cannot be returned: the length",
			"\t/// arithmetic downstream is not fallible.",
			f"\tpub fn {_ident(name + '_value')}(&self) -> {rtype} {{",
			f"\t\tself.{_ident(name)}().unwrap_or(0)",
			"\t}",
		]

	def _coded_region(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""The encoded bytes of a coded region, and the decode beside them.

		The decode calls the C implementation, which decision 0017 makes the
		only one there is -- so it goes through `extern "C"` and therefore
		through `unsafe`, and section 26.18 says where that `unsafe` belongs:
		at the call site, with a note saying what the caller is promising,
		rather than buried in a helper.
		"""
		name  = _ident(c_name(local_name(struct, placement)))
		start = self._offset_expression(struct, placement)
		if start is None or placement.size_max_bits is None \
				or placement.size_bits % BITS_PER_BYTE:
			return ["", f"\t// No accessor for {placement.path}: its encoded",
			        f"\t// extent is {placement.codec}'s to report."]

		# The interior's extent through the codec's expansion
		# (`traverse.region_extent`), not the region's minimum: a region whose
		# interior the data sizes reported zero bytes, in all four backends
		# and with no refusal (26.35). A fixed interior is unaffected -- its
		# minimum is its extent.
		size = self._region_length(struct, placement)
		if size is None:
			return ["", f"\t// No accessor for {placement.path}: its encoded",
			        f"\t// extent is {placement.codec}'s to report."]

		lines = [
			"",
			f"\t/// `{placement.path}` is `{placement.codec}` output: the bytes",
			"\t/// on the wire rather than the value. What they mean is behind",
			"\t/// the transform (13.5).",
			f"\tpub fn {name}(&self) -> &[u8] {{",
			f"\t\tlet at = {self._unparen(start)};",
			# Clamped, because the length is the interior's and the interior is
			# sized by fields the message chose.
			f"\t\tlet n  = core::cmp::min({size},",
			"\t\t\tself.bytes.len().saturating_sub(at));",
			"\t\t&self.bytes[at..at + n]",
			"\t}",
		]

		return lines + self._decode_accessor(struct, placement, size)

	def _coded_delimited(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""A region found by scanning and then decoded (section 13.6).

		Scan first, decode second: a stuffing code protects its own terminator,
		so the sequence is unambiguous in the encoded bytes and would not be in
		the decoded ones. This backend emitted the bytes and said nothing about
		the transform, so a reader had no way to know they were not the value.
		"""
		name    = _ident(c_name(local_name(struct, placement)))
		decoded = self._decode_accessor(struct, placement, f"self.{name}_len()")

		about = ([("\t/// The decoded bytes are below: the transform is derived"
		           " from the"),
		          f"\t/// kernel, so the length is {placement.codec}'s to"
		          " report and not",
		          "\t/// this module's to guess."] if decoded else
		         [("\t/// There is no accessor for the decoded bytes: the"
		           " transform is"),
		          f"\t/// the caller's to run, and its length is"
		          f" {placement.codec}'s to report",
		          "\t/// rather than this module's to guess."])

		return [
			"",
			f"\t/// `{placement.path}` is `{placement.codec}` output, and the",
			"\t/// bytes above are the encoded form.",
			"\t///",
			*about,
			"\t///",
			"\t/// The scan runs on the encoded bytes, which is the order the",
			"\t/// format specifies -- a stuffing code protects its own",
			"\t/// terminator, so the sequence is unambiguous here and would",
			"\t/// not be after decoding.",
			*decoded,
		]

	def _decode_accessor(self, struct: ResolvedStruct, placement: Placement,
			encoded: str) -> list[str]:
		"""The decoded bytes, into a buffer the caller owns.

		`encoded` is how many bytes the wire form occupies: a constant for a
		sized region, the scan's own length for a delimited one -- and not the
		span, which includes a delimiter the codec has no business
		transforming.
		"""
		codec = self.codecs.get(placement.codec or "")
		if codec is None:
			return []

		# A tier-1 codec is bound to an implementation and to the ABI of
		# 13.2a, which is settled whatever the algorithm -- so a region with
		# an `extern` impl decodes here too (26.35).
		symbol = extern_symbol(self.schema, placement.codec or "")
		if symbol is not None:
			return self._extern_decode(struct, placement, codec, symbol,
			                           encoded)

		if not decodes_here(codec):
			return []

		ratio = codec.ratio
		if ratio is None or ratio[0] == 0:
			return []

		name    = _ident(c_name(local_name(struct, placement)))
		bound   = decode_bound(codec, placement)
		bitwise = decode_counts_bits(codec)
		sym     = f"situ_{c_name(placement.codec or '')}_decode"
		scale   = " * 8" if bitwise else ""
		unscale = " / 8" if bitwise else ""

		return [
			"",
			f"\t/// The decoded bytes of `{placement.path}`, into a buffer the",
			"\t/// caller owns. Nothing here allocates, so the capacity is a",
			f"\t/// parameter -- {ratio[0]}:{ratio[1]}, so the value is that"
			" much smaller",
			"\t/// than the wire form. A short buffer is refused rather than",
			"\t/// half-filled.",
			*([f"\tpub const {name.upper()}_DECODED_MAX: usize = {bound};", ""]
			  if bound is not None else []),
			f"\tpub fn {name}_decode(&self, out: &mut [u8]) -> Result<usize> {{",
			f"\t\tlet encoded = {encoded};",
			f"\t\tlet need = encoded * {ratio[1]} / {ratio[0]};",
			"",
			"\t\tif out.len() < need {",
			"\t\t\treturn Err(Error::Bounds);",
			"\t\t}",
			"\t\t// SAFETY: the codec is the C implementation (decision 0017)",
			"\t\t// and this is the one call that crosses to it. Both slices",
			"\t\t// are checked above -- the input is this region's own bytes",
			"\t\t// and the output is at least what the ratio says it needs.",
			"\t\tlet written = unsafe {",
			f"\t\t\t{sym}(self.{name}().as_ptr(), (encoded{scale}) as u32,",
			"\t\t\t\tout.as_mut_ptr())",
			"\t\t};",
			f"\t\tOk(written as usize{unscale})",
			"\t}",
		]

	def _extern_decode(self, struct: ResolvedStruct, placement: Placement,
			codec: object, symbol: str, encoded: str) -> list[str]:
		"""The decode of a tier-1 region, through the ABI its `impl` binds."""
		name  = _ident(c_name(local_name(struct, placement)))
		bound = decode_bound(codec, placement)

		return [
			"",
			f"\t/// The decoded bytes of `{placement.path}`, into a buffer the",
			"\t/// caller owns. Nothing here allocates, so the capacity is a",
			"\t/// parameter.",
			"\t///",
			f"\t/// `{placement.codec}` is a tier-1 codec: `{symbol}_decode`"
			" is the",
			"\t/// implementation this schema binds, and its error is its own",
			"\t/// to report (13.1, 13.2a).",
			*([f"\tpub const {name.upper()}_DECODED_MAX: usize = {bound};", ""]
			  if bound is not None else []),
			f"\tpub fn {name}_decode(&self, out: &mut [u8]) -> Result<usize> {{",
			"\t\tlet mut written: u32 = 0;",
			"\t\t// SAFETY: the implementation is the one this schema binds",
			"\t\t// and this is the one call that crosses to it. The input is",
			"\t\t// this region's own bytes and the output is the caller's,",
			"\t\t// both passed with the lengths they actually have.",
			"\t\tlet code = unsafe {",
			f"\t\t\t{symbol}_decode(self.{name}().as_ptr(),"
			f" ({encoded}) as u32,",
			"\t\t\t\tout.as_mut_ptr(), out.len() as u32, &mut written)",
			"\t\t};",
			"",
			"\t\tif code != 0 {",
			"\t\t\treturn Err(situ_rt::Error::from_code(code));",
			"\t\t}",
			"\t\tOk(written as usize)",
			"\t}",
		]

	def _codec_externs(self) -> list[str]:
		"""Decoders this module calls, declared once at module scope."""
		wanted = sorted({
			held.codec for struct in self.resolved.structs.values()
			for held in own_members(struct)
			if held.codec and self._decodes(held)})

		# And the tier-1 ones, under the symbol their `impl` binds (13.2a).
		tier_one = sorted({
			symbol for struct in self.resolved.structs.values()
			for held in own_members(struct)
			if held.codec and held.kind == "coded"
			and (symbol := extern_symbol(self.schema, held.codec)) is not None})

		if not wanted and not tier_one:
			return []

		return [
			"",
			# `//` and not `///`: a doc comment on an `extern` block is an
			# `unused_doc_comments` warning, and warnings are errors here.
			"// The codec implementations, which are C's (decision 0017).",
			'extern "C" {',
			*[f"\tfn situ_{c_name(name)}_decode(input: *const u8,"
			  f" {'bits' if decode_counts_bits(self.codecs[name]) else 'len'}:"
			  " u32, out: *mut u8) -> u32;" for name in wanted],
			*[f"\tfn {symbol}_decode(input: *const u8, in_len: u32,"
			  " out: *mut u8, out_cap: u32, out_len: *mut u32) -> u32;"
			  for symbol in tier_one],
			"}",
		]

	def _decodes(self, placement: Placement) -> bool:
		codec = self.codecs.get(placement.codec or "")
		return codec is not None and decodes_here(codec)

	def _arm_accessors(self, struct: ResolvedStruct) -> list[str]:
		"""Each variant arm's members, guarded by the discriminant (9.6)."""
		lines: list[str] = []
		for variant in own_members(struct):
			if variant.kind != "variant":
				continue
			for arm, member in arm_members(struct, variant):
				if member is not None:
					lines.extend(self._arm_member(struct, variant, arm, member))
		return lines

	def _arm_member(self, struct: ResolvedStruct, variant: Placement,
			arm: Arm, placement: Placement) -> list[str]:
		"""One arm member, as a `Result`: the arm may not be the one there.

		`Error::Version` is what an unrecognised discriminant gets, and
		reading an arm that is not present is the same mistake from the other
		end.
		"""
		held = self._over_fields(struct, variant.discriminant or "", "self")
		if arm.value is None:
			matched = matched_values(variant)
			if not matched:
				return []
			test = " || ".join(f"{held} == {one.value}" for one in matched)
		else:
			test = f"{held} != {arm.value}"

		name   = _ident(c_name(local_name(struct, placement)))
		scalar = placement.scalar
		start  = self._offset_expression(struct, placement)
		if start is None:
			return []

		head = [
			"",
			f"\t/// `{placement.path}`, present when the discriminant selects",
			f"\t/// `{arm.source or arm.value}`; `Error::Version` otherwise.",
		]
		refuse = [f"\t\tif {self._unparen(test)} {{",
		          "\t\t\treturn Err(Error::Version);",
		          "\t\t}"]

		# `data_sized` in the guard, not just the two spellings that name a
		# count: `i32 run[n + 1]` sets neither `array_count` nor `sized_by`,
		# so an arm that is a run of values looked exactly like a scalar arm
		# and got a getter for the first element. The same sentence 26.47
		# wrote about the ordinary member dispatch, in the parallel one an
		# arm has.
		if scalar is not None and placement.array_count is None \
				and placement.sized_by is None \
				and not data_sized(placement):
			return [
				*head,
				f"\tpub fn {name}(&self) -> Result<{self._rust_type(scalar)}> {{",
				*refuse,
				# `as` the field's type: `read_be` hands back a `u64` and
				# the ordinary getter casts the same way.
				f"\t\tOk({self._unparen(self._raw_load(placement, scalar))}"
				f" as {self._rust_type(scalar)})",
				"\t}",
			]

		if scalar is not None and indexed_elements(placement):
			# A run of values wider than a byte. The slice below is not
			# available to it -- the element is ValueConverted, so the bytes
			# are not the values -- so it is the count and the indexed getter
			# an ordinary run gets, each of them a `Result` because the arm
			# may not be the one present.
			width  = scalar.bits // BITS_PER_BYTE
			length = (self._length_expression(struct, placement)
			          if placement.array_count is None
			          else str(placement.array_count * width))
			if length is not None:
				rtype = self._rust_type(scalar)
				load  = self._load(placement, scalar,
				                   offset=f"{self._unparen(start)}"
				                          f" + index * {width}")
				return [
					*head,
					f"\tpub fn {_ident(name + '_count')}(&self)"
					" -> Result<usize> {",
					*refuse,
					f"\t\tlet at = {self._unparen(start)};",
					f"\t\tOk(core::cmp::min({self._unparen(length)},",
					"\t\t\tself.bytes.len().saturating_sub(at))"
					f" / {width})",
					"\t}",
					"",
					f"\t/// Element `index` of `{placement.path}`. No slice",
					"\t/// accessor: the element is ValueConverted, so bytes",
					"\t/// handed back whole would not be the values.",
					f"\tpub fn {name}(&self, index: usize)"
					f" -> Result<{rtype}> {{",
					f"\t\tif index >= self.{_ident(name + '_count')}()? {{",
					"\t\t\treturn Err(Error::Bounds);",
					"\t\t}",
					f"\t\tOk({self._unparen(load)})",
					"\t}",
				]

		if scalar is not None and scalar.bits == BITS_PER_BYTE:
			# A constant count is a length too, and `_length_expression`
			# answers only for the ones the data decides -- so `u8
			# gateway[4]`, an ICMP redirect's whole payload, got no accessor
			# and, unlike C++ next door, no note either. Silence about a
			# member is the one thing a reader cannot ask about.
			length = (self._length_expression(struct, placement)
			          if placement.array_count is None
			          else str(placement.array_count))
			if length is None:
				return [*head, f"\t// ...and its length is not one this"
				        " backend can compute."]
			return [
				*head,
				f"\tpub fn {name}(&self) -> Result<&[u8]> {{",
				*refuse,
				f"\t\tlet at = {self._unparen(start)};",
				# Clamped the way every other run is. Unclamped this did not
				# hand out too many bytes, which is what the same hole cost the
				# other three -- it panicked, on a DNS label declaring 55 bytes
				# in a five-byte frame. A panic is the safe end of that
				# spectrum and is still a generated accessor killing the
				# caller's process over bytes the caller did not choose.
				f"\t\tlet n  = core::cmp::min({self._unparen(length)},",
				"\t\t\tself.bytes.len().saturating_sub(at));",
				"\t\tOk(&self.bytes[at..at + n])",
				"\t}",
			]

		# A struct-typed arm: its members belong to its type, so handing back
		# one of those is the whole of the work.
		nested = self.resolved.structs.get(placement.type_name or "")

		# A *variable-size* one, measured from its own bytes. C has emitted
		# this since variants landed and the other three demanded a single
		# size -- while `validate` called the accessor regardless, so a schema
		# with such an arm produced a module that does not compile. MQTT is
		# four of them: CONNECT, PUBLISH, SUBSCRIBE and UNSUBSCRIBE all end in
		# something the data sizes (26.55).
		if nested is not None and not nested.layout.is_fixed_size \
				and has_computable_extent(self.resolved.structs, nested):
			inner = _pascal(nested.name)
			base  = c_name(local_name(struct, placement))
			return [
				*head,
				# How many bytes this arm occupies, for the switch that places
				# whatever follows the variant. The length chain names it and
				# only the ordinary nested member emitted one, so the first
				# schema with a variable-size arm named a method nothing
				# defines -- MQTT's CONNECT, three times over.
				f"\tpub fn {_ident(base + '_extent')}(&self) -> usize {{",
				f"\t\tlet at = {self._unparen(start)};",
				"\t\tif self.bytes.len() < at {",
				"\t\t\treturn 0;",
				"\t\t}",
				f"\t\t{inner} {{ bytes: &self.bytes[at..] }}.extent()",
				"\t}",
				"",
				f"\tpub fn {name}(&self) -> Result<{inner}<'_>> {{",
				*refuse,
				f"\t\tlet at = {self._unparen(start)};",
				"\t\tif self.bytes.len() < at {",
				"\t\t\treturn Err(Error::Bounds);",
				"\t\t}",
				"",
				"\t\t// Twice: once over what is left, to give the extent",
				"\t\t// something to measure, and once at the size it says.",
				f"\t\tlet whole = {inner} {{ bytes: &self.bytes[at..] }};",
				"\t\tlet size  = whole.extent();",
				"\t\tif self.bytes.len() - at < size {",
				"\t\t\treturn Err(Error::Bounds);",
				"\t\t}",
				f"\t\tOk({inner} {{ bytes: &self.bytes[at..at + size] }})",
				"\t}",
			]

		if nested is not None and nested.layout.is_fixed_size:
			inner = _pascal(nested.name)
			return [
				*head,
				f"\tpub fn {name}(&self) -> Result<{inner}<'_>> {{",
				*refuse,
				f"\t\tlet at = {self._unparen(start)};",
				# The same bounds question a nested member asks, asked here
				# too: an arm sits at an offset the discriminant chose, and
				# slicing to `at + SIZE` on a frame that stops earlier is a
				# panic rather than an `Err`.
				f"\t\tif self.bytes.len() < at"
				f" || self.bytes.len() - at < {inner}::SIZE {{",
				"\t\t\treturn Err(Error::Bounds);",
				"\t\t}",
				f"\t\t{inner}::new(&self.bytes[at..at + {inner}::SIZE])",
				"\t}",
			]
		return []

	def _getter(self, struct: ResolvedStruct, entry: Resolved) -> list[str]:
		placement = entry.placement
		scalar    = placement.scalar

		kind = classify(struct, placement, self.structs)

		if kind is Member.OPAQUE:
			return self._opaque(struct, placement)
		if kind is Member.TAG:
			return self._tag(struct, placement)
		if kind is Member.MARKER:
			return self._marker(struct, placement)
		if kind is Member.RESERVED:
			return ["",
			        f"\t// {placement.path} is reserved: no accessor, and",
			        "\t// validate() holds it to the declared pattern."]
		if kind is Member.CODED:
			return self._coded_region(struct, placement)
		if kind is Member.TLV:
			return self._tlv_region(struct, placement)
		if kind is Member.INDEXED:
			return self._indexed_region(struct, placement)
		if kind is Member.LOCATED:
			return self._located(struct, placement)
		if kind is Member.REPEAT_WHILE:
			return self._repeat_while(struct, placement)
		if kind is Member.DELIMITED:
			lines = self._delimited(struct, placement)
			if placement.codec is not None:
				lines.extend(self._coded_delimited(struct, placement))
			return lines
		if kind is Member.RECORD_RUN:
			return self._record_run(struct, placement)
		if kind is Member.VARIABLE:
			return self._variable(struct, placement)
		if kind is Member.UNPLACED or kind is Member.REGION:
			# A sealed region has no accessor of its own: its interior is behind
			# the gate, which is emitted below. The fallthrough note read as a
			# missing feature while sitting directly above the thing that supports
			# it -- the same contradiction the coded-region note had.
			if placement.kind == "sealed":
				return ["",
				        f"\t// {placement.path} is sealed by"
				        f" {placement.codec}: it has no accessor",
				        f"\t// of its own, and its interior is reached through the"
				        " gate below,",
				        f"\t// which opens only once the tag has verified (14.3)."]

			if placement.kind == "variant":
				# Not a gap: a variant has no accessor of its own because
				# there is no one thing to hand back, and its arms are
				# emitted above. The fallthrough note said "not in the static
				# subset yet", which reads as a missing feature.
				return ["",
				        f"\t// {placement.path} is a variant: exactly one arm",
				        "\t// is present, and each is above behind the",
				        "\t// discriminant that selects it. The variant itself",
				        "\t// has no accessor."]
			return ["", f"\t// {placement.path}: not in the static subset yet."]
		if kind is Member.VARINT:
			return self._varint_field(struct, placement)
		if kind is Member.NOTHING:
			return []

		name = _ident(local_name(struct, placement))

		if kind is Member.NESTED:
			nested = _pascal(placement.type_name or "")
			inner  = self.resolved.structs.get(placement.type_name or "")
			base   = c_name(local_name(struct, placement))
			at     = self._offset_expression(struct, placement)

			if inner is not None and not inner.layout.is_fixed_size \
					and at is not None:
				# The slice ran to the end of the buffer, which the inner
				# struct's own accessors survive -- they bound themselves --
				# so nothing crashed and nothing compiled wrong. What broke is
				# the member *after* it: with no extent to add, its offset
				# could not be resolved and it was silently left out of the
				# generated module entirely.
				#
				# `extent` is emitted on a stricter condition than this one,
				# so where the inner struct cannot be measured at all these
				# call a method that does not exist -- caught by rustc rather
				# than by anyone reading, which is the only mercy in it.
				if not has_computable_extent(self.resolved.structs, inner):
					return [
						"",
						f"\t// No accessor for {placement.path}: one "
						f"`{placement.type_name}` has no",
						"\t// extent this backend can compute, so nothing"
						" can say where it ends.",
					]
				return [
					"",
					f"\t/// How many bytes {placement.path} occupies here, read",
					"\t/// from its own contents.",
					f"\tpub fn {_ident(f'{base}_extent')}(&self) -> usize {{",
					f"\t\t{nested} {{ bytes: &self.bytes[({at})..] }}.extent()",
					"\t}",
					"",
					f"\t/// {placement.path}, sized from its own contents.",
					"\t///",
					"\t/// `Err(Bounds)` where the frame does not contain it:",
					"\t/// every accessor on the result trusts that its own",
					"\t/// bytes are all here, which is 20.2's acquisition",
					"\t/// check one level in. C has refused this since phase",
					"\t/// 4 and the other three could not (26.31).",
					f"\tpub fn {name}(&self) -> Result<{nested}<'_>> {{",
					f"\t\tlet at = {at};",
					f"\t\tlet n  = self.{_ident(f'{base}_extent')}();",
					"\t\tif self.bytes.len() < at || self.bytes.len() - at < n {",
					"\t\t\treturn Err(Error::Bounds);",
					"\t\t}",
					f"\t\tOk({nested} {{ bytes: &self.bytes[at..at + n] }})",
					"\t}",
				]

			# A fixed-size nested struct at whatever offset the members before
			# it leave. This asked the placement for a constant one and
			# crashed the compiler on the assertion inside `offset_bytes` --
			# for a header, a variable-length field and another header, which
			# is as ordinary as a protocol gets. C reads its own offset
			# function here and always has.
			if at is None:
				return ["",
				        f"\t// No accessor for {placement.path}: this backend",
				        "\t// cannot resolve where it starts."]

			return [
				"",
				f"\t/// {placement.path} at {placement.offset_bytes}."
				if placement.offset_bits is not None else
				f"\t/// {placement.path}, at an offset the message decides.",
				"\t///",
				"\t/// `Err(Bounds)` where the frame does not contain it (26.31).",
				f"\tpub fn {name}(&self) -> Result<{nested}<'_>> {{",
				f"\t\tlet at = {self._unparen(at)};",
				f"\t\tif self.bytes.len() < at"
				f" || self.bytes.len() - at < {nested}::SIZE {{",
				"\t\t\treturn Err(Error::Bounds);",
				"\t\t}",
				f"\t\tOk({nested} {{ bytes: &self.bytes[at..] }})",
				"\t}",
			]

		if kind is Member.TEXT_NUMBER:
			return self._fixed_text_number(struct, placement)
		if kind is Member.ARRAY:
			if scalar is None or scalar.bits != BITS_PER_BYTE:
				return self._struct_array(struct, placement)
			count = placement.array_count or 0
			start = placement.offset_bytes
			lines = [
				"",
				f"\t/// {placement.path}: {count} bytes. A slice, so the length",
				"\t/// travels with the pointer and cannot be lost.",
				f"\tpub fn {name}(&self) -> &[u8] {{",
				# Empty where they are not all here, for the reason the text
				# digits above give: a struct built over what is left of a
				# short message has no acquiring check behind it.
				f"\t\tif self.bytes.len() < {start + count} {{",
				"\t\t\treturn &[];",
				"\t\t}",
				f"\t\t&self.bytes[{start}..{start + count}]",
				"\t}",
			]
			if any(attr.name == "nul_terminated" for attr in placement.attrs):
				lines.extend([
					"",
					f"\t/// Content length: to the first zero byte, or {count}.",
					f"\tpub fn {name}_len(&self) -> usize {{",
					f"\t\tsitu_rt::nul_len(self.{name}())",
					"\t}",
				])
			return lines

		if scalar is None:
			return ["", f"\t// {placement.path}: not in the static subset yet."]

		# A member after a variable-length one is placed at run time. Its
		# arithmetic is the struct's own walk, and only where the read is
		# measured from differs.
		offset = (None if placement.offset_bits is not None
		          else self._offset_expression(struct, placement))

		# The layout solver refuses a bit-packed field at a dynamic offset
		# outright, so this asserts that rule rather than declaring a gap. If
		# the rule is relaxed it fires here instead of emitting a wrong bit
		# offset, which is undetectable at run time.
		assert not (placement.offset_bits is None and scalar.is_bit_packed), \
			f"{placement.path}: bit-packed at a dynamic offset"

		if placement.offset_bits is None and offset is None:
			return ["", f"\t// {placement.path}: its offset cannot be resolved."]

		if placement.since is not None and placement.version_field is not None:
			# `Result` rather than the value: there is nothing to return when
			# the field is not there, and returning the bytes that follow
			# would hand back another member's. `#[must_use]` means ignoring
			# the refusal does not compile clean.
			version = _ident(c_name(placement.version_field))
			return [
				"",
				*self._axes_doc(entry),
				f"\t/// Present from version {placement.since}. An older"
				" message does not",
				"\t/// carry these bytes, so this reports rather than reading"
				" what does.",
				f"\tpub fn {name}(&self) -> Result<{self._field_type(placement)}> {{",
				f"\t\tif self.{version}() < {placement.since} {{",
				"\t\t\treturn Err(Error::Version);",
				"\t\t}",
				*self._versioned_bounds(placement),
				f"\t\tOk({self._load(placement, scalar, offset)})",
				"\t}",
			]

		# A scalar whose offset the message decides answers zero where it does
		# not fit. Rust's alternative is the panic inside `read_be`, which is
		# an abort in `no_std` -- a denial of service rather than a wrong
		# answer, and neither is what a caller wants (26.27).
		fits = self._fits(struct, placement,
		                  max(1, (scalar.bits + BITS_PER_BYTE - 1)
		                      // BITS_PER_BYTE))

		return [
			"",
			*self._axes_doc(entry),
			*([] if fits is None else [
				"\t/// Zero where the member does not fit: its offset is a sum",
				"\t/// of lengths the message chose, and `validate` reports",
				"\t/// such a message as malformed.",
			]),
			f"\tpub fn {name}(&self) -> {self._field_type(placement)} {{",
			*([f"\t\t{self._load(placement, scalar, offset)}"] if fits is None
			  else [
				f"\t\tif !({fits}) {{",
				# `None` where the field is an enum: the guard was written
				# for an integer getter and `return 0` does not typecheck
				# against `Option<T>`. An enum-typed member behind a
				# delimited one is the shape that has both.
				f"\t\t\treturn {self._nothing(placement)};",
				"\t\t}",
				f"\t\t{self._load(placement, scalar, offset)}",
			]),
			"\t}",
		]

	def _register(self, struct: ResolvedStruct) -> list[str]:
		"""A memory-mapped register (section 15).

		A `Word` is a copy of the bits and the register is a place on a bus,
		and keeping them apart is what the headline asks for: a partial-width
		field in a `no_rmw` register cannot be written alone, so composing the
		whole word and writing it once is the only shape the API has.

		The transport is `read_volatile`/`write_volatile` through a raw
		pointer, which is where a Rust program's `unsafe` lives -- marked at
		the call site rather than buried, because a reader auditing this needs
		to see it.
		"""
		info = struct.layout.register
		assert info is not None

		name  = _pascal(struct.name)
		word  = self._rust_type_for_bits(info.width)
		lines = [
			f"/// register {struct.name}"
			+ (f" at {info.address:#x}" if info.address is not None else "")
			+ f": {info.width} bits, {info.access_width}-bit bus access"
			+ (", volatile" if info.volatile else "")
			+ (", no read-modify-write" if info.no_rmw else "") + ".",
			"#[derive(Debug, Clone, Copy, PartialEq, Eq)]",
			f"pub struct {name}Word({word});",
			"",
			f"impl {name}Word {{",
			f"\tpub const fn new(raw: {word}) -> Self {{",
			"\t\tSelf(raw)",
			"\t}",
			"",
			f"\tpub const fn raw(self) -> {word} {{",
			"\t\tself.0",
			"\t}",
		]

		for entry in own_entries(struct):
			lines.extend(self._register_field(entry, name, word))

		lines.extend(["}", ""])

		lines.extend([
			f"/// The register itself: a place on a bus, not a value.",
			f"pub struct {name} {{",
			"	base: *mut u8,",
			"}",
			"",
			f"impl {name} {{",
		])
		if info.address is not None:
			lines.append(f"	pub const ADDRESS: usize = {info.address:#x};")
		lines.extend([
			f"	pub const WIDTH: usize = {info.width};",
			"",
			"	/// # Safety",
			"	///",
			"	/// `base` must address this device's register block, and stay",
			"	/// valid for as long as this value does. Nothing situ knows can",
			"	/// check that -- it is the one thing the caller has to promise.",
			"	pub const unsafe fn new(base: *mut u8) -> Self {",
			"		Self { base }",
			"	}",
			"",
			f"	pub fn read(&self) -> {name}Word {{",
			"		// SAFETY: the pointer came from `new`, whose contract is that",
			"		// it addresses this block.",
			f"		{name}Word(unsafe {{ self.word().read_volatile() }})",
			"	}",
			"",
			f"	pub fn write(&self, value: {name}Word) {{",
			"		// SAFETY: as above.",
			"		unsafe { self.word().write_volatile(value.raw()) }",
			"	}",
		])

		for entry in own_entries(struct):
			lines.extend(self._register_action(entry, name, word))

		lines.extend([
			"",
			f"	fn word(&self) -> *mut {word} {{",
			f"		// SAFETY: the offset is the register's declared address.",
			f"		(unsafe {{ self.base.add({info.address or 0:#x}) }}) as *mut {word}",
			"	}",
			"}",
			"",
		])
		return lines

	def _register_field(self, entry: Resolved, owner: str,
			word: str) -> list[str]:
		placement = entry.placement
		scalar    = placement.scalar

		if placement.kind == "reserved":
			return ["",
			        f"\t// {placement.path} is reserved: no accessor, and its",
			        "\t// bits ride through a compose untouched."]
		if scalar is None:
			return []

		name  = _ident(placement.path.rsplit(".", 1)[-1])
		base  = c_name(placement.path.rsplit(".", 1)[-1])
		mode  = placement.access_mode or ast.AccessMode.RW
		shift = placement.offset_bits or 0
		mask  = (1 << scalar.bits) - 1
		lines = ["", f"\t/// {placement.path}: {mode.value},"
		         f" bits {shift}..{shift + scalar.bits - 1}"]

		if mode.readable:
			lines.extend([
				f"\tpub const fn {name}(self) -> {word} {{",
				f"\t\t(self.0 >> {shift}) & {mask:#x}",
				"\t}",
			])
		else:
			lines.append(f"\t// No {name}(): the mode is {mode.value}, so a read"
			             f" returns nothing the field holds.")

		if mode.writable and mode.is_assignment:
			lines.extend([
				f"\tpub const fn {_ident('with_' + base)}(self, value: {word})"
				f" -> Self {{",
				f"\t\tSelf((self.0 & !({mask:#x} << {shift}))"
				f" | ((value & {mask:#x}) << {shift}))",
				"\t}",
			])
		elif mode.writable:
			lines.append(f"\t// No with_{base}(): `{mode.value}` is not an"
			             f" assignment; see the register's own method.")
		else:
			lines.append(f"\t// No with_{base}(): the mode is {mode.value}.")
		return lines

	def _register_action(self, entry: Resolved, owner: str,
			word: str) -> list[str]:
		"""A write that is not an assignment: `w1c`, and `on_write`."""
		placement = entry.placement
		scalar    = placement.scalar
		if scalar is None or placement.kind == "reserved":
			return []

		name  = c_name(placement.path.rsplit(".", 1)[-1])
		mode  = placement.access_mode or ast.AccessMode.RW
		shift = placement.offset_bits or 0
		mask  = (1 << scalar.bits) - 1

		if placement.on_write is not ast.SideEffect.NONE:
			return [
				"",
				f"\t/// {placement.path} has on_write ="
				f" {placement.on_write.value}: the write is the event.",
				f"\tpub fn {_ident('trigger_' + name)}(&self) {{",
				f"\t\tself.write({owner}Word({mask:#x} << {shift}))",
				"\t}",
			]
		if mode is ast.AccessMode.W1C:
			return [
				"",
				f"\t/// {placement.path} is w1c: writing a one clears it, and",
				"\t/// every other bit stays zero so neighbours are untouched.",
				f"\tpub fn {_ident('clear_' + name)}(&self) {{",
				f"\t\tself.write({owner}Word({mask:#x} << {shift}))",
				"\t}",
			]
		return []

	def _rust_type_for_bits(self, bits: int) -> str:
		return f"u{_storage_width(bits)}"

	def _sealed(self, struct: ResolvedStruct) -> list[Placement]:
		return [entry.placement for entry in struct.entries
		        if entry.placement.kind == "sealed"]

	def _gate_opens(self, struct: ResolvedStruct) -> list[str]:
		"""The only thing that hands out a gate, and only once verified."""
		lines: list[str] = []

		for region in self._sealed(struct):
			name  = c_name(local_name(struct, region))
			gate  = _pascal(f"{struct.name}_{name}_gate")
			lines.extend([
				"",
				f"\t/// Open {region.path}, and only if the tag has verified.",
				"\t///",
				f"\t/// `{gate}` has a private field, so nothing outside this",
				"\t/// module can construct one and this is the only function",
				"\t/// that does. Section 14.3, as a type rather than a rule.",
				f"\tpub fn {_ident('open_' + name)}(&self, verified: bool)"
				f" -> Result<{gate}<'_>> {{",
				"\t\tif !verified {",
				"\t\t\treturn Err(Error::Tag);",
				"\t\t}",
				f"\t\tOk({gate} {{ bytes: self.bytes }})",
				"\t}",
			])
		return lines

	def _region_runs(self, struct: ResolvedStruct) -> list[str]:
		"""A run of records inside a region, walked from out here.

		Everything else in a sealed region is reached through the gate, and a
		run is too -- but how far it *reaches* is not a question about the
		plaintext: it places the members after the region, and the tag
		covering it has to span it. `own_entries` drops a dotted path, so this
		module had no walk while its own accessors named one. C and C++ emit
		the same family for the same reason.
		"""
		lines: list[str] = []
		for entry in struct.entries:
			placement = entry.placement
			if placement.kind != "field":
				continue
			# A `coded` region's interior as well as a `sealed` one's: the
			# region names its own length through its members, and a run
			# is measured by a walk whichever kind of region it is in.
			# Keyed on `sealed_by` alone, a run inside a `coded` region
			# named a span nothing emitted -- the same defect one
			# container over, found the day that container joined the
			# composed space.
			inside = (placement.sealed_by is not None
			          or (placement.codec is not None
			              and "." in placement.path[len(struct.name) + 1:]))
			if not inside:
				continue
			if placement.type_name not in self.structs:
				continue
			if not is_counted_run(self.resolved.structs, placement) \
					and placement.repeat_while is None:
				continue
			lines.extend(self._variable(struct, placement))
		return lines

	def _gates(self, struct: ResolvedStruct) -> list[str]:
		lines: list[str] = []

		for region in self._sealed(struct):
			name = c_name(local_name(struct, region))
			gate = _pascal(f"{struct.name}_{name}_gate")

			# Not `offset_bits is not None`: a scalar behind a variable-length
			# member *inside* the region has an offset the message decides,
			# and dropping it left the member unreachable here and in C++
			# while C read it through the gate.
			inside = [entry for entry in struct.entries
			          if entry.placement.sealed_by == region.name
			          and entry.placement.kind == "field"
			          # Not a field of an element of a run: the element type
			          # has its own accessors and the walk is how a caller
			          # reaches one. Emitted here it read element zero at the
			          # region's base, under the run's name.
			          and "[]" not in entry.placement.path]

			lines.extend([
				f"/// {region.path}: reachable only through a verified open.",
				"///",
				"/// The field below is private to this module, so no code",
				"/// outside can make one of these. Parsing attacker-controlled",
				"/// plaintext before authenticating it is not discouraged here;",
				"/// it does not compile.",
				f"pub struct {gate}<'a> {{",
				"	bytes: &'a [u8],",
				"}",
				"",
				f"impl<'a> {gate}<'a> {{",
			])

			# Rendered for the enclosing struct, then moved into the gate's
			# vocabulary once, here, rather than in each emitter below: a
			# fragment transformed and then embedded is a fragment a later
			# pass rewrites again, and three sites patching the spellings
			# each had met is how C++ came to leave `label_span()`,
			# `n_len()` and `n_value()` behind (26.51).
			interior = {_ident(self._gate_name(struct, entry.placement))
			            for entry in inside}
			for entry in inside:
				lines.extend(self._in_gate(struct, interior,
				                           self._gated(struct, entry)))

			if not inside:
				lines.append("	// Nothing in this region has an accessor.")

			lines.extend(["}", ""])
		return lines

	def _gate_name(self, struct: ResolvedStruct, placement: Placement) -> str:
		"""What an interior member is called on the gate.

		The member's local name with the region's stripped, which is the same
		derivation C spells out in full -- `situ_s_body_run_a_get` -- and what
		C++ and the differential drivers now ask for. This took the *last*
		path component, which is the same answer for a member of the region
		and a different one for anything deeper: a field of an element of a
		run inside the region came out as `a()`, with nothing left to say
		which run it belongs to, and two runs in one region would have
		collided on it.
		"""
		local  = c_name(local_name(struct, placement))
		region = c_name(placement.sealed_by or "")
		if region and local.startswith(f"{region}_"):
			return local[len(region) + 1:]
		return local

	def _in_gate(self, struct: ResolvedStruct, interior: set[str],
			lines: list[str]) -> list[str]:
		"""One accessor, moved from the struct's vocabulary into the gate's.

		Everything the expression machinery renders is written for the
		enclosing struct: `self.label_span()`, `self.n_value()`,
		`self.n_len()`. Inside the gate `self` is the gate, which has none of
		them -- so the module stopped compiling the moment an interior
		member's offset or length depended on anything but a constant. Four
		names, one cause, and the next schema would have found a fifth.

		The gate holds the same slice the enclosing struct does -- `open_`
		hands it `self.bytes` -- so a struct rebuilt from it answers exactly
		what the enclosing one would, and the accessors keep their meaning
		rather than being re-derived here.

		The *outer* members only, minus any name the region declares itself:
		`n()` inside the gate is the gate's `n()` where the interior has one,
		and reaching for the enclosing struct would be reaching for a member
		that is not there.
		"""
		stems = {_ident(c_name(local_name(struct, held)))
		         for held in own_members(struct)} - interior
		if not stems:
			return lines

		owner   = f"{_pascal(struct.name)} {{ bytes: self.bytes }}"
		pattern = "|".join(re.escape(stem)
		                   for stem in sorted(stems, key=len, reverse=True))
		# The suffix is left open rather than listed: `_span`, `_span_from`,
		# `_value`, `_len`, `_offset`, `_count` and `_extent` are the ones
		# reached so far, and a list of them is a list to fall behind. What
		# makes this safe is the stem set, not the suffix.
		def through(hit: re.Match[str]) -> str:
			return f"{owner}.{hit.group(1)}("

		return [re.sub(rf"self\.((?:{pattern})(?:_[a-z0-9_]+)?)\(", through, line)
		        for line in lines]

	def _gated_elements(self, struct: ResolvedStruct, placement: Placement,
			scalar: ScalarType) -> list[str]:
		"""A run of values wider than a byte, inside a sealed region.

		No slice, for the reason there is none outside a gate either: the
		element is ValueConverted, so the bytes are not the values. The count
		and the indexed getter, read through the gate's own view -- which this
		backend emitted for neither spelling, leaving the interior of a sealed
		`u16 x[n]` unreachable while C handed its elements out through a plain
		view (14.3).
		"""
		name  = _ident(self._gate_name(struct, placement))
		width = scalar.bits // BITS_PER_BYTE
		start = self._offset_expression(struct, placement)
		length = (str(placement.array_count * width)
		          if placement.array_count is not None
		          else self._length_expression(struct, placement))
		if start is None or length is None:
			return ["", f"\t// {placement.path}: this backend cannot resolve"
			        " its extent."]

		rtype = self._rust_type(scalar)
		load  = self._load(placement, scalar,
		                   f"{self._unparen(start)} + index * {width}")
		return [
			"",
			f"\tpub fn {_ident(name + '_count')}(&self) -> usize {{",
			f"\t\tlet at = {self._unparen(start)};",
			f"\t\tcore::cmp::min({self._unparen(length)},",
			f"\t\t\tself.bytes.len().saturating_sub(at)) / {width}",
			"\t}",
			"",
			f"\t/// Element `index`. No slice accessor: the element is",
			"\t/// ValueConverted, so bytes handed back whole would not be",
			"\t/// the values.",
			f"\tpub fn {name}(&self, index: usize) -> Result<{rtype}> {{",
			f"\t\tif index >= self.{_ident(name + '_count')}() {{",
			"\t\t\treturn Err(Error::Bounds);",
			"\t\t}",
			f"\t\tOk({self._unparen(load)})",
			"\t}",
		]

	def _gated(self, struct: ResolvedStruct, entry: Resolved) -> list[str]:
		placement = entry.placement
		scalar    = placement.scalar
		name      = _ident(self._gate_name(struct, placement))

		if any(attr.name == "secret" for attr in placement.attrs):
			return ["",
			        f"\t// {placement.path} is [secret]: no accessor is",
			        "\t// generated for it at all (section 14.6)."]

		if scalar is None:
			return []

		if placement.array_count is not None or placement.sized_by is not None:
			if indexed_elements(placement):
				return self._gated_elements(struct, placement, scalar)
			if scalar.bits != BITS_PER_BYTE:
				return []

			# The offset may be the message's: a region that begins after a
			# delimited member starts wherever the scan ends, and so does
			# everything in it. This asked the placement for a constant and
			# raised the assertion inside `offset_bytes` -- a traceback out of
			# the compiler, where section 17 asks for a diagnostic. The scalar
			# branch below has read the expression since the interior grew
			# dynamic offsets; this one was never told.
			where = (str(placement.offset_bytes)
			         if placement.offset_bits is not None
			         else self._offset_expression(struct, placement))
			if where is None:
				return ["", f"\t// {placement.path}: this backend cannot resolve",
				        "\t// where it starts."]
			start = self._unparen(where)

			if placement.array_count is not None:
				count = placement.array_count
				return [
					"",
					f"\tpub fn {name}(&self) -> &[u8] {{",
					# Empty where they are not all here: a gate is handed the
					# region's bytes, and a frame cut short inside the region
					# leaves fewer of them than the schema declares.
					*self._holds(start, count, "&[]"),
					f"\t\t&self.bytes[{start}..{start} + {count}]",
					"\t}",
				]

			# A length the data decides, read through the gate's own view --
			# the driving field is plaintext at the same offsets, which is why
			# reading it here is not a reference to transform output (13.3).
			# This refused it as "not emitted yet" while the other three
			# emitted it, so a sealed payload was reachable in three languages
			# and not in this one.
			holder = self.resolved.structs.get(placement.path.split(".")[0])
			length = (None if holder is None else
			          self._length_expression(holder, placement))
			if length is None:
				return ["", f"\t// {placement.path}: this backend cannot"
				        " resolve its length."]

			return [
				"",
				f"\tpub fn {name}(&self) -> &[u8] {{",
				f"\t\tlet n = core::cmp::min({self._unparen(length)},",
				f"\t\t\tself.bytes.len().saturating_sub({start}));",
				f"\t\t&self.bytes[{start}..{start} + n]",
				"\t}",
			]

		at = (None if placement.offset_bits is not None
		      else self._offset_expression(struct, placement))
		if placement.offset_bits is None and at is None:
			return ["", f"\t// {placement.path}: this backend cannot resolve",
			        "\t// where it starts."]

		# Bounded where the offset is the message's, as every other dynamic
		# read is: `read_be` indexes the slice, so an unbounded one is a panic
		# rather than a wrong answer -- and the gate is the place where the
		# caller has already been told the bytes are trustworthy.
		width = max(1, (scalar.bits + BITS_PER_BYTE - 1) // BITS_PER_BYTE)
		guard = ([] if at is None else [
			f"\t\tif self.bytes.len().saturating_sub({self._unparen(at)})"
			f" < {width} {{",
			"\t\t\treturn 0;",
			"\t\t}",
		])
		return [
			"",
			f"\tpub fn {name}(&self) -> {self._field_type(placement)} {{",
			*guard,
			f"\t\t{self._load(placement, scalar, at and self._unparen(at))}",
			"\t}",
		]

	# -- delimited members (section 8.6) --------------------------------

	def _scan_call(self, placement: Placement, slice_expr: str) -> str:
		delim = placement.delimiter
		assert delim is not None
		bytes_ = "b\"" + "".join(f"\\x{byte:02x}" for byte in delim) + "\""

		if placement.delimiter_quote is None and placement.delimiter_escape is None:
			return f"situ_rt::scan({slice_expr}, {bytes_})"

		quote  = (f"{placement.delimiter_quote}"
		          if placement.delimiter_quote is not None else "situ_rt::NO_BYTE")
		escape = (f"{placement.delimiter_escape}"
		          if placement.delimiter_escape is not None else "situ_rt::NO_BYTE")
		return (f"situ_rt::scan_relaxed({slice_expr}, {bytes_}, "
		        f"{quote}, {escape})")

	def _scan_slice(self, placement: Placement, start: str) -> str:
		"""The bytes the scan may look at: to the cap, or to the end.

		The start is clamped as well as the end. `start` is a sum of length
		fields the message chooses, so it can exceed the slice -- and
		`&bytes[start..]` panics before any limit is applied, which is
		memory-safe and is still a message an attacker sends to stop the
		process. C++ read out of bounds on the same input; this panicked;
		Python returned a wrong number. All three now answer as C does, which
		is an empty scan.
		"""
		begin = f"core::cmp::min({start}, self.bytes.len())"
		if placement.delimiter_cap is None:
			return f"&self.bytes[{begin}..]"
		return (f"&self.bytes[{begin}..core::cmp::min("
		        f"{begin} + {placement.delimiter_cap}, self.bytes.len())]")

	def _delimited(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""The same three numbers every backend gives.

		A slice carries its own length, so `len` here is the content and the
		accessor hands back exactly it -- there is no pointer to pair with a
		count and no way to lose one. `span` is content plus delimiter, which
		is what the next member's offset is computed from.
		"""
		assert placement.delimiter is not None
		name  = _ident(local_name(struct, placement))
		base  = c_name(local_name(struct, placement))
		delim = placement.delimiter
		start = self._offset_expression(struct, placement)
		if start is None:
			return ["", f"\t// {placement.path}: this backend cannot resolve"
			        " where the scan starts."]

		sliced = self._scan_slice(placement, start)
		# Saturating. `start` is a sum of length fields the message chooses,
		# so it can exceed the frame -- `u16 n` claiming 65535 in a ten-byte
		# view puts the scan base past the end, and an unsaturating
		# subtraction then hands `scan` about four billion bytes to search.
		# Measured as an AddressSanitizer SEGV before this line changed; the
		# C backend has been saturating here since the `[remaining]` fix and
		# these three were not.
		room   = f"self.bytes.len().saturating_sub({start})"
		limit  = (room if placement.delimiter_cap is None
		          else f"core::cmp::min({placement.delimiter_cap}, {room})")

		sliced_at = self._scan_slice(placement, "at")
		room_at   = "self.bytes.len().saturating_sub(at)"
		limit_at  = (room_at if placement.delimiter_cap is None
		             else f"core::cmp::min({placement.delimiter_cap}, {room_at})")

		# With `[trim]` the framing and the value are different numbers.
		scan = _ident(f"{base}_raw_len" if placement.trimmed else f"{base}_len")

		lines = [
			"",
			f"\t/// `{placement.path}` runs to the first"
			f" {render_delimiter(delim)}.",
			f"\tpub fn {_ident(f'{base}_offset')}(&self) -> usize {{",
			*(self._offset_body(struct, placement)
			  or [f"\t\t{self._unparen(start)}"]),
			"\t}",
			"",
			"\t/// The scan, from a base the caller already knows. Every loop",
			"\t/// that sums offsets has one, and the plain form below",
			"\t/// re-derives it by rescanning everything before this member.",
			f"\tpub fn {scan}_from(&self, at: usize) -> usize {{",
			f"\t\t{self._scan_call(placement, sliced_at)}",
			"\t}",
			"",
			f"\tpub fn {scan}(&self) -> usize {{",
			f"\t\tself.{scan}_from(self.{_ident(f'{base}_offset')}())",
			"\t}",
			"",
			f"\tpub fn {_ident(f'{base}_terminated_from')}(&self, at: usize)"
			" -> bool {",
			f"\t\tself.{scan}_from(at) < {limit_at}",
			"\t}",
			"",
			f"\tpub fn {_ident(f'{base}_span_from')}(&self, at: usize)"
			" -> usize {",
			f"\t\tself.{scan}_from(at) + "
			f"if self.{_ident(f'{base}_terminated_from')}(at)"
			f" {{ {len(delim)} }} else {{ 0 }}",
			"\t}",
			"",
			"\t/// Whether the delimiter is there. It is not when the frame was",
			"\t/// cut short, which is the only thing parse can catch here.",
			f"\tpub fn {_ident(f'{base}_terminated')}(&self) -> bool {{",
			f"\t\tself.{_ident(f'{base}_terminated_from')}"
			f"(self.{_ident(f'{base}_offset')}())",
			"\t}",
			"",
			"\t/// Content plus delimiter: where the next member starts.",
			f"\tpub fn {_ident(f'{base}_span')}(&self) -> usize {{",
			f"\t\tself.{scan}() + if self."
			f"{_ident(f'{base}_terminated')}() {{ {len(delim)} }} else {{ 0 }}",
			"\t}",
			"",
			"\t/// The member's bytes, before anything is trimmed.",
			f"\tpub fn {_ident(f'{base}_raw')}(&self) -> &[u8] {{",
			f"\t\tlet at = self.{_ident(f'{base}_offset')}();",
			f"\t\t&self.bytes[at..at + self.{scan}()]",
			"\t}",
		]

		if placement.trimmed:
			lines.extend([
				"",
				"\t/// `[trim]`: the whitespace at either end is framing rather",
				"\t/// than value, so the span above is unchanged.",
				f"\tpub fn {_ident(f'{base}_len')}(&self) -> usize {{",
				f"\t\tsitu_rt::trim(self.{_ident(f'{base}_raw')}()).len()",
				"\t}",
			])

		value = (f"situ_rt::trim(self.{_ident(f'{base}_raw')}())"
		         if placement.trimmed else f"self.{_ident(f'{base}_raw')}()")

		if placement.radix is not None:
			return lines + self._text_number(struct, placement, value)

		fold = "situ_rt::ascii_ci_eq" if placement.case_insensitive else None
		return lines + [
			"",
			f"\tpub fn {name}(&self) -> &[u8] {{",
			f"\t\t{value}",
			"\t}",
			"",
			f"\t/// Whether `{placement.path}` is a given token, compared "
			+ ("folding ASCII case." if fold else "byte for byte."),
			"\t///",
			"\t/// The length is part of the comparison, so a prefix is not a",
			"\t/// match.",
			f"\tpub fn {_ident(f'{base}_eq')}(&self, other: &[u8]) -> bool {{",
			(f"\t\t{fold}({value}, other)" if fold
			 else f"\t\t{value} == other"),
			"\t}",
		]

	def _text_number(self, struct: ResolvedStruct, placement: Placement,
			value: str) -> list[str]:
		"""Digits, read through a `Result` that cannot be dropped.

		`#[must_use]` on `Result` is what makes this backend's version of the
		claim stronger than C's: there, ignoring the error is a warning nobody
		enabled; here it does not compile.
		"""
		assert placement.radix is not None
		scalar = placement.scalar
		assert scalar is not None

		name  = _ident(local_name(struct, placement))
		base  = c_name(local_name(struct, placement))
		rtype = self._field_type(placement, writing=True)
		limit = (1 << scalar.bits) - 1

		return [
			"",
			f"\t/// `{placement.path}`: digits, in the range of {scalar.name}.",
			"\t///",
			"\t/// The only accessor here that can fail. Every other conversion",
			"\t/// is total; a decimal parse is not, and returning 0 for `12x4`",
			"\t/// would hand back a number nobody wrote.",
			f"\tpub fn {name}(&self) -> Result<{rtype}> {{",
			f"\t\tlet raw = {value};",
			"",
			f"\t\tmatch situ_rt::parse_uint(raw, {placement.radix}, {limit}) {{",
			f"\t\t\tSome(value) => Ok(value as {rtype}),",
			"\t\t\tNone => Err(Error::Constraint),",
			"\t\t}",
			"\t}",
			"",
			"\t/// The same digits where an error cannot be returned: the offset",
			"\t/// arithmetic after this field is not fallible. `validate`",
			"\t/// refuses a frame whose digits are not digits, so a validated",
			"\t/// one always parses here.",
			f"\tpub fn {_ident(f'{base}_value')}(&self) -> {rtype} {{",
			f"\t\tself.{name}().unwrap_or(0)",
			"\t}",
		]

	def _nested_text_values(self, struct: ResolvedStruct) -> list[str]:
		"""The non-failing read of a *nested* text number.

		A member of a nested struct can drive a length -- `u8
		name[header.namesize]` in a cpio entry -- and an expression over it
		names a method on *this* type. Nothing emitted it.
		"""
		lines: list[str] = []
		for entry in struct.entries:
			placement = entry.placement
			scalar    = placement.scalar
			if placement.radix is None or placement.offset_bits is None:
				continue
			# Nested *or* the struct's own. Restricting this to nested
			# members assumed the fixed-width form beside it emitted its own
			# `_value`, and it does not: `decimal u32 n[4]; u16 d[n]` named a
			# helper nothing defined. Every text driver in `examples/` is
			# either delimited or nested, which are the two forms that had it.
			if scalar is None or placement.array_count is None:
				continue

			name  = _ident(c_name(local_name(struct, placement)))
			rtype = self._field_type(placement, writing=True)
			limit = (1 << scalar.bits) - 1
			at    = placement.offset_bits // BITS_PER_BYTE
			lines.extend([
				"",
				f"\t/// `{placement.path}`, where an error cannot be returned:",
				"\t/// the offset arithmetic after it is not fallible, and",
				"\t/// `validate` refuses a frame whose digits are not digits.",
				f"\tpub fn {name}_value(&self) -> {rtype} {{",
				# Zero where the digits are not all here, which is what the
				# runtime's reads answer and what this could not: an extent
				# function builds a struct over whatever bytes are left, so
				# this ran with an empty slice behind it and panicked --
				# an abort in `no_std`, over a message somebody else chose.
				f"\t\tif self.bytes.len() < {at + placement.array_count} {{",
				"\t\t\treturn 0;",
				"\t\t}",
				f"\t\tlet raw = &self.bytes[{at}..{at + placement.array_count}];",
				"",
				f"\t\tmatch situ_rt::parse_uint(raw, {placement.radix},"
				f" {limit}) {{",
				f"\t\t\tSome(value) => value as {rtype},",
				"\t\t\tNone => 0,",
				"\t\t}",
				"\t}",
			])
		return lines

	def _over_fields(self, struct: ResolvedStruct, source: str,
			held: str) -> str:
		names = [entry.placement.name for entry in struct.entries
		         if entry.placement.scalar is not None
		         and "." not in entry.placement.path[len(struct.name) + 1:]]
		# `as usize` on every read, and Rust is right to demand it. A length
		# field is narrow -- `hdr_ext_len` is a u8 -- and `(len + 1) * 8` in
		# u8 arithmetic is 255 + 1 = 0, then zero. C computes the same
		# expression correctly only because integer promotion widens it to
		# `int` first, which is a rule this backend does not have and a
		# guarantee C stops giving above 16 bits.
		# An enum-typed field's getter hands back `Option<T>`, because
		# section 8.7 admits a value no member names -- and `Option<T> as
		# usize` is not a cast Rust has. So an expression over one reads the
		# backing bytes directly, which is what the expression means anyway:
		# a discriminant is compared against numbers.
		#
		# Every use of this was wrong for an enum discriminant -- the extent
		# chain, the `default: error` check, the arm guards -- so a schema
		# with `case K.a:` did not compile at all. No Rust test had one.
		# Own members only, and keyed by the same filter `names` uses. Keyed
		# by every entry, a *nested* member with the same name won -- entries
		# are in layout order and the deeper one comes later -- so a variant
		# switching on `nlmsg_type` read the `nlmsg_type` of the `nlmsghdr`
		# echoed inside its own error arm, twenty bytes further on. Every arm
		# guard and the whole extent chain were reading the wrong field.
		by_path = {local_name(struct, placement): placement
		           for placement in readable_names(struct)}
		# Constants too. A `const` is a compile-time value and the renderer
		# only rewrote *fields*, so `align_up(HEADER_BYTES + n, 4)` reached
		# the target as `HEADER_BYTES` -- an identifier that exists in the
		# schema and in no generated file. Folding it here keeps the emitted
		# arithmetic the arithmetic the schema wrote.
		consts = self.resolved.layout.env.consts

		def read(name: str) -> str:
			if name in consts:
				return str(consts[name])
			held_at = by_path[name]
			# A text number is digits, not bytes of an integer: reading it
			# where it sits parses ASCII as a binary integer, which is a
			# plausible number nobody wrote.
			if "." in name and held_at.radix is not None:
				return f"({held}.{_ident(c_name(name))}_value() as usize)"
			# A nested member has no accessor of this struct's own -- `at
			# file.pixel_offset` in `examples/bmp` -- and its offset is a
			# constant here, so it is read where it sits. Same spelling as
			# the enum case below, which needs the bytes for its own reason.
			if "." in name or (held_at.type_name in self.enums
			                   and held_at.scalar is not None):
				assert held_at.scalar is not None
				# The backing bytes, because the getter hands back an
				# `Option` and nothing compares that to a number. At a
				# dynamic offset there is no constant to read them at -- a
				# discriminant behind a delimited member is exactly that --
				# and asking the placement for one asserted out of the
				# compiler.
				at = (None if held_at.offset_bits is not None
				      else self._offset_expression(struct, held_at))
				if held_at.offset_bits is None and at is None:
					raise UnknownName(name)
				raw = self._raw_load(held_at, held_at.scalar,
				                     at and self._unparen(at))
				return f"({self._unparen(raw)} as usize)"
			# A varint's own getter reports a truncated encoding; `_value` is
			# the read that cannot fail, which is what the count form uses --
			# and a text number's does too, which only the nested branch
			# above knew.
			if held_at.varint is not None or held_at.radix is not None:
				return f"({held}.{_ident(c_name(name) + '_value')}() as usize)"
			return f"({held}.{_ident(c_name(name))}() as usize)"

		return expand_calls(over_fields([*by_path, *consts], source, read),
		                    rust_spelling)


	def _run_index(self, struct: ResolvedStruct, placement: Placement,
			walk: list[str], cond: str, inner: str) -> list[str]:
		"""The second family for a run: element offsets, resolved once (0022).

		C's shape rather than Python's list, because this backend is `no_std`
		and there is no allocator to lean on. `max N` bounds the array, and a
		run without one gets a note saying what to add. Three languages, one
		decision, three constructs -- which is what it means for the family to
		be the consumer's rather than the schema's.
		"""
		if not self.materialize:
			return []

		cap  = placement.repeat_cap or placement.delimiter_cap
		name = c_name(local_name(struct, placement))
		if cap is None:
			return [
				"",
				f"\t// No index for {placement.path}: the run has no `max`, so",
				"\t// how many offsets to hold is not a number this knows and",
				"\t// the array would have to be allocated. Add `max N`.",
			]

		kind = _pascal(f"{struct.name}_{name}_index")
		return [
			"",
			f"\t/// Where each element of `{placement.path}` starts.",
			"\t///",
			"\t/// The map calls this run `access = Sequential`: reaching",
			"\t/// element N means reading the N-1 before it, so the indexed",
			"\t/// accessor above walks from the base on every call. Building",
			"\t/// this is one pass; every lookup after it is arithmetic.",
			f"\tpub fn {_ident(name + '_indexed')}(&self) -> {kind} {{",
			f"\t\tlet mut out = {kind} {{ count: 0, start: [0; {cap} + 1] }};",
			"",
			*walk,
			"\t\t\tout.start[n] = at;",
			"\t\t\tat += size;",
			"\t\t\tn  += 1;",
			f"\t\t\tif !({cond}) {{",
			"\t\t\t\tbreak;",
			"\t\t\t}",
			f"\t\t\tif n == {cap} {{",
			"\t\t\t\tbreak;",
			"\t\t\t}",
			"\t\t}",
			"",
			"\t\tout.count = n;",
			"\t\tout.start[n] = at;",
			"\t\tout",
			"\t}",
			"",
			"\t/// Element `index`, in constant time: arithmetic rather than a",
			"\t/// walk. Calling the walking accessor here would build an index",
			"\t/// and then ignore it.",
			f"\tpub fn {_ident(name + '_at')}(&self, idx: &{kind},"
			" index: usize)",
			f"\t\t\t-> Result<{inner}<'_>> {{",
			"\t\tif index >= idx.count {",
			"\t\t\treturn Err(Error::Bounds);",
			"\t\t}",
			f"\t\tOk({inner} {{",
			"\t\t\tbytes: &self.bytes[idx.start[index]..idx.start[index + 1]],",
			"\t\t})",
			"\t}",
		]

	def _run_index_type(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""The index's own type, at module scope rather than inside `impl`."""
		if not self.materialize:
			return []
		cap = placement.repeat_cap or placement.delimiter_cap
		if cap is None:
			return []

		name = c_name(local_name(struct, placement))
		kind = _pascal(f"{struct.name}_{name}_index")
		return [
			"",
			f"/// Element offsets for `{placement.path}`, held by the caller.",
			"///",
			"/// One more than `count`: the last entry is where the run ends,",
			"/// so an element's size is the gap to its neighbour.",
			"#[derive(Debug, Clone, Copy)]",
			f"pub struct {kind} {{",
			"\tpub count: usize,",
			f"\tpub start: [usize; {cap} + 1],",
			"}",
		]

	def _repeat_while(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""A run ending after the element that fails a condition (8.6.6)."""
		element = self.resolved.structs.get(placement.type_name or "")
		if element is None or self._extent_expression(element) is None:
			return ["", f"\t// No accessors for {placement.path}: one"
			        f" `{placement.type_name}` has no extent",
			        "\t// this backend can compute."]

		base  = c_name(local_name(struct, placement))
		inner = _pascal(placement.type_name or "")
		start = self._offset_expression(struct, placement)
		if start is None:
			return ["", f"\t// {placement.path}: this backend cannot resolve"
			        " where the run starts."]

		cond = self._over_fields(element, placement.repeat_while or "", "element")
		cap  = ("" if placement.repeat_cap is None
		        else f" && n < {placement.repeat_cap}")

		def walk_from(from_: str) -> list[str]:
			return [
			f"\t\tlet mut at = {from_};",
			"\t\tlet mut n  = 0usize;",
			"",
			f"\t\twhile at < self.bytes.len(){cap} {{",
			f"\t\t\tlet element = {inner} {{ bytes: &self.bytes[at..] }};",
			"\t\t\tlet size = element.extent();",
			"\t\t\tif size == 0 || at + size > self.bytes.len() {",
			"\t\t\t\tbreak;",
			"\t\t\t}",
			]

		walk = walk_from(start)
		tail = [
			"\t\t\tat += size;",
			"\t\t\tn  += 1;",
			f"\t\t\tif !({cond}) {{",
			"\t\t\t\tbreak;",
			"\t\t\t}",
			"\t\t}",
		]

		return [
			"",
			f"\t/// `{placement.path}` is a run of `{placement.type_name}`"
			f" ending after",
			f"\t/// the element for which `{placement.repeat_while}` is false.",
			"\t/// That element is part of the run: the condition is asked",
			"\t/// about it once it has been read.",
			f"\tpub fn {_ident(f'{base}_count')}(&self) -> usize {{",
			*walk, *tail,
			"\t\tn",
			"\t}",
			"",
			f"\tpub fn {_ident(base)}(&self, index: usize) -> Result<{inner}<'_>> {{",
			*walk,
			"\t\t\tif n == index {",
			f"\t\t\t\treturn Ok({inner} {{ bytes: &self.bytes[at..at + size] }});",
			"\t\t\t}",
			*tail,
			"\t\tErr(Error::Bounds)",
			"\t}",
			"",
			"\t/// The walk, from a base the caller already knows: the same",
			"\t/// helper every delimited member has, for the same reason.",
			f"\tpub fn {_ident(f'{base}_span_from')}(&self, start: usize)"
			" -> usize {",
			*walk_from("start"), *tail,
			"\t\tlet _ = n;",
			"\t\tat - start",
			"\t}",
			"",
			f"\tpub fn {_ident(f'{base}_span')}(&self) -> usize {{",
			f"\t\tself.{_ident(f'{base}_span_from')}({start})",
			"\t}",
			*self._run_index(struct, placement, walk, cond, inner),
		]

	def _record_run(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""A run of records, ending where the terminator would be an element."""
		assert placement.delimiter is not None
		element = self.resolved.structs.get(placement.type_name or "")
		if element is None or self._extent_expression(element) is None:
			return ["", f"\t// No accessors for {placement.path}: one"
			        f" `{placement.type_name}` has no extent",
			        "\t// this backend can compute, so the run cannot be walked."]

		base  = c_name(local_name(struct, placement))
		delim = placement.delimiter
		inner = _pascal(placement.type_name or "")
		start = self._offset_expression(struct, placement)
		if start is None:
			return ["", f"\t// {placement.path}: this backend cannot resolve"
			        " where the run starts."]

		bytes_ = "b\"" + "".join(f"\\x{byte:02x}" for byte in delim) + "\""

		def walk_from(from_: str) -> list[str]:
			return [
			f"\t\tlet mut at = {from_};",
			"\t\tlet mut n  = 0usize;",
			"",
			f"\t\twhile at + {len(delim)} <= self.bytes.len() {{",
			f"\t\t\tif &self.bytes[at..at + {len(delim)}] == {bytes_} {{",
			"\t\t\t\tbreak;",
			"\t\t\t}",
			f"\t\t\tlet element = {inner} {{ bytes: &self.bytes[at..] }};",
			"\t\t\tlet size = element.extent();",
			"\t\t\tif size == 0 || at + size > self.bytes.len() {",
			"\t\t\t\t// A zero-extent element would loop here forever, and",
			"\t\t\t\t// one past the end was never in this frame.",
			"\t\t\t\tbreak;",
			"\t\t\t}",
			]

		walk = walk_from(start)

		return [
			"",
			f"\t/// `{placement.path}` is a run of `{placement.type_name}`,"
			f" ending where",
			f"\t/// {render_delimiter(delim)} stands in for one. Walked, not"
			" indexed: a view",
			"\t/// borrows the caller's slice and nothing here allocates, so",
			"\t/// there is nowhere to keep a table of offsets.",
			f"\tpub fn {_ident(f'{base}_count')}(&self) -> usize {{",
			*walk,
			"\t\t\tat += size;",
			"\t\t\tn  += 1;",
			"\t\t}",
			"\t\tn",
			"\t}",
			"",
			f"\tpub fn {_ident(base)}(&self, index: usize) -> Result<{inner}<'_>> {{",
			*walk,
			"\t\t\tif n == index {",
			f"\t\t\t\treturn Ok({inner} {{ bytes: &self.bytes[at..at + size] }});",
			"\t\t\t}",
			"\t\t\tat += size;",
			"\t\t\tn  += 1;",
			"\t\t}",
			"\t\tErr(Error::Bounds)",
			"\t}",
			"",
			"\t/// Every element plus the terminator: where the next member",
			"\t/// starts. Where the run ran out of buffer there is none to add.",
			f"\tpub fn {_ident(f'{base}_span_from')}(&self, start: usize)"
			" -> usize {",
			*walk_from("start"),
			"\t\t\tat += size;",
			"\t\t\tn  += 1;",
			"\t\t}",
			"\t\tlet _ = n;",
			f"\t\tif at + {len(delim)} <= self.bytes.len() {{",
			f"\t\t\tat += {len(delim)};",
			"\t\t}",
			"\t\tat - start",
			"\t}",
			"",
			f"\tpub fn {_ident(f'{base}_span')}(&self) -> usize {{",
			f"\t\tself.{_ident(f'{base}_span_from')}({start})",
			"\t}",
		]

	def _delimiter_checks(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""The delimiter is there, and a text number's digits are digits.

		Terminated first: for a frame cut short before the digits both are
		wrong, and "this frame stops early" is the more useful answer. The
		other three report it in that order too.
		"""
		if "." in placement.path[len(struct.name) + 1:]:
			return []		# checked under the element's own struct

		base  = c_name(local_name(struct, placement))
		lines = [
			f"\t\tif !self.{_ident(f'{base}_terminated')}() {{",
			"\t\t\treturn Err(Error::Constraint);",
			"\t\t}",
		]
		if placement.radix_minimal:
			value = (f"situ_rt::trim(self.{_ident(f'{base}_raw')}())"
			         if placement.trimmed
			         else f"self.{_ident(f'{base}_raw')}()")
			lines.extend([
				f"\t\tif !situ_rt::digits_minimal({value}, {placement.radix}) {{",
				"\t\t\treturn Err(Error::Constraint);",
				"\t\t}",
			])
		if placement.radix is not None:
			# Reading it is the check. `?` rather than a match, because the
			# error is already the right one and `Result` cannot be dropped.
			lines.append(f"\t\tself.{_ident(local_name(struct, placement))}()?;")
		return lines

	def _required(self, struct: ResolvedStruct) -> list[str]:
		"""Framing: is a whole message here, and if not how many bytes? (20.3)

		Returns `Framing` rather than `Result<usize>`, because both arms carry
		a number and they mean different things -- one is the length, the
		other a lower bound on it. A `Result` would have to drop one or
		smuggle it through the error.

		An associated function over a slice: framing is asked before there is
		a struct to ask. The length expressions read through `self`, so it
		builds one over the prefix, which is a slice and costs nothing.
		"""
		if struct.layout.register is not None:
			return []

		name = _pascal(struct.name)
		head = [
			"",
			"\t/// How many bytes a whole one needs, given `data` of them.",
			"\t///",
			"\t/// `Complete(n)` when one is present and `n` bytes long;",
			"\t/// `Need(n)` when not, with `n` a lower bound on the total.",
			"\tpub fn required(data: &[u8]) -> situ_rt::Framing {",
			"\t\tlet have = data.len();",
		]

		if struct.layout.is_fixed_size and struct.layout.is_byte_sized:
			return [*head,
			        "\t\tif have < Self::SIZE {",
			        "\t\t\treturn situ_rt::Framing::Need(Self::SIZE);",
			        "\t\t}",
			        "\t\tsitu_rt::Framing::Complete(Self::SIZE)",
			        "\t}"]

		parts = extent_parts(self.resolved.structs, struct)
		if parts is None:
			return self._unframeable(struct)

		constant, variable = parts
		if constant == 0 and not variable:
			return self._unframeable(struct,
			        "a complete one can be zero bytes, so every buffer already"
			        " holds one")

		steps: list[str] = []
		for placement in variable:
			if placement.kind == "indexed":
				return self._unframeable(struct, "an `indexed` region reaches"
				        " wherever its furthest element ends, which its offset"
				        " table does not say")
			if is_run(placement, self.structs):
				walk = self._framing_walk(struct, placement)
				if walk is None:
					return self._unframeable(struct, "a run whose element"
					        " cannot be framed cannot be framed either")
				steps.extend(walk)
				continue

			length = self._length_expression(struct, placement)
			if length is None:
				return self._unframeable(struct)

			local = _ident(c_name(local_name(struct, placement)) + "_terminated")
			steps.extend([
				"",
				f"\t\t// {placement.path}: reading its length means reading",
				"\t\t// bytes that have to be here first.",
				"\t\tif have < at {",
				"\t\t\treturn situ_rt::Framing::Need(at);",
				"\t\t}",
			])
			if placement.delimiter is not None:
				steps.extend([
					f"\t\tif !probe.{local}() {{",
					"\t\t\t// The delimiter is not in what we have, and how"
					" much more",
					"\t\t\t// is the sender's to know. One byte is the honest"
					" bound.",
					"\t\t\treturn situ_rt::Framing::Need(have + 1);",
					"\t\t}",
				])
			steps.append(
				f"\t\tat += {self._unparen(length).replace('self.', 'probe.')};")

		return [
			*head,
			# `mut` only where something adds to it. A schema whose only
			# variable member is a versioned one resolves entirely to
			# constants, and `unused_mut` is an error under `-D warnings`.
			f"\t\tlet {'mut ' if steps else ''}at = {constant};",
			"",
			"\t\tif have < Self::SIZE_MIN {",
			"\t\t\treturn situ_rt::Framing::Need(Self::SIZE_MIN);",
			"\t\t}",
			"",
			*([
				"\t\t// A struct over what has arrived, so every length below",
				"\t\t// reads through the same bounds the accessors do.",
				f"\t\tlet probe = {name} {{ bytes: data }};",
			] if any("probe." in line for line in steps) else []),
			*steps,
			"",
			"\t\tif have < at {",
			"\t\t\treturn situ_rt::Framing::Need(at);",
			"\t\t}",
			"\t\tsitu_rt::Framing::Complete(at)",
			"\t}",
		]

	def _framing_walk(self, struct: ResolvedStruct,
			placement: Placement) -> list[str] | None:
		"""Framing a run: one element at a time, through the element's own
		`required`.

		The walk the accessors use stops at the end of the bytes as readily as
		at the end of the run, and those are opposite answers to "is a whole
		one here?". The element's own framing is what separates them, being
		the same question one level down.
		"""
		element = self.resolved.structs.get(placement.type_name or "")
		if element is None or not frameable(self.resolved.structs, element):
			return None

		inner = _pascal(element.name)
		read  = [
			f"\t\t\tlet part = match {inner}::required(&data[at..]) {{",
			"\t\t\t\tsitu_rt::Framing::Complete(n) => n,",
			"\t\t\t\tsitu_rt::Framing::Need(n) =>",
			"\t\t\t\t\treturn situ_rt::Framing::Need(at + n),",
			"\t\t\t};",
		]
		cap  = placement.repeat_cap if placement.repeat_while else None
		body: list[str] = []

		if placement.repeat_while is not None:
			cond = self._over_fields(element, placement.repeat_while or "",
			                         "element")
			body.extend([
				*read,
				f"\t\t\tlet element = {inner} {{"
				" bytes: &data[at..at + part] };",
				"\t\t\tat += part;",
				"",
				"\t\t\t// The condition is asked about the element just"
				" read, which",
				"\t\t\t// is the whole difference from a delimiter -- and"
				" only once",
				"\t\t\t// that element is known to be entirely here.",
				f"\t\t\tif !({cond}) {{",
				"\t\t\t\tbreak;",
				"\t\t\t}",
				*([] if cap is None else [
					"\t\t\tseen += 1;",
					f"\t\t\tif seen == {cap} {{",
					"\t\t\t\tbreak;",
					"\t\t\t}",
				]),
			])
		else:
			delim = placement.delimiter
			assert delim is not None
			bytes_ = "b\"" + "".join(f"\\x{byte:02x}" for byte in delim) + "\""
			body.extend([
				f"\t\t\tif have < at + {len(delim)} {{",
				f"\t\t\t\treturn situ_rt::Framing::Need(at + {len(delim)});",
				"\t\t\t}",
				"",
				"\t\t\t// The terminator only terminates where an element"
				" would",
				"\t\t\t// start. It belongs to this member, as a delimiter"
				" does.",
				f"\t\t\tif &data[at..at + {len(delim)}] == {bytes_} {{",
				f"\t\t\t\tat += {len(delim)};",
				"\t\t\t\tbreak;",
				"\t\t\t}",
				"",
				*read,
				"\t\t\tat += part;",
			])

		loop = ["\t\tloop {", *body, "\t\t}"]
		if cap is not None:
			loop = ["\t\t{", "\t\t\tlet mut seen = 0usize;",
			        *[f"\t{line}" if line else line for line in loop],
			        "\t\t}"]

		return [
			"",
			f"\t\t// {placement.path}: a run of `{element.name}`, framed one",
			"\t\t// element at a time -- the walk the accessors use cannot"
			" tell the",
			"\t\t// end of the run from the end of the bytes, and this asks"
			" each",
			"\t\t// element whether it is whole.",
			*loop,
		]

	def _unframeable(self, struct: ResolvedStruct,
			why: str | None = None) -> list[str]:
		if struct.layout.register is not None:
			return []
		reason = why or (
			"it ends where the view ends, so how long one is is the"
			" transport's answer rather than the message's"
			if any(held.sized_by == "remaining" for held in own_members(struct))
			else "one of its members has no length this can compute")
		return [
			"",
			f"\t// No `required`: {reason}.",
			"\t// Framing such a message is the layer below's job.",
		]

	def _extent_expression(self, struct: ResolvedStruct) -> str | None:
		"""How many bytes one instance of a variable struct occupies."""
		# The arithmetic and the refusals are shared
		# (traverse.extent_parts); rendering one length is Rust's business.
		# A fixed-size element still has an extent: it is the size. Only
		# variable structs reach `extent_parts`, which says so by returning
		# None for a fixed one -- so a `while` run over `struct e { u8 k; u8
		# pad; }` got no walk at all, and every member after the run went on
		# calling the span function nobody emitted. C computes this from the
		# size directly and was the only backend that built such a schema.
		if struct.layout.is_fixed_size and struct.layout.is_byte_sized:
			return str(int(struct.layout.size_bytes))

		parts = extent_parts(self.resolved.structs, struct)
		if parts is None:
			return None
		constant, variable = parts

		terms = [str(constant)]
		for placement in variable:
			length = self._length_expression(struct, placement)
			if length is None:
				return None
			terms.append(length)
		return " + ".join(terms)

	def _extent_method(self, struct: ResolvedStruct) -> list[str]:
		"""Emitted only for a type something walks a run of."""
		# A run walks them and a nested member sizes its slice from one -- and
		# a *count*-driven run of elements with no single size walks them too,
		# which this did not name (26.36).
		def walks(holder: ResolvedStruct, entry: Resolved) -> bool:
			placement = entry.placement
			if placement.type_name != struct.name:
				return False
			if classify(holder, placement, self.structs) in (
					Member.RECORD_RUN, Member.REPEAT_WHILE, Member.NESTED,
					Member.INDEXED):
				return True
			# `data_sized`, not `sized_by`: the arithmetic spelling of a
			# count is a count, and reading only the bare one left a run
			# of variable records without the `extent` its own walk calls.
			return (data_sized(placement)
			        and not struct.layout.is_fixed_size)

		if not any(walks(other, entry)
		           for other in self.resolved.structs.values()
		           for entry in other.entries):
			return []

		extent = self._extent_expression(struct)
		if extent is None:
			return []

		return [
			"",
			f"\t/// How many bytes one `{struct.name}` occupies. A run of these",
			"\t/// is walked, and the walk needs to know where each one ends.",
			"\tpub fn extent(&self) -> usize {",
			f"\t\t{extent}",
			"\t}",
		]

	def _offset_expression(self, struct: ResolvedStruct,
			placement: Placement) -> str | None:
		"""Where a member starts: the same walk every backend does."""
		if placement.offset_bits is not None:
			return str(placement.offset_bits // BITS_PER_BYTE)

		parts = preceding_parts(struct, placement)
		if parts is None:
			return None

		constant = sum(part for part in parts if isinstance(part, int))
		terms: list[str] = []

		for other in parts:
			if isinstance(other, int):
				continue
			length = self._length_expression(struct, other)
			if length is None:
				return None
			terms.append(length)

		if not terms:
			return str(constant)

		# Saturating, term by term: one of these is a length the message
		# chose, and the slice that follows an out-of-range offset panics --
		# an abort in `no_std`, which is the denial of service rather than the
		# mitigation (26.27).
		folded = str(constant)
		for term in terms:
			# `_unparen`, because `-D warnings` rejects a parenthesised
			# argument and a length expression arrives wrapped.
			folded = (f"situ_rt::advance({folded}, {self._unparen(term)},"
			          " self.bytes.len())")
		return folded

	def _fits(self, struct: ResolvedStruct, placement: Placement,
			bytes_: int) -> str | None:
		"""Whether a fixed-size member at a *dynamic* offset is in the slice.

		None where the question does not arise: a statically placed member is
		inside the frame by the check that made the view (20.2).
		"""
		if placement.offset_bits is not None:
			return None
		start = self._offset_expression(struct, placement)
		if start is None:
			return None
		return (f"self.bytes.len().saturating_sub({start}) >= {bytes_}")

	def _versioned_bounds(self, placement: Placement,
			held: str = "self.bytes") -> list[str]:
		"""And the frame has to hold it, which the acquiring check did not say.

		The one place 20.2's argument does not reach: a versioned struct's
		minimum is its *first* version's, so a message declaring version 2 in
		three bytes is a well-formed question about a member that is not
		there. All four backends asked the version and stopped -- C read past
		the view, this panicked in `read_be`, which `no_std` makes an abort.
		"""
		scalar = placement.scalar
		if scalar is None or placement.offset_bits is None:
			return []
		width = max(1, (scalar.bits + BITS_PER_BYTE - 1) // BITS_PER_BYTE)
		return [
			f"\t\tif {held}.len().saturating_sub({placement.offset_bytes})"
			f" < {width} {{",
			"\t\t\treturn Err(Error::Bounds);",
			"\t\t}",
		]

	def _arm_validation(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""Validate the arm the discriminant selects, through its own type.

		Nothing did, in any backend: a variant's check was the discriminant
		and nothing else, so every constraint inside an arm was declared by
		the schema and enforced by nobody. Through the arm's own accessor,
		which already refuses the arm that is not present -- so an `Err` here
		means "not this arm" and is not the message being wrong.
		"""
		inner = self.resolved.structs.get(placement.type_name or "")
		if inner is None:
			return []
		if not inner.layout.is_fixed_size \
				and not has_computable_extent(self.resolved.structs, inner):
			return []

		# One of them, not a run of them. The arm accessor is emitted for an
		# arm that *is* a struct, and this named it for an arm that is a run
		# of one -- so `validate` called `body_run()` for `vrec run[n + 1]`,
		# which the arm emitter had declined a page earlier. A validator
		# naming what its own backend refused to emit is that refusal
		# arriving as a compile error.
		if not self._is_arm_struct(placement):
			return []

		name = _ident(c_name(local_name(struct, placement)))
		return [
			f"\t\t// {placement.path}: the arm the discriminant selects",
			"\t\t// carries its own constraints, and its own validator is",
			"\t\t// what knows them.",
			f"\t\tif let Ok(arm) = self.{name}() {{",
			"\t\t\tarm.validate()?;",
			"\t\t}",
		]

	def _is_arm_struct(self, placement: Placement) -> bool:
		"""Whether an arm is one struct rather than a run of them.

		The same question C and C++ ask before naming an arm's accessor, and
		the same answer: a bracket after the type makes it a run, whichever
		way the count is written.
		"""
		return (placement.array_count is None
		        and placement.delimiter is None
		        and placement.repeat_while is None
		        and not data_sized(placement))

	def _arm_fits_check(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""The arm the discriminant selects has to fit the frame it is in.

		A variant was outside the length checks in all four backends -- a DNS
		label declaring 55 bytes in a five-byte frame is the same lie
		`u8 opts[hdr.length]` tells. The accessors clamp it (26.35); this is
		the other half, without which clamping turns a lie into a truncation.

		Through the accessor rather than by re-deriving the discriminant test:
		it refuses the arm that is not present and clamps the one that is, so
		a short answer is the mismatch.
		"""
		scalar = placement.scalar
		if placement.sized_by is None or scalar is None:
			return []
		if scalar.bits != BITS_PER_BYTE:
			return []
		declared = self._length_expression(struct, placement)
		if declared is None:
			return []

		name = _ident(c_name(local_name(struct, placement)))
		return [
			f"\t\t// {placement.path}: the arm the discriminant selects has to",
			"\t\t// fit the frame. The accessor clamps; this is where a message",
			"\t\t// that does not fit is called malformed.",
			f"\t\tif let Ok(held) = self.{name}() {{",
			f"\t\t\tif held.len() < ({declared}) {{",
			"\t\t\t\treturn Err(Error::Bounds);",
			"\t\t\t}",
			"\t\t}",
		]

	def _fits_check(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""A length the message declares must fit the frame it is in.

		Nothing checked this in any backend: `u8 opts[hdr.length]` with a
		`u16` length in a 32-byte frame parsed clean. The accessor clamps,
		which is what keeps a caller who skips validation safe; this is what
		tells a caller who does not that the message is malformed rather than
		short. Clamping alone silently turns a lie into a truncation.
		"""
		if "." in placement.path[len(struct.name) + 1:]:
			return []

		# The other half of the same sentence: a member *placed* after a
		# variable-length region has an offset the message chose.
		if not declares_its_own_length(placement):
			extent = (placement.size_bits // BITS_PER_BYTE
			          if placement.is_fixed_size
			          and placement.size_bits % BITS_PER_BYTE == 0 else None)
			fits = (None if not extent
			        else self._fits(struct, placement, extent))
			if fits is None:
				return []
			return [
				f"\t\t// {placement.path}: its offset is a sum of lengths the",
				"\t\t// message chose, so the frame is not known to contain"
				" it.",
				f"\t\tif !({fits}) {{",
				"\t\t\treturn Err(Error::Bounds);",
				"\t\t}",
			]

		declared = self._length_expression(struct, placement)
		start    = self._offset_expression(struct, placement)
		if declared is None or start is None:
			return []
		return [
			f"\t\t// {placement.path}: the length the message declares has to",
			"\t\t// fit the frame it is in.",
			f"\t\tif self.bytes.len().saturating_sub({start}) < ({declared}) {{",
			"\t\t\treturn Err(Error::Bounds);",
			"\t\t}",
		]

	def _discriminant_check(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""`default: error` -- the discriminant must select an arm.

		Section 14.5 says an unrecognised discriminant is rejected on parse,
		and no backend rejected it. It stayed invisible while a variant had no
		computable extent, because nothing walked one.
		"""
		values = matched_values(placement)
		if not values or placement.discriminant is None:
			return []

		held = self._over_fields(struct, placement.discriminant, "self")
		test = " && ".join(f"{held} != {arm.value}" for arm in values)
		named = ", ".join(arm.source or str(arm.value) for arm in values)
		return [
			f"\t\t// {placement.path}: an arm for {named}, and"
			f" `default: error` for the rest.",
			f"\t\tif {test} {{",
			"\t\t\treturn Err(Error::Version);",
			"\t\t}",
		]

	@staticmethod
	def _unparen(expr: str) -> str:
		"""Strip parentheses that enclose the whole expression.

		`unused_parens` is a hard error under `-D warnings`, and it fires on
		the tail expression of a block and on a match arm alike -- so an
		`if`/`else` chain cannot carry a length expression that arrived
		already wrapped, which every one of them does.

		Only a pair that encloses everything: `(a) + (b)` opens and closes
		twice and keeps both.
		"""
		while expr.startswith("(") and expr.endswith(")"):
			depth = 0
			for index, char in enumerate(expr):
				depth += (char == "(") - (char == ")")
				if depth == 0 and index < len(expr) - 1:
					return expr		# the opener closed early
			expr = expr[1:-1]
		return expr

	def _located(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""A member the data positions, reached from the message (9.8).

		Takes the message bytes as well as `self`, because a generated struct
		holds the *frame* slice and a located member is not in it. The
		returned slice borrows the message rather than the frame, which is the
		lifetime the signature has to say out loud.
		"""
		name   = _ident(c_name(local_name(struct, placement)))
		offset = self._over_fields(struct, placement.located or "", "self")
		length = self._length_expression(struct, placement)
		if length is None and placement.is_fixed_size \
				and placement.size_bits % BITS_PER_BYTE == 0:
			length = str(placement.size_bits // BITS_PER_BYTE)
		if length is None:
			return ["",
			        f"\t// No accessor for {placement.path}: it is placed by",
			        f"\t// `{placement.located}`, and how long it is is not",
			        "\t// something this backend can work out."]

		return [
			"",
			f"\t/// {placement.path}, at `{placement.located}` bytes from the",
			"\t/// start of the *message* rather than of this frame.",
			"\t///",
			"\t/// The offset is the message's, so nothing about this frame",
			"\t/// says it is inside the buffer -- checked here, on every",
			"\t/// call, which is what `offset = DataPlaced` in the map costs.",
			f"\tpub fn {name}<'m>(&self, msg: &'m [u8]) -> Result<&'m [u8]> {{",
			# `let x = (expr);` is `unused_parens` too, which is a hard error
			# under `-D warnings`. Same helper as the variant chain: the
			# sub-expressions arrive parenthesised because that is how a
			# generator stays composable, and Rust objects to it everywhere.
			f"\t\tlet at = {self._unparen(offset)};",
			f"\t\tlet n  = {self._unparen(length)};",
			"",
			"\t\tif n > msg.len() || at > msg.len() - n {",
			"\t\t\treturn Err(Error::Bounds);",
			"\t\t}",
			"\t\tOk(&msg[at..at + n])",
			"\t}",
		]

	def _variant_length(self, struct: ResolvedStruct,
			placement: Placement) -> str | None:
		"""How many bytes the selected arm occupies, as one expression.

		A conditional chain rather than a statement `switch`, because callers
		want this inside a sum -- the extent of the struct around it, or the
		offset of whatever follows.

		An unmatched discriminant yields zero, which is not a claim that such
		a message is empty: `default: error` says there is no such message,
		and `validate` is where that is said. Zero is the value the run walk
		already refuses to advance by.
		"""
		if not placement.arm_cases or placement.discriminant is None:
			return None

		held  = self._over_fields(struct, placement.discriminant, "self")
		chain = "0"
		for arm, member in reversed(arm_members(struct, placement)):
			if member is None:
				continue		# `default: error`; falls to the zero above
			if member.is_fixed_size:
				length = str(member.size_bits // BITS_PER_BYTE)
			else:
				rendered = self._length_expression(struct, member)
				if rendered is None:
					return None
				# A branch body is a block and `-D warnings` rejects
				# parentheses around its value, however they got there.
				length = self._unparen(rendered)

			# `if` is an expression here, so this nests the same way the
			# ternary does in C. Built without parentheses of its own and
			# wrapped once at the end: each round puts the previous chain in
			# an `else` block, and a parenthesised block value is the error
			# `_unparen` exists for.
			chain = (length if arm.value is None
			         else f"if {held} == {arm.value} {{ {length} }}"
			              f" else {{ {chain} }}")
		return chain if chain == "0" else f"({chain})"

	def _offset_body(self, struct: ResolvedStruct,
			placement: Placement) -> list[str] | None:
		"""The offset accessor's body, accumulating rather than summing.

		`_offset_expression` builds `0 + a_span() + b_span()`, and each of
		those re-derives its own base by rescanning everything before it, so
		the expression costs far more than the terms in it. Measured on an
		eight-member record: 1590ms, against 57ms once the sum keeps a
		running total. An expression cannot hold one; this is the same sum as
		statements.
		"""
		if placement.offset_bits is not None:
			return None

		parts = preceding_parts(struct, placement)
		if parts is None:
			return None

		lines = ["\t\tlet mut at = 0usize;"]
		for other in parts:
			if isinstance(other, int):
				if other:
					lines.append(f"\t\tat += {other};")
				continue
			length = self._length_expression(struct, other, running="at")
			if length is None:
				return None
			# Saturating, like the expression form. This one was left plain
			# when that was fixed, so `examples/message` with a hostile
			# `rec_count` resolved `trailer` a quarter of a megabyte into a
			# kilobyte slice and panicked -- found by the differential test,
			# which is what an incomplete fix looks like from outside.
			lines.append(f"\t\tat = situ_rt::advance(at,"
			             f" {self._unparen(length)}, self.bytes.len());")
		return [*lines, "\t\tat"]

	def _region_length(self, struct: ResolvedStruct,
			region: Placement) -> str | None:
		"""How many bytes a coded or sealed region occupies, at runtime.

		Only C had this, so the other three could place nothing after such a
		region.
		"""
		rule = region_extent(struct, region,
		                     self.codecs.get(region.type_name), self.resolved.structs)
		if rule is None:
			return None

		terms = [str(rule.constant)]
		for member in rule.variable:
			length = self._length_expression(struct, member)
			if length is None:
				return None
			terms.append(f"({length})")
		inner = " + ".join(terms)

		if rule.kind == "preserving":
			return inner
		if rule.kind == "add":
			return f"({inner}) + {rule.add}"
		if rule.kind == "ratio":
			return f"(({inner}) * {rule.out}) / {rule.into}"
		return (f"((({inner}) + {rule.group_in - 1})"
		        f" / {rule.group_in}) * {rule.group_out}")

	def _length_expression(self, struct: ResolvedStruct,
			placement: Placement, running: str | None = None) -> str | None:
		if placement.kind == "variant":
			return self._variant_length(struct, placement)

		# A run inside a variant arm has no length here, because the arm
		# emitter has no walk: an arm is emitted by a family of its own, and
		# that family reaches a scalar, a byte run, a run of wide values and
		# one fixed-size struct -- never a run of records. Answering anyway
		# named `<arm>_span()`, which nothing defines, so the framing did not
		# compile for `vrec run[n + 1]` in an arm. C and C++ decline the same
		# member for the same reason; the gap is one gap in four backends
		# rather than four answers to one schema.
		if arm_of(struct, placement) is not None \
				and (placement.repeat_while is not None
				     or placement.delimiter is not None
				     or is_counted_run(self.resolved.structs, placement)):
			return None

		# Wherever the delimiter turns out to be. One name for "how far this
		# member reaches", whether it is a byte run or a run of records.
		if placement.delimiter is not None or placement.repeat_while is not None:
			name = c_name(local_name(struct, placement))
			# Every kind that reaches here has the `_from` form: a byte array's
			# scan, a record run's walk and a `while` run's. The runs were the
			# exception, and it cost a rescan of everything before the run on
			# every accumulating pass over it.
			if running is not None:
				return f"self.{_ident(name + '_span_from')}({running})"
			return f"self.{_ident(name + '_span')}()"

		# Arithmetic over a field rather than a reference to one. Without this
		# the member fell through to the scalar case and this backend read one
		# byte and called it the field.
		if is_counted_run(self.resolved.structs, placement):
			base = c_name(local_name(struct, placement))
			return (f"self.{_ident(base + '_span_from')}({running})"
			        if running is not None
			        else f"self.{_ident(base + '_span')}()")

		if placement.size_expr is not None:
			rendered = self._over_fields(struct, placement.size_expr, "self")
			each     = element_bytes(placement)
			return rendered if each == 1 else f"({rendered}) * {each}"

		# A nested struct with no single size. Without this the member after
		# it had no resolvable offset and was dropped from the module.
		inner = self.resolved.structs.get(placement.type_name or "")
		if (inner is not None and not inner.layout.is_fixed_size
				and placement.kind == "field"
				and placement.array_count is None
				and placement.sized_by is None):
			if not has_computable_extent(self.resolved.structs, inner):
				return None		# and so nothing after it can be placed
			name = _ident(c_name(local_name(struct, placement)) + "_extent")
			return f"self.{name}()"
		if placement.kind in ("coded", "sealed"):
			return self._region_length(struct, placement)

		if placement.varint is not None:
			if not self._reads_varint(placement):
				return None
			name = c_name(local_name(struct, placement))
			return f"self.{_ident(name + '_len')}()"

		# An opaque region's size expression is already a byte count: there are
		# no elements to multiply by, and asking for an element width finds no
		# scalar and gives up. C has had this branch all along.
		if placement.kind == "opaque":
			count = self._count_expression(struct, placement)
			return None if count is None else f"({count})"

		if placement.sized_by == "remaining":
			start = self._offset_expression(struct, placement)
			return None if start is None else f"(self.bytes.len() - ({start}))"
		if placement.sized_by is None:
			return None

		# An `indexed` region's count counts entries, and an entry is an
		# `offset_type` wide (`traverse.index_entry_bytes`). Asking the
		# *element* for a width finds a struct with no fixed one and gives up,
		# so the region had no length here and `validate` had no check.
		entry = index_entry_bytes(placement)
		if entry is not None:
			table = self._count_expression(struct, placement)
			return None if table is None else f"({table}) * {entry}"

		count = self._count_expression(struct, placement)
		element = self._element_bytes(placement)
		if count is None or element is None:
			return None
		return f"({count})" if element == 1 else f"({count}) * {element}"

	def _count_expression(self, struct: ResolvedStruct,
			placement: Placement) -> str | None:
		# A count written as arithmetic. `sized_by` holds a path and holds
		# nothing for `x[n + 1]`, so this looked up a driver named "None",
		# found none, and handed its caller a Python `None` -- which C++
		# formatted straight into `return None;`. The expression *is* the
		# count; it is only the bare-reference form that needs a driver
		# looked up (invariant 69, in the fourth place that spells this
		# question two ways).
		if placement.sized_by is None and placement.size_expr is not None:
			return self._over_fields(struct, placement.size_expr, "self")

		driver = self.resolved.find(f"{struct.name}.{placement.sized_by}")
		if driver is None:
			return None

		# A varint driver has no scalar and no constant offset, so neither the
		# guard above nor the load below applies to it. It was refused by the
		# `scalar is None` check, which is why a length-prefixed field -- the
		# thing a varint is usually for -- could not be sized by one.
		if driver.placement.varint is not None:
			if not self._reads_varint(driver.placement):
				return None
			name = _ident(c_name(local_name(struct, driver.placement)) + "_value")
			return f"self.{name}() as usize"

		if driver.placement.scalar is None:
			return None

		# Digits, not bits, and behind the scans of everything before it --
		# so neither the offset check nor the raw load below applies.
		if driver.placement.radix is not None:
			name = _ident(c_name(local_name(struct, driver.placement)) + "_value")
			return f"self.{name}() as usize"

		if driver.placement.offset_bits is None:
			# The driver is itself behind a variable-length member, so there
			# is no constant to read it at -- but its own accessor knows where
			# it is, and this backend has always emitted one. Reading at a
			# static offset was the only thing tried, so the member it sizes
			# was dropped with a note, which is the whole of what "cannot
			# resolve" meant. All three backends had it; C did not.
			name = _ident(c_name(local_name(struct, driver.placement)))
			return f"self.{name}() as usize"
		return (f"{self._raw_load(driver.placement, driver.placement.scalar)}"
		        f" as usize")

	def _element_bytes(self, placement: Placement) -> int | None:
		nested = self.resolved.structs.get(placement.type_name or "")
		if nested is not None:
			return (int(nested.layout.size_bytes)
			        if nested.layout.is_fixed_size else None)
		if placement.scalar is not None and placement.scalar.bits % BITS_PER_BYTE == 0:
			return max(placement.scalar.bits // BITS_PER_BYTE, 1)
		return None

	def _variable(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""A member whose extent the data decides."""
		name   = _ident(local_name(struct, placement))
		base   = c_name(local_name(struct, placement))
		start  = self._offset_expression(struct, placement)
		length = self._length_expression(struct, placement)

		nested = self.resolved.structs.get(placement.type_name or "")

		# The length is the member's total extent, which a walk does not need:
		# it steps element by element. Asking for it first refused a run of
		# variable-size records before the branch that walks them ran (26.36).
		if start is None or (length is None and (nested is None
		                                         or nested.layout.is_fixed_size)):
			return ["", f"\t// {placement.path}: sized by"
			        f" `{placement.sized_by}`, which this backend cannot resolve."]
		lines  = [
			"",
			f"\t/// {placement.path}: offset and extent both from the data.",
			f"\tpub fn {_ident(base + '_offset')}(&self) -> usize {{",
			# Accumulating, like the delimited members' own offset above.
			# This summed instead, and every term in the sum re-derived its
			# base by rescanning the members before it -- so the fix of 26.30
			# had reached the delimited members here and nothing placed after
			# them.
			*(self._offset_body(struct, placement) or [f"\t\t{start}"]),
			"\t}",
		]

		if nested is None:
			# `length` is not None here: the refusal above only lets a
			# missing one through for a variable-size struct element, which
			# this branch is not.
			assert length is not None
			# An element wider than a byte is reached by index, which the
			# constant-count emitter below has always done and this did not:
			# the same array with its count in the message came back as a
			# `&[u8]`, and the values in it are whatever byte order the schema
			# named rather than the host's.
			if indexed_elements(placement):
				lines.extend(self._variable_elements(struct, placement, length))
				return lines
			# Clamped to what the slice holds. The length is a field, so it is
			# whatever the message says, and `&bytes[at..at + declared]`
			# *panics* on a message that claims more than it carries -- which
			# is memory-safe and is still a denial of service in a `no_std`
			# build where a panic aborts. `validate` reports it as malformed;
			# this is what keeps a caller who skipped it running.
			lines.extend([
				"",
				f"\tpub fn {name}(&self) -> &[u8] {{",
				f"\t\tlet at = self.{_ident(base + '_offset')}();",
				f"\t\tlet n  = core::cmp::min({self._unparen(length)},",
				"\t\t\tself.bytes.len().saturating_sub(at));",
				"\t\t&self.bytes[at..at + n]",
				"\t}",
			])
			return lines

		inner = _pascal(placement.type_name or "")
		count = self._count_expression(struct, placement)

		# `[remaining]` says how many *bytes* are left, not how many elements
		# are in them, and an element with no single size has no stride to
		# divide the one by to get the other -- so there is no count here that
		# is not the walk itself. This wrote the Python `None` it came back as
		# straight into the module, where `None` is not a `usize` in any
		# language. C++ had the same line and the same defect.
		#
		# The walk is the answer rather than a refusal: every accessor below
		# reads `_count()`, and so does the offset of every member after the
		# run, so declining it would take the rest of the struct with it.
		walked = (count is None and not nested.layout.is_fixed_size
		          and has_computable_extent(self.resolved.structs, nested))
		if count is None and not walked:
			return lines + [
				"",
				f"\t// No {_ident(base + '_count')}(): one"
				f" `{placement.type_name}` cannot be measured",
				"\t// from its own bytes, so a run of them can neither be"
				" counted nor walked.",
			]

		# ...and where the count *is* the walk, the walks below stop at the
		# frame rather than calling it. A stopping rule that walks would make
		# every step of a walk a walk of its own.
		bound = "" if count is None else f"n < self.{_ident(base + '_count')}() && "

		lines.extend([
			"",
			*([f"\t/// How many elements are here: `{placement.sized_by}` gives",
			   "\t/// the bytes that are left, and only the walk says how many",
			   "\t/// elements fit in them.",
			   f"\tpub fn {_ident(base + '_count')}(&self) -> usize {{",
			   f"\t\tlet mut at = self.{_ident(base + '_offset')}();",
			   "\t\tlet mut n  = 0usize;",
			   "",
			   "\t\twhile at < self.bytes.len() {",
			   f"\t\t\tlet element = {inner} {{ bytes: &self.bytes[at..] }};",
			   "\t\t\tlet size    = element.extent();",
			   "",
			   "\t\t\tif size == 0 || at + size > self.bytes.len() {",
			   "\t\t\t\t// A zero-extent element would walk here forever,",
			   "\t\t\t\t// and one past the end was never in this frame.",
			   "\t\t\t\tbreak;",
			   "\t\t\t}",
			   "\t\t\tat += size;",
			   "\t\t\tn  += 1;",
			   "\t\t}",
			   "\t\tn",
			   "\t}"]
			  if count is None else [
				f"\tpub fn {_ident(base + '_count')}(&self) -> usize {{",
				f"\t\t{count}",
				"\t}"]),
			"",
			f"\t/// Element `index`. Bounded by the count as well as the",
			"\t/// extent: bytes after the array are inside the view and are",
			"\t/// not elements.",
			f"\tpub fn {name}(&self, index: usize) -> Result<{inner}<'_>> {{",
			*([] if count is None else [
				f"\t\tif index >= self.{_ident(base + '_count')}() {{",
				"\t\t\treturn Err(Error::Bounds);",
				"\t\t}",
			]),
			*([f"\t\tlet at = self.{_ident(base + '_offset')}()"
			   f" + index * {inner}::SIZE;",
			   f"\t\t{inner}::new(&self.bytes[at..])"]
			  if nested.layout.is_fixed_size else [
				# No stride to index by, so the run is walked -- the
				# terminated run's walk with the count as its stopping rule.
				f"\t\tlet mut at = self.{_ident(base + '_offset')}();",
				"\t\tlet mut n  = 0usize;",
				"",
				"\t\twhile at < self.bytes.len() {",
				f"\t\t\tlet element = {inner} {{ bytes: &self.bytes[at..] }};",
				"\t\t\tlet size    = element.extent();",
				"",
				"\t\t\tif size == 0 || at + size > self.bytes.len() {",
				"\t\t\t\t// A zero-extent element would walk here forever,",
				"\t\t\t\t// and one past the end was never in this frame.",
				"\t\t\t\tbreak;",
				"\t\t\t}",
				"\t\t\tif n == index {",
				f"\t\t\t\treturn {inner}::new(&self.bytes[at..at + size]);",
				"\t\t\t}",
				"\t\t\tat += size;",
				"\t\t\tn  += 1;",
				"\t\t}",
				"\t\tErr(Error::Bounds)",
			  ]),
			"\t}",
		])

		if not nested.layout.is_fixed_size:
			lines.extend([
				"",
				"\t/// How far the whole run reaches: the walk above with no",
				"\t/// index to stop at. It is what places every member after",
				"\t/// the run, and nothing emitted it -- so those members",
				"\t/// were declined as having an offset this could not",
				"\t/// resolve, which was true only because this was missing.",
				f"\tpub fn {_ident(base + '_span_from')}(&self, start: usize)"
				" -> usize {",
				"\t\tlet mut at = start;",
				# The counter only where the stopping rule reads it. A run the
				# message counts stops at `n`; one that runs to the end of the
				# frame does not, and incrementing a number nothing looks at is
				# a warning -- which `-D warnings` makes an error, and which
				# invariant 23 says not to teach a reader to ignore.
				*([] if not bound else ["\t\tlet mut n  = 0usize;"]),
				"",
				f"\t\twhile {bound}at < self.bytes.len() {{",
				f"\t\t\tlet element = {inner} {{ bytes: &self.bytes[at..] }};",
				"\t\t\tlet size    = element.extent();",
				"",
				"\t\t\tif size == 0 || at + size > self.bytes.len() {",
				"\t\t\t\tbreak;",
				"\t\t\t}",
				"\t\t\tat += size;",
				*([] if not bound else ["\t\t\tn  += 1;"]),
				"\t\t}",
				"\t\tat - start",
				"\t}",
				"",
				f"\tpub fn {_ident(base + '_span')}(&self) -> usize {{",
				f"\t\tself.{_ident(base + '_span_from')}"
				f"(self.{_ident(base + '_offset')}())",
				"\t}",
			])
		return lines

	def _variable_elements(self, struct: ResolvedStruct, placement: Placement,
			length: str) -> list[str]:
		"""`u16 x[n]`: a count the message gives, and a getter taking an index.

		The count is clamped to the slice, which is the same clamp the byte
		spelling above puts on its length and for the same reason: the number
		is the message's, and a caller looping to it would otherwise index
		past the frame (invariant 41).
		"""
		name   = _ident(local_name(struct, placement))
		base   = c_name(local_name(struct, placement))
		scalar = placement.scalar
		assert scalar is not None
		width  = scalar.bits // BITS_PER_BYTE
		rtype  = self._rust_type(scalar)
		load   = self._load(placement, scalar,
		                    offset=f"self.{_ident(base + '_offset')}()"
		                           f" + index * {width}")

		return [
			"",
			f"\t/// How many {scalar.name} elements of `{placement.path}` are"
			" here.",
			"\t///",
			"\t/// The message says how many there are; this says how many the",
			"\t/// frame holds, which is the one a caller may loop to.",
			f"\tpub fn {_ident(base + '_count')}(&self) -> usize {{",
			f"\t\tlet at = self.{_ident(base + '_offset')}();",
			f"\t\tcore::cmp::min({self._unparen(length)},",
			"\t\t\tself.bytes.len().saturating_sub(at))"
			f" / {width}",
			"\t}",
			"",
			f"\t/// Element `index`, an {scalar.name}.",
			"\t///",
			"\t/// No slice accessor: the element is ValueConverted, so bytes",
			"\t/// handed back whole would not be the values.",
			f"\tpub fn {name}(&self, index: usize) -> Result<{rtype}> {{",
			f"\t\tif index >= self.{_ident(base + '_count')}() {{",
			"\t\t\treturn Err(Error::Bounds);",
			"\t\t}",
			f"\t\tOk({load})",
			"\t}",
		]

	def _scalar_array(self, struct: ResolvedStruct, placement: Placement,
			scalar: ScalarType) -> list[str]:
		"""`u16 samples[4]`: one getter taking an index.

		No slice: the element is `ValueConverted`, so bytes handed back whole
		would not be the values. Index them individually, which is C's rule and
		the reason it gives.
		"""
		name  = _ident(c_name(local_name(struct, placement)))
		count = placement.array_count or 0
		width = scalar.bits // BITS_PER_BYTE
		start = self._offset_expression(struct, placement)
		if start is None:
			return ["", f"\t// {placement.path}: this backend cannot resolve"
			        " where the array starts."]

		rtype = self._rust_type(scalar)
		load  = self._load(placement, scalar,
		                   offset=f"{self._unparen(start)} + index * {width}")

		return [
			"",
			f"\t/// `{placement.path}`: {count} elements of {scalar.name}.",
			"\t///",
			"\t/// No slice accessor: the element is ValueConverted, so bytes",
			"\t/// handed back whole would not be the values.",
			f"\tpub const {name.upper()}_COUNT: usize = {count};",
			f"\tpub fn {name}(&self, index: usize) -> Result<{rtype}> {{",
			f"\t\tif index >= {count} {{",
			"\t\t\treturn Err(Error::Bounds);",
			"\t\t}",
			f"\t\tOk({load})",
			"\t}",
		]

	def _struct_array(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""A counted array of structs, or of wide scalars."""
		# A wide scalar element gets an indexed getter, which is what C emits.
		# It reached the note below, which said `u16` has no fixed size -- of a
		# type that plainly has one, because the branch was only ever written
		# for struct elements.
		scalar = placement.scalar
		if scalar is not None and not scalar.is_bit_packed \
				and scalar.bits % BITS_PER_BYTE == 0:
			return self._scalar_array(struct, placement, scalar)

		inner = self.resolved.structs.get(placement.type_name or "")
		if inner is None or not inner.layout.is_fixed_size:
			return ["", f"\t// {placement.path}: element type"
			        f" {placement.type_name} has no fixed size."]

		name  = _ident(local_name(struct, placement))
		count = placement.array_count or 0
		start = placement.offset_bytes
		outer = _pascal(placement.type_name or "")

		return [
			"",
			f"\t/// {placement.path}: {count} elements of {outer}.",
			f"\tpub fn {name}(&self, index: usize) -> Result<{outer}<'_>> {{",
			f"\t\tif index >= {count} {{",
			"\t\t\treturn Err(Error::Bounds);",
			"\t\t}",
			f"\t\t{outer}::new(&self.bytes[{start} + index * {outer}::SIZE..])",
			"\t}",
		]

	def _axes_doc(self, entry: Resolved) -> list[str]:
		vector = entry.vector
		axes   = " ".join(f"{axis.value}={vector.get(axis).render()}"
		                  for axis in (Axis.OFFSET, Axis.SIZE, Axis.REPR,
		                               Axis.ATOMIC, Axis.MUTATE))
		return [f"\t/// {entry.placement.path}: {axes}"]

	def _marker(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""The byte-order marker's constants and accessors (section 8.3).

		Compared as a byte sequence rather than decoded as a number: it has to
		be readable before its own byte order is known.

		This backend said "not in the static subset yet" for the marker and
		then read every field it governs big-endian regardless, so a
		little-endian TIFF -- which is most of them -- came back byte-swapped
		with no diagnostic. The map said `ConditionallyConverted(byte_order)`
		on those fields the whole time.
		"""
		marker = self.markers.get(placement.name)
		scalar = placement.scalar
		if marker is None or scalar is None:
			return []

		env    = self.resolved.layout.env
		little = evaluate(marker.little, env)
		big    = evaluate(marker.big, env)
		width  = scalar.bits
		name   = c_name(local_name(struct, placement))
		digits = width // 4

		return [
			"",
			f"\t/// `{placement.path}`: which byte order the rest of this frame",
			"\t/// uses, read from the data (8.3). Compared as a byte sequence",
			"\t/// rather than decoded as a number -- it has to be readable",
			"\t/// before its own order is known.",
			f"\tpub const {name.upper()}_LITTLE: u{width} ="
			f" 0x{little:0{digits}X};",
			f"\tpub const {name.upper()}_BIG: u{width} = 0x{big:0{digits}X};",
			"",
			"\t/// The host's own order. A writer stores this rather than",
			"\t/// picking one, which is what makes the writer deterministic",
			"\t/// even though the format admits both.",
			f"\tpub const {name.upper()}_HOST: u{width} ="
			f" if cfg!(target_endian = \"big\") {{",
			f"\t\tSelf::{name.upper()}_BIG",
			"\t} else {",
			f"\t\tSelf::{name.upper()}_LITTLE",
			"\t};",
			"",
			f"\tpub fn {_ident(name + '_is_little')}(&self) -> bool {{",
			f"\t\tsitu_rt::read_be(self.bytes, {placement.offset_bytes},"
			f" {width // 8}) as u{width} == Self::{name.upper()}_LITTLE",
			"\t}",
		]

	def _marker_predicate(self, placement: Placement) -> str:
		name = c_name(placement.marker or "")
		return f"self.{_ident(name + '_is_little')}()"

	def _load(self, placement: Placement, scalar: ScalarType,
			offset: str | None = None) -> str:
		raw   = self._raw_load(placement, scalar, offset)
		rtype = self._rust_type(scalar)

		if scalar.is_bcd:
			raw = f"situ_rt::bcd_decode({raw}, {scalar.digits})"

		if placement.type_name in self.enums:
			enum = _pascal(placement.type_name or "")
			return (f"// SAFETY-FREE: the raw value is handed back where it is\n"
			        f"\t\t// not a member; section 8.7 rejects those on parse.\n"
			        f"\t\tmatch {raw} as {self._rust_type(scalar)} {{\n"
			        + "\n".join(
			            f"\t\t\t{value} => Some({enum}::{_pascal(member)}),"
			            for member, value in
			            self.resolved.layout.env.enums[placement.type_name or ""].items())
			        + "\n\t\t\t_ => None,\n\t\t}")

		return f"{raw} as {rtype}"

	def _raw_load(self, placement: Placement, scalar: ScalarType,
			offset: str | None = None) -> str:
		at  = offset if offset is not None else str(placement.offset_bytes)

		if scalar.is_bit_packed:
			msb  = placement.bit_order is not ast.BitOrder.LSB_FIRST
			raw  = (f"situ_rt::read_bits(self.bytes, {placement.offset_bits},"
			        f" {scalar.bits}, {str(msb).lower()})")
			if scalar.signed:
				return f"situ_rt::sign_extend({raw}, {scalar.bits})"
			return raw

		# A field the data decides the order of. Without this it read the
		# marker's own format big-endian whatever the marker said.
		if placement.marker is not None:
			predicate = self._marker_predicate(placement)
			width     = scalar.bits // BITS_PER_BYTE
			raw       = (f"(if {predicate} {{"
			             f" situ_rt::read_le(self.bytes, {at}, {width}) }}"
			             f" else {{ situ_rt::read_be(self.bytes, {at},"
			             f" {width}) }})")
			if scalar.signed:
				return f"situ_rt::sign_extend({raw}, {scalar.bits})"
			return raw

		reader = _reader(placement.endian)
		raw    = (f"situ_rt::{reader}(self.bytes, {at},"
		          f" {scalar.bits // BITS_PER_BYTE})")
		if scalar.signed:
			return f"situ_rt::sign_extend({raw}, {scalar.bits})"
		return raw

	# -- writes --------------------------------------------------------

	# -- obligations (sections 14.2 and 16.1) ---------------------------

	def _dirty_bits(self, struct: ResolvedStruct, placement: Placement) -> str:
		"""The bits every obligation over this field stands for, ORed.

		Every one, not the first: a field under a tag *and* an invariant leaves
		both stale, and marking one of the two is a buffer that reports itself
		ready to send while a covered byte no longer matches what
		authenticates it.
		"""
		names = [f"Self::DIRTY_{c_name(held.name).upper()}"
		         for label in placement.covered_by
		         if (held := obligation(self.schema, struct, label)) is not None]
		return " | ".join(names) if names else "0"

	def _dirty_constants(self, struct: ResolvedStruct) -> list[str]:
		"""One associated constant per obligation over this struct's bytes.

		The numbering is `traverse.obligations`, shared with the other three
		backends: a caller who reads a bit out of one language's generated code
		and checks it against another's must find the same answer.
		"""
		held = obligations(self.schema, struct)
		if not held:
			return []

		lines = ["",
		         "\t/// Dirty bits. A covered write sets one; the buffer is not",
		         "\t/// transmittable until it is cleared -- a tag by being",
		         "\t/// recomputed and finalized, a derived field by its recompute."]
		lines.extend(
			f"\tpub const DIRTY_{c_name(one.name).upper()}: u32 = {hex(1 << one.bit)};"
			for one in held)
		lines.append(f"\tpub const DIRTY_MASK: u32 = {hex((1 << len(held)) - 1)};")
		return lines

	def _invariants(self, struct: ResolvedStruct) -> list[str]:
		"""A derived field, and the one thing allowed to write it.

		The lattice has already refused the plain setter -- `mutate` is
		Immutable -- so without this the schema could state a relationship and
		never satisfy it.
		"""
		lines: list[str] = []

		for decl in derived_by(self.schema, struct):
			field = decl.derived.partition(".")[2]
			entry = next((e for e in struct.entries
			              if e.placement.path == f"{struct.name}.{field}"), None)
			if entry is None or entry.placement.scalar is None:
				continue

			base  = c_name(field)
			value = invariant_expression(struct, decl.expr, self)
			if value is None:
				lines.extend([
					"",
					f"\t// No recompute_{base}(): this backend cannot evaluate",
					f"\t// `{unparse_expr(decl.expr)}` at run time. The refusal to",
					"\t// write the field directly still stands, so the invariant",
					"\t// cannot be broken -- only left unsatisfiable here.",
				])
				continue

			scalar = entry.placement.scalar
			rtype  = self._field_type(entry.placement, writing=True)
			bit    = f"Self::DIRTY_{base.upper()}"

			lines.extend([
				"",
				f"\t/// `{decl.derived} == {unparse_expr(decl.expr)}`",
				"\t///",
				f"\t/// Writing anything the right side reads sets `{bit}`, and",
				"\t/// the buffer is not transmittable until this recomputes",
				"\t/// (section 16.1).",
				f"\tpub fn {_ident(f'recompute_{base}')}(&mut self,"
				" dirty: &mut Dirty) {",
				f"\t\tlet value = ({value}) as {rtype};",
				f"\t\t{self._store(entry.placement, scalar)}",
				f"\t\tdirty.clear({bit});",
				"\t}",
			])

		return lines

	# -- invariant.Terms, in Rust ---------------------------------------

	def literal(self, value: int) -> str:
		return f"{value}usize"

	def binary(self, op: str, left: str, right: str) -> str:
		return f"({left} {op} {right})"

	def offset(self, struct: ResolvedStruct, placement: Placement) -> str | None:
		return (f"{placement.offset_bytes}usize" if placement.offset_bits is not None
		        else None)

	def size(self, struct: ResolvedStruct, placement: Placement) -> str | None:
		if placement.is_fixed_size:
			return f"{placement.size_bits // BITS_PER_BYTE}usize"
		return self._length_expression(struct, placement)

	def count(self, struct: ResolvedStruct, placement: Placement) -> str | None:
		return self._count_expression(struct, placement)

	def _setter(self, struct: ResolvedStruct, entry: Resolved) -> list[str]:
		placement = entry.placement
		scalar    = placement.scalar

		if placement.kind != "field":
			return []
		if scalar is None or placement.array_count is not None \
				or placement.sized_by is not None:
			# A run has no setter: its bytes are the slice above. Where the
			# map has *weakened* `mutate`, though, that is a claim about the
			# member and section 1 says the absence is explained -- and this
			# said nothing for `message.opts`, whose length a field decides.
			mutate = entry.vector.get(Axis.MUTATE)
			if scalar is None or mutate.base in ("InPlaceFixed",
			                                     "InPlaceSlack"):
				return []
			name = _ident(f"set_{c_name(local_name(struct, placement))}")
			return ["",
			        f"\t// No {name}(): mutate is {mutate.render()}. The slice",
			        "\t// above is where the bytes are; making room for more of",
			        "\t// them is a rewrite of the frame rather than a store."]
		if placement.type_name in self.structs:
			return []

		# A scalar at a *dynamic* offset is writable too, and this backend
		# emitted nothing at all for one -- no setter and no note, so a field
		# the capability map calls `mutate = InPlaceFixed` could be read here
		# and not written. `examples/dnsname`'s `question.qtype` sits after a
		# name and is the case; the other three have had the setter since
		# 26.27, and each does nothing where the member does not fit (26.35).
		if placement.offset_bits is None:
			return self._dynamic_setter(struct, entry)

		# `set_` plus a raw identifier is not one: `r#` has to prefix the whole
		# name. `set_type` is not a keyword, so the escape is only needed on
		# the getter.
		base = c_name(local_name(struct, placement))
		name = _ident(local_name(struct, placement))
		setter = _ident(f"set_{base}")

		if entry.vector.get(Axis.MUTATE).base != "InPlaceFixed":
			return ["",
			        f"\t// No {setter}(): mutate is"
			        f" {entry.vector.get(Axis.MUTATE).render()}."]
		rtype = self._field_type(placement, writing=True)

		if placement.since is not None and placement.version_field is not None:
			# Writing it to an earlier message puts these bytes past that
			# message's end. `Result` is `#[must_use]`, so unlike C the
			# refusal cannot be dropped without a warning.
			version = _ident(c_name(placement.version_field))
			return [
				"",
				f"\t/// Writing this to a message older than version"
				f" {placement.since} would put",
				"\t/// these bytes past its end, so it refuses.",
				f"\tpub fn {setter}(&mut self, value: {rtype}) -> Result<()> {{",
				f"\t\tif self.as_ref().{version}() < {placement.since} {{",
				"\t\t\treturn Err(Error::Version);",
				"\t\t}",
				# The declared version is the message's claim, not a fact
				# about the buffer: `ver = 2` in three bytes passed this and
				# panicked in `write_be` on the byte after the frame.
				*self._versioned_bounds(placement, "self.bytes"),
				f"\t\t{self._store(placement, scalar)}",
				"\t\tOk(())",
				"\t}",
			]

		# A covered write takes the dirty word, because it has to mark a bit
		# that outlives the view. This backend used to refuse the setter
		# outright, which is sound but leaves a field the map calls writable
		# with no way to write it -- and the schema then means something
		# narrower in Rust than in the other three.
		if placement.covered_by:
			what = ", ".join(placement.covered_by)
			return [
				"",
				f"\t/// Writing this leaves {what} stale, so it takes the dirty",
				"\t/// word and marks the bit. The cost is in the signature",
				"\t/// rather than in a comment somebody may not read.",
				f"\tpub fn {setter}(&mut self, dirty: &mut Dirty, value: {rtype}) {{",
				f"\t\t{self._store(placement, scalar)}",
				f"\t\tdirty.mark({self._dirty_bits(struct, placement)});",
				"\t}",
			]

		return [
			"",
			f"\tpub fn {setter}(&mut self, value: {rtype}) {{",
			f"\t\t{self._store(placement, scalar)}",
			"\t}",
		]

	def _nothing(self, placement: Placement) -> str:
		"""What a getter returns where the member is not in the frame."""
		return "None" if placement.type_name in self.enums else "0"

	def _dynamic_setter(self, struct: ResolvedStruct,
			entry: Resolved) -> list[str]:
		"""A setter for a scalar whose offset the message decides.

		Does nothing where the member does not fit, which is what C, C++ and
		Python do: writing past the frame is somebody else's data, and
		`validate` is where the caller learns the message was malformed.
		"""
		placement = entry.placement
		scalar    = placement.scalar
		assert scalar is not None

		base   = c_name(local_name(struct, placement))
		setter = _ident(f"set_{base}")

		if entry.vector.get(Axis.MUTATE).base != "InPlaceFixed":
			return ["",
			        f"\t// No {setter}(): mutate is"
			        f" {entry.vector.get(Axis.MUTATE).render()}."]

		offset = self._offset_expression(struct, placement)
		fits   = self._fits(struct, placement,
		                    max(1, (scalar.bits + BITS_PER_BYTE - 1)
		                        // BITS_PER_BYTE))
		if offset is None or fits is None:
			return ["",
			        f"\t// No {setter}(): this backend cannot resolve where"
			        " the member starts."]

		# Through `as_ref()`: the offset is a read -- a scan or a sum of
		# lengths -- and those helpers live on the immutable view. `bytes` is
		# the one thing both types have.
		#
		# Past what somebody else already wrote, which is invariant 53 in its
		# other form: `_store` puts an `as_ref()` on a marker predicate itself,
		# because every other setter site needs one, and this rewrote that into
		# `self.as_ref().as_ref()`. A marker-governed member behind a
		# variable-length one is the only shape that reaches both, and no
		# schema here has one -- `examples/tiff` is a header of constant
		# offsets.
		def reading(text: str) -> str:
			return re.sub(r"self\.(?!as_ref\(\))(?!bytes\b)",
			              "self.as_ref().", text)

		rtype = self._field_type(placement, writing=True)

		# A covered member takes the dirty word here as it does at a constant
		# offset. This returned an empty list saying "the covered form is
		# emitted below", and below is in the caller, which had already
		# dispatched here -- so a tag-covered member behind a variable-length
		# one had no setter in this backend and no note either, while the
		# other three wrote it (invariant 48).
		covered = list(placement.covered_by)
		what    = ", ".join(covered)
		return [
			"",
			*([] if not covered else [
				f"\t/// Writing this leaves {what} stale, so it takes the dirty",
				"\t/// word and marks the bit.",
			]),
			"\t/// Does nothing where the member does not fit: its offset is a",
			"\t/// sum of lengths the message chose, and writing past the frame",
			"\t/// is somebody else's data. `validate` reports such a message.",
			f"\tpub fn {setter}(&mut self,"
			+ (" dirty: &mut Dirty," if covered else "")
			+ f" value: {rtype}) {{",
			f"\t\tif !({reading(fits)}) {{",
			"\t\t\treturn;",
			"\t\t}",
			f"\t\t{reading(self._store(placement, scalar, self._unparen(offset)))}",
			*([] if not covered else [
				f"\t\tdirty.mark({self._dirty_bits(struct, placement)});",
			]),
			"\t}",
		]

	def _store(self, placement: Placement, scalar: ScalarType,
			offset: str | None = None) -> str:
		at    = offset if offset is not None else str(placement.offset_bytes)
		value = "value as u64"

		if placement.type_name in self.enums:
			value = "value as u64"
		if scalar.is_bcd:
			value = f"situ_rt::bcd_encode({value}, {scalar.digits})"

		if scalar.is_bit_packed:
			msb = placement.bit_order is not ast.BitOrder.LSB_FIRST
			return (f"situ_rt::write_bits(self.bytes, {placement.offset_bits},"
			        f" {scalar.bits}, {str(msb).lower()}, {value});")

		# The write has to agree with the read, or a round trip swaps the
		# value: the getter branched on the marker and the setter did not.
		if placement.marker is not None:
			# Through `as_ref`: the setters are on the `Mut` struct and the
			# marker accessor is on the read one, which is the split this
			# backend makes everywhere rather than anything about markers.
			width = scalar.bits // BITS_PER_BYTE
			predicate = self._marker_predicate(placement).replace(
				"self.", "self.as_ref().", 1)
			return (f"if {predicate} {{"
			        f" situ_rt::write_le(self.bytes, {at}, {width}, {value}) }}"
			        f" else {{ situ_rt::write_be(self.bytes, {at}, {width},"
			        f" {value}) }}")

		writer = _writer(placement.endian)
		return (f"situ_rt::{writer}(self.bytes, {at},"
		        f" {scalar.bits // BITS_PER_BYTE}, {value});")

	# -- validation ----------------------------------------------------

	def _validate(self, struct: ResolvedStruct) -> list[str]:
		from situc.expr import evaluate

		checks: list[str] = []

		for entry in own_entries(struct):
			placement = entry.placement
			scalar    = placement.scalar
			name      = _ident(local_name(struct, placement))

			check = classify_check(struct, placement, self.structs)

			# The bounds question first, and it does not replace the rest: a
			# member can be both outside the frame and constrained, and
			# `continue` here left `[must_eq]` unchecked for every
			# dynamically placed field.
			checks.extend(self._fits_check(struct, placement))

			if check is Check.NOTHING:
				continue
			if check is Check.DISCRIMINANT:
				checks.extend(self._discriminant_check(struct, placement))
				# And each arm, in declaration order, which is where the
				# other three emit theirs: `own_entries` drops a dotted
				# path, so an arm member never reaches this loop on its own.
				for _, member in arm_members(struct, placement):
					if member is not None:
						checks.extend(self._arm_fits_check(struct, member))
						checks.extend(self._arm_validation(struct, member))
				continue
			if check is Check.DELIMITED:
				checks.extend(self._delimiter_checks(struct, placement))
				continue
			if check is Check.REPEATED:
				checks.extend(self._array_checks(struct, placement, name))
				continue
			if check is Check.NESTED:
				# Only where the accessor exists.
				inner = self.resolved.structs.get(placement.type_name or "")
				if inner is not None and not inner.layout.is_fixed_size \
						and not has_computable_extent(
							self.resolved.structs, inner):
					checks.append(f"\t\t// {placement.path}: no accessor to"
					              " validate through.")
					continue
				# Two questions rather than one: the frame may not contain
				# the member at all, which the accessor refuses now (26.31),
				# and the member may be there and malformed. `?` carries both
				# out, in that order.
				checks.append(f"\t\tself.{name}()?.validate()?;")
				continue

			assert scalar is not None

			# The same offset the accessor uses. A member after a
			# variable-length one is placed at run time, and the validator
			# reads it the same way the getter does.
			offset = (None if placement.offset_bits is not None
			          else self._offset_expression(struct, placement))
			assert not (placement.offset_bits is None
			            and scalar.is_bit_packed), \
				f"{placement.path}: bit-packed at a dynamic offset"
			if placement.offset_bits is None and offset is None:
				continue

			if check is Check.RESERVED:
				policy = _reserved_policy(placement.attrs)
				if policy in ("must_be_zero", "must_be_one"):
					want = 0 if policy == "must_be_zero" else (1 << scalar.bits) - 1
					checks.extend([
						f"\t\tif {self._raw_load(placement, scalar, offset)} != {want} {{",
						"\t\t\treturn Err(Error::Constraint);",
						"\t\t}",
					])
				continue

			enum = self.enums.get(placement.type_name or "")
			if enum is not None \
					and enum.effective_default is ast.EnumDefault.ERROR:
				checks.extend([
					f"\t\tif !{_pascal(enum.name)}::is_known("
					f"{self._raw_load(placement, scalar, offset)} as"
					f" {self._rust_type(scalar)}) {{",
					"\t\t\treturn Err(Error::Constraint);",
					"\t\t}",
				])

			for attr in placement.attrs:
				if attr.name not in ("must_eq", "max", "min") or attr.value is None:
					continue
				expected = evaluate(attr.value, self.resolved.layout.env)
				operator = {"must_eq": "!=", "max": ">", "min": "<"}[attr.name]
				checks.extend([
					f"\t\tif {self._raw_load(placement, scalar, offset)}"
					f" {operator} {expected} {{",
					"\t\t\treturn Err(Error::Constraint);",
					"\t\t}",
				])

		return [
			"",
			"\t/// Every constraint the schema declares, on parse.",
			"\tpub fn validate(&self) -> Result<()> {",
			*(checks or ["\t\t// Nothing in this struct is constrained."]),
			"\t\tOk(())",
			"\t}",
		]

	def _reserved_checks(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""Reserved bytes hold their pattern, however many of them there are.

		This backend was the one that did not crash on a run the message
		sizes, because it returned early on a count of None -- so it emitted
		no check at all and said nothing, which invariant 27 rates below the
		crash.
		"""
		policy = _reserved_policy(placement.attrs)
		scalar = placement.scalar
		if policy not in ("must_be_zero", "must_be_one") or scalar is None:
			return []
		if scalar.is_bit_packed or scalar.bits % BITS_PER_BYTE != 0:
			return []

		start = self._offset_expression(struct, placement)
		if start is None:
			return [f"\t\t// {placement.path}: this backend cannot resolve"
			        " where the reserved bytes are."]

		width = scalar.bits // BITS_PER_BYTE
		if placement.array_count is not None:
			count: str | None = str(placement.array_count * width)
		elif data_sized(placement):
			count = self._length_expression(struct, placement)
			if count is None:
				return [f"\t\t// {placement.path}: this backend cannot resolve"
				        " how many reserved bytes there are."]
		else:
			count = str(width)

		want = 0 if policy == "must_be_zero" else 0xFF
		return [
			f"\t\t// {placement.path} is reserved [{policy}]",
			"\t\t{",
			f"\t\t\tlet at = {start};",
			f"\t\t\tlet n = {count};",
			"\t\t\tlet end = core::cmp::min(at + n, self.bytes.len());",
			f"\t\t\tif self.bytes[core::cmp::min(at, end)..end]"
			f".iter().any(|&b| b != {want}) {{",
			"\t\t\t\treturn Err(Error::Constraint);",
			"\t\t\t}",
			"\t\t}",
		]

	def _array_checks(self, struct: ResolvedStruct, placement: Placement,
			name: str) -> list[str]:
		checks: list[str] = []
		if placement.kind == "reserved":
			return self._reserved_checks(struct, placement)

		count = placement.array_count
		if count is None or placement.scalar is None:
			return checks
		if placement.scalar.bits != BITS_PER_BYTE:
			return checks

		for attr in placement.attrs:
			if attr.name == "encoding":
				named = getattr(attr.value, "name", None)
				if named in ("ascii", "utf8"):
					checks.extend([
						f"\t\tif !situ_rt::{named}_valid(self.{name}()) {{",
						"\t\t\treturn Err(Error::Constraint);",
						"\t\t}",
					])
			if attr.name == "nul_terminated":
				checks.extend([
					f"\t\tif self.{name}_len() >= {count} {{",
					"\t\t\treturn Err(Error::Constraint);",
					"\t\t}",
				])
		return checks

	# -- types ---------------------------------------------------------

	def _field_type(self, placement: Placement, writing: bool = False) -> str:
		if placement.type_name in self.enums:
			enum = _pascal(placement.type_name or "")
			# A field may hold a value no member names; section 8.7 rejects
			# those on parse, not on read, so the getter says so in its type.
			return enum if writing else f"Option<{enum}>"
		scalar = placement.scalar
		assert scalar is not None
		return self._rust_type(scalar)

	def _rust_type(self, scalar: ScalarType) -> str:
		width = _storage_width(scalar.bits)
		return f"i{width}" if scalar.signed else f"u{width}"


def _storage_width(bits: int) -> int:
	for width in WORD_WIDTHS:
		if bits <= width:
			return width
	return 64


def _reserved_policy(attrs: tuple[ast.Attr, ...]) -> str | None:
	for attr in attrs:
		if attr.name in ("must_be_zero", "must_be_one"):
			return str(attr.name)
		if attr.name in ("preserve", "unknown"):
			return None
	return "must_be_zero"
