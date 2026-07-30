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

from dataclasses import dataclass, field

from situc import ast
from situc.capability import Axis
from situc.codegen.c.names import c_name
from situc.diagnostics import Diagnostic
from situc.layout import BITS_PER_BYTE, Placement
from situc.names import over_fields, render_delimiter
from situc.propagate import Resolved
from situc.invariant import derived as derived_by
from situc.invariant import expression as invariant_expression
from situc.resolve import ResolvedSchema, ResolvedStruct
from situc.traverse import (
	Check, Member, arm_members, classify, classify_check, declares_its_own_length,
	extent_parts,
	has_computable_extent, local_name, matched_values, obligation,
	obligations, own_entries, own_members,
)
from situc.types import ScalarType
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
		prefix: str = "situ") -> Generated:
	return Generated(module=Emitter(schema, resolved, basename).module(),
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
			basename: str) -> None:
		self.schema   = schema
		self.resolved = resolved
		self.basename = basename
		self.enums    = {decl.name: decl for decl in schema.enums()}
		self.structs  = set(resolved.structs)

	def module(self) -> str:
		body: list[str] = []
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
		text = "\n".join(body)
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

		lines.extend(self._extent_method(struct))
		lines.extend(self._required(struct))
		lines.extend(self._validate(struct))
		lines.extend(self._gate_opens(struct))
		lines.extend(["}", ""])
		lines.extend(self._gates(struct))

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

		lines.extend(self._invariants(struct))
		lines.extend(["}", ""])
		return lines

	# -- reads ---------------------------------------------------------

	def _getter(self, struct: ResolvedStruct, entry: Resolved) -> list[str]:
		placement = entry.placement
		scalar    = placement.scalar

		kind = classify(struct, placement, self.structs)

		if kind is Member.RESERVED:
			return ["",
			        f"\t// {placement.path} is reserved: no accessor, and",
			        "\t// validate() holds it to the declared pattern."]
		if kind is Member.LOCATED:
			return self._located(struct, placement)
		if kind is Member.REPEAT_WHILE:
			return self._repeat_while(struct, placement)
		if kind is Member.DELIMITED:
			return self._delimited(struct, placement)
		if kind is Member.RECORD_RUN:
			return self._record_run(struct, placement)
		if kind is Member.VARIABLE:
			return self._variable(struct, placement)
		if kind is Member.UNPLACED or kind is Member.REGION:
			return ["", f"\t// {placement.path}: not in the static subset yet."]
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
					f"\tpub fn {name}(&self) -> {nested}<'_> {{",
					f"\t\tlet at = {at};",
					f"\t\tlet n  = self.{_ident(f'{base}_extent')}();",
					f"\t\t{nested} {{ bytes: &self.bytes[at..at + n] }}",
					"\t}",
				]

			return [
				"",
				f"\t/// {placement.path} at {placement.offset_bytes}.",
				f"\tpub fn {name}(&self) -> {nested}<'_> {{",
				f"\t\t{nested} {{ bytes: &self.bytes[{placement.offset_bytes}..]"
				f" }}",
				"\t}",
			]

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
				f"\t\tOk({self._load(placement, scalar, offset)})",
				"\t}",
			]

		return [
			"",
			*self._axes_doc(entry),
			f"\tpub fn {name}(&self) -> {self._field_type(placement)} {{",
			f"\t\t{self._load(placement, scalar, offset)}",
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

	def _gates(self, struct: ResolvedStruct) -> list[str]:
		lines: list[str] = []

		for region in self._sealed(struct):
			name = c_name(local_name(struct, region))
			gate = _pascal(f"{struct.name}_{name}_gate")

			inside = [entry for entry in struct.entries
			          if entry.placement.sealed_by == region.name
			          and entry.placement.kind == "field"
			          and entry.placement.offset_bits is not None]

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

			for entry in inside:
				lines.extend(self._gated(entry))

			if not inside:
				lines.append("	// Nothing in this region has an accessor.")

			lines.extend(["}", ""])
		return lines

	def _gated(self, entry: Resolved) -> list[str]:
		placement = entry.placement
		scalar    = placement.scalar
		name      = _ident(placement.path.rsplit(".", 1)[-1])

		if any(attr.name == "secret" for attr in placement.attrs):
			return ["",
			        f"\t// {placement.path} is [secret]: no accessor is",
			        "\t// generated for it at all (section 14.6)."]

		if scalar is None:
			return []

		if placement.array_count is not None or placement.sized_by is not None:
			if scalar.bits != BITS_PER_BYTE:
				return []
			count = (str(placement.array_count) if placement.array_count is not None
			         else None)
			if count is None:
				return ["", f"\t// {placement.path}: a data-driven length inside",
				        "\t// a gate is not emitted yet."]
			start = placement.offset_bytes
			return [
				"",
				f"\tpub fn {name}(&self) -> &[u8] {{",
				f"\t\t&self.bytes[{start}..{start + int(count)}]",
				"\t}",
			]

		return [
			"",
			f"\tpub fn {name}(&self) -> {self._field_type(placement)} {{",
			f"\t\t{self._load(placement, scalar)}",
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
		"""The bytes the scan may look at: to the cap, or to the end."""
		if placement.delimiter_cap is None:
			return f"&self.bytes[({start})..]"
		return (f"&self.bytes[({start})..core::cmp::min("
		        f"({start}) + {placement.delimiter_cap}, self.bytes.len())]")

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
		limit  = (f"(self.bytes.len() - ({start}))" if placement.delimiter_cap is None
		          else f"core::cmp::min({placement.delimiter_cap}, "
		               f"self.bytes.len() - ({start}))")

		# With `[trim]` the framing and the value are different numbers.
		scan = _ident(f"{base}_raw_len" if placement.trimmed else f"{base}_len")

		lines = [
			"",
			f"\t/// `{placement.path}` runs to the first"
			f" {render_delimiter(delim)}.",
			f"\tpub fn {_ident(f'{base}_offset')}(&self) -> usize {{",
			f"\t\t{start}",
			"\t}",
			"",
			f"\tpub fn {scan}(&self) -> usize {{",
			f"\t\t{self._scan_call(placement, sliced)}",
			"\t}",
			"",
			"\t/// Whether the delimiter is there. It is not when the frame was",
			"\t/// cut short, which is the only thing parse can catch here.",
			f"\tpub fn {_ident(f'{base}_terminated')}(&self) -> bool {{",
			f"\t\tself.{scan}() < {limit}",
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
		return over_fields(names, source,
		                   lambda name: f"({held}.{_ident(c_name(name))}()"
		                                " as usize)")

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
		walk = [
			f"\t\tlet mut at = {start};",
			"\t\tlet mut n  = 0usize;",
			"",
			f"\t\twhile at < self.bytes.len(){cap} {{",
			f"\t\t\tlet element = {inner} {{ bytes: &self.bytes[at..] }};",
			"\t\t\tlet size = element.extent();",
			"\t\t\tif size == 0 || at + size > self.bytes.len() {",
			"\t\t\t\tbreak;",
			"\t\t\t}",
		]
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
			f"\tpub fn {_ident(f'{base}_span')}(&self) -> usize {{",
			*walk, *tail,
			"\t\tlet _ = n;",
			f"\t\tat - ({start})",
			"\t}",
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
		walk = [
			f"\t\tlet mut at = {start};",
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
			f"\tpub fn {_ident(f'{base}_span')}(&self) -> usize {{",
			*walk,
			"\t\t\tat += size;",
			"\t\t\tn  += 1;",
			"\t\t}",
			"\t\tlet _ = n;",
			f"\t\tif at + {len(delim)} <= self.bytes.len() {{",
			f"\t\t\tat += {len(delim)};",
			"\t\t}",
			f"\t\tat - ({start})",
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
			if placement.delimiter is not None \
					and placement.type_name in self.structs:
				return self._unframeable(struct,
				        "a run of records ends at a terminator this cannot tell"
				        " apart from the end of the bytes so far")

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
		# A run walks them and a nested member sizes its slice from one.
		if not any(classify(other, entry.placement, self.structs)
		           in (Member.RECORD_RUN, Member.REPEAT_WHILE, Member.NESTED)
		           and entry.placement.type_name == struct.name
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

		constant = 0
		terms: list[str] = []

		for other in own_members(struct):
			if other.path == placement.path:
				break
			if other.is_fixed_size:
				constant += other.size_bits // BITS_PER_BYTE
				continue
			length = self._length_expression(struct, other)
			if length is None:
				return None
			terms.append(length)

		return " + ".join([str(constant), *terms])

	def _fits_check(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""A length the message declares must fit the frame it is in.

		Nothing checked this in any backend: `u8 opts[hdr.length]` with a
		`u16` length in a 32-byte frame parsed clean. The accessor clamps,
		which is what keeps a caller who skips validation safe; this is what
		tells a caller who does not that the message is malformed rather than
		short. Clamping alone silently turns a lie into a truncation.
		"""
		if not declares_its_own_length(placement):
			return []
		if "." in placement.path[len(struct.name) + 1:]:
			return []

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

	def _length_expression(self, struct: ResolvedStruct,
			placement: Placement) -> str | None:
		if placement.kind == "variant":
			return self._variant_length(struct, placement)

		# Wherever the delimiter turns out to be. One name for "how far this
		# member reaches", whether it is a byte run or a run of records.
		if placement.delimiter is not None or placement.repeat_while is not None:
			return (f"self.{_ident(c_name(local_name(struct, placement)) + '_span')}()")

		# Arithmetic over a field rather than a reference to one. Without this
		# the member fell through to the scalar case and this backend read one
		# byte and called it the field.
		if placement.size_expr is not None:
			return self._over_fields(struct, placement.size_expr, "self")

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
		if placement.sized_by == "remaining":
			start = self._offset_expression(struct, placement)
			return None if start is None else f"(self.bytes.len() - ({start}))"
		if placement.sized_by is None:
			return None

		count = self._count_expression(struct, placement)
		element = self._element_bytes(placement)
		if count is None or element is None:
			return None
		return f"({count})" if element == 1 else f"({count}) * {element}"

	def _count_expression(self, struct: ResolvedStruct,
			placement: Placement) -> str | None:
		driver = self.resolved.find(f"{struct.name}.{placement.sized_by}")
		if driver is None or driver.placement.scalar is None:
			return None

		# Digits, not bits, and behind the scans of everything before it --
		# so neither the offset check nor the raw load below applies.
		if driver.placement.radix is not None:
			name = _ident(c_name(local_name(struct, driver.placement)) + "_value")
			return f"self.{name}() as usize"

		if driver.placement.offset_bits is None:
			return None
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

		if start is None or length is None:
			return ["", f"\t// {placement.path}: sized by"
			        f" `{placement.sized_by}`, which this backend cannot resolve."]

		nested = self.resolved.structs.get(placement.type_name or "")
		lines  = [
			"",
			f"\t/// {placement.path}: offset and extent both from the data.",
			f"\tpub fn {_ident(base + '_offset')}(&self) -> usize {{",
			f"\t\t{start}",
			"\t}",
		]

		if nested is None:
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
		lines.extend([
			"",
			f"\tpub fn {_ident(base + '_count')}(&self) -> usize {{",
			f"\t\t{count}",
			"\t}",
			"",
			f"\t/// Element `index`. Bounded by the count as well as the",
			"\t/// extent: bytes after the array are inside the view and are",
			"\t/// not elements.",
			f"\tpub fn {name}(&self, index: usize) -> Result<{inner}<'_>> {{",
			f"\t\tif index >= self.{_ident(base + '_count')}() {{",
			"\t\t\treturn Err(Error::Bounds);",
			"\t\t}",
			f"\t\tlet at = self.{_ident(base + '_offset')}()"
			f" + index * {inner}::SIZE;",
			f"\t\t{inner}::new(&self.bytes[at..])",
			"\t}",
		])
		return lines

	def _struct_array(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""A counted array of structs."""
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
		big = placement.endian is not ast.Endian.LITTLE
		at  = offset if offset is not None else str(placement.offset_bytes)

		if scalar.is_bit_packed:
			msb  = placement.bit_order is not ast.BitOrder.LSB_FIRST
			raw  = (f"situ_rt::read_bits(self.bytes, {placement.offset_bits},"
			        f" {scalar.bits}, {str(msb).lower()})")
			if scalar.signed:
				return f"situ_rt::sign_extend({raw}, {scalar.bits})"
			return raw

		reader = "read_be" if big else "read_le"
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

		if placement.kind != "field" or placement.offset_bits is None:
			return []
		if scalar is None or placement.array_count is not None \
				or placement.sized_by is not None:
			return []
		if placement.type_name in self.structs:
			return []

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

	def _store(self, placement: Placement, scalar: ScalarType,
			offset: str | None = None) -> str:
		big   = placement.endian is not ast.Endian.LITTLE
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

		writer = "write_be" if big else "write_le"
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

			if check is Check.NOTHING:
				continue
			fits = self._fits_check(struct, placement)
			if fits:
				checks.extend(fits)
				continue
			if check is Check.DISCRIMINANT:
				checks.extend(self._discriminant_check(struct, placement))
				continue
			if check is Check.DELIMITED:
				checks.extend(self._delimiter_checks(struct, placement))
				continue
			if check is Check.REPEATED:
				checks.extend(self._array_checks(placement, name))
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
				checks.append(f"\t\tself.{name}().validate()?;")
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

	def _array_checks(self, placement: Placement, name: str) -> list[str]:
		checks: list[str] = []
		count = placement.array_count
		if count is None or placement.scalar is None:
			return checks
		if placement.scalar.bits != BITS_PER_BYTE:
			return checks

		if placement.kind == "reserved":
			policy = _reserved_policy(placement.attrs)
			if policy in ("must_be_zero", "must_be_one"):
				want  = 0 if policy == "must_be_zero" else 0xFF
				start = placement.offset_bytes
				checks.extend([
					f"\t\tif self.bytes[{start}..{start + count}]"
					f".iter().any(|&b| b != {want}) {{",
					"\t\t\treturn Err(Error::Constraint);",
					"\t\t}",
				])
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
