"""The Python backend (section 26.17).

Python reaches people who would otherwise not describe their format at all,
and it enforces the least of the lattice of any target situ has. Both are
reasons to build it; section 20.1 asks only that the second be said rather than
left for a reader to discover.

The surface is properties, not `get_x()` methods, because a Python caller who
has to write `packet.version()` will write the parser by hand instead -- and a
backend nobody uses enforces nothing at all. The cost that syntax hides is
recorded where situ records every cost: the capability map, and a docstring on
each field that quotes it.

`validate()` raises rather than returning a code, for the same reason. Idiom is
not a capability, and a return code a Python caller silently drops is worse
than an exception they have to catch.
"""

from __future__ import annotations

import keyword

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
	expand_calls, over_fields, python_spelling, render_delimiter,
	translate_operators,
)
from situc.propagate import Resolved
from situc.resolve import ResolvedSchema, ResolvedStruct
from situc.invariant import derived as derived_by
from situc.invariant import expression as invariant_expression
from situc.traverse import (
	codec_entry_point, decode_counts_bits,
	declared_value_bounds, pinned_bytes,
	is_own_member,
	Check, Member, arm_members, coded_spans, containment_order, covered_run,
	data_sized,
	dynamic_frame_owner,
	readable_names,
	decode_bound, region_extent, offset_plan,
	decodes_here, classify,
	classify_check, declares_its_own_length,
	extent_parts, frameable,
	extern_symbol, has_computable_extent, index_entry_bytes, indexed_elements,
	is_run,
	local_name,
	element_bytes, is_counted_run, matched_values, obligation,
	pad_alignment,
	preceding_parts,
	obligations, own_entries, own_members,
)
from situc.types import ScalarType, lookup
from situc.unparse import expr_to_source as unparse_expr
from situc import __version__



def _is_run(placement: Placement) -> bool:
	"""A member whose bytes are a run rather than one value.

	Three spellings, and the sealed-gate emitters knew two: a counted array,
	a member sized by a named field, and a member sized by *arithmetic over*
	one. The third has neither `array_count` nor `sized_by`, so
	`fragment[length - 24]` -- DTLS's whole encrypted payload -- fell through
	to the scalar branch and came back as a single byte in three backends
	while C read it correctly (26.188).

	`data_sized` alone is not the predicate: it is false for a *fixed* array,
	which is a run all the same, and substituting it dropped `[secret]`
	byte arrays out of the gate's own list. The union is what "is a run"
	means here.
	"""
	return (placement.array_count is not None
	        or placement.sized_by is not None
	        or data_sized(placement))


def _byte_order(endian: ast.Endian | None) -> str:
	"""The `int.from_bytes` order argument, as source.

	A third answer, and this had two: a field asked `is not LITTLE` and read
	`native` big-endian, while an indexed table's entry asked `is BIG` and
	read the same schema's `native` little-endian. `NATIVE_BIG` is a runtime
	constant so the choice is the target machine's.
	"""
	if endian is ast.Endian.NATIVE:
		return '("big" if NATIVE_BIG else "little")'
	return '"big"' if endian is ast.Endian.BIG else '"little"'


def _order(endian: ast.Endian | None) -> str:
	"""What the `big=` argument of a read or a write is, as source.

	`native` resolves in the generated module rather than here: situc runs on
	the machine building the code and not on the machine running it, so a
	byte order decided at generation time is right only by coincidence
	(invariant 8). It used to be neither -- `endian is not LITTLE` put native
	in the big-endian branch, so every field of a host-order schema came back
	byte-swapped on a little-endian machine, with nothing said.
	"""
	if endian is ast.Endian.NATIVE:
		return "NATIVE_BIG"
	return str(endian is not ast.Endian.LITTLE)


def _self_as(attrs: tuple[ast.Attr, ...]) -> int | None:
	"""What a self-covering tag's own bytes read as, or None (14.2)."""
	for attr in attrs:
		if attr.name == "self_as" and isinstance(attr.value, ast.IntLiteral):
			return int(attr.value.value)
	return None


def _pythonic(source: str) -> str:
	"""A schema expression as Python.

	The schema's operators are C's, which is a choice the language made once
	and every other backend is happy with. Python spells three of them in
	words, and emitting `||` produced a module that did not parse -- the same
	shape as `/` meaning float division here and integer division everywhere
	else (8.6.2).

	`!=` is left alone, and getting that ordering right is the whole of the
	difficulty -- which is why it lives in `names` now, where the Lua
	dissector asks the same question and would otherwise get it wrong
	separately.
	"""
	return translate_operators(source, conj=" and ", disj=" or ",
	                           ne="!=", neg="not ", div="//")


@dataclass
class Generated:
	"""One module per schema."""

	module: str
	basename: str
	warnings: list[Diagnostic] = field(default_factory=list)

	def files(self) -> dict[str, str]:
		return {f"{self.basename}.py": self.module}


def py_name(path: str) -> str:
	"""`c_name`, mangled where Python could not parse the result.

	The same job `bare_name` does for C and C++ and a different word list:
	`int` is a builtin here rather than a keyword and needs no help, while
	`class`, `def` and `lambda` cannot be a method, a class or an attribute.
	`keyword.iskeyword` is asked rather than a list restated, because the
	interpreter that will read this file is the authority on what it can
	parse.

	Applied to every identifier this backend emits rather than to member
	names alone: a *struct* named `class` is `class class:`, which fails in
	the same place for the same reason.

	Soft keywords are deliberately left alone. `match` and `type` parse fine
	as ordinary names -- that is what makes them soft -- and mangling them
	would rename a member for nothing.
	"""
	name = c_name(path)
	return f"{name}_" if keyword.iskeyword(name) else name


def generate(schema: ast.Schema, resolved: ResolvedSchema, basename: str,
		prefix: str = "situ", materialize: bool = False) -> Generated:
	return Generated(module=Emitter(schema, resolved, basename,
	                                materialize).module(),
	                 basename=basename)


class Emitter:
	def __init__(self, schema: ast.Schema, resolved: ResolvedSchema,
			basename: str, materialize: bool = False) -> None:
		self.schema   = schema
		self.resolved = resolved
		self.basename = basename
		#: Accessor paths this emitter actually wrote, recorded as it writes
		#: them. `validate` consults it rather than re-deriving whether an
		#: accessor exists: three backends each grew their own answer to that
		#: and each was wrong once (26.74, invariant 111). Whether *this*
		#: backend declined is not a layout fact, so it is asked of the
		#: emitter, which knows, rather than of the layout, which cannot.
		self._emitted: set[str] = set()
		self.enums    = {decl.name: decl for decl in schema.enums()}
		self.codecs   = {decl.name: decl for decl in schema.codecs()}
		self.markers  = {decl.name: decl for decl in schema.markers()}
		self.structs  = set(resolved.structs)
		#: Emit the second accessor family (decision 0022): the consumer's
		#: choice rather than the schema's, and off unless asked for.
		self.materialize = materialize

	def module(self) -> str:
		lines = [
			f'"""Generated by situc {__version__} from {self.basename}.situ -- do not edit.',
			"",
			"The operations below are the ones this schema's capability vectors",
			"support. Two axes do not survive the trip into Python and say so",
			"here rather than in each docstring: `atomic` means nothing, because",
			"Python has no single-instruction access, and `repr` costs what the",
			"map says it costs even though a property makes it look free.",
			'"""',
			"",
			"from __future__ import annotations",
			"",
			"import enum",
			*(["import sys"] if self._has_marker() else []),
			"",
			# Only where a walk hands one back: an unused import is a
			# warning in every linter a caller might run over this.
			*(["from collections.abc import Iterator", ""]
			  if self._tlv_items() else []),
			"from situ_runtime import (",
			"\tBoundsError, ConstraintError, Gate, Message, TruncatedError,",
			"\tVersionError, View,",
			"\tacquire,",
			"\tadvance,",
			"\tas_enum,",
			"\tascii_valid, bcd_decode, bcd_encode, bcd_valid, known_enum,",
			"\tleaf, nonneg,",
			*(["\tNATIVE_BIG,"] if self._has_native_order() else []),
			*(["\talign_up,"] if self._has_pad() else []),
			*(["\tutf16le_valid,"] if self._uses_utf16()[0] else []),
			*(["\tutf16be_valid,"] if self._uses_utf16()[1] else []),
			"\tcompose, nul_len, open_gate, utf8_valid,",
			*self._tlv_imports(),
			*self._delimited_imports(),
			")",
			"",
			# Annotated, because `__all__ = []` has no element type and a
			# caller running mypy over this gets `var-annotated` for it --
			# which `std/codecs.situ`, all signatures and no structs, is.
			"__all__: list[str] = [",
		]
		exported = [py_name(decl.name) for decl in self.schema.enums()]
		exported += [py_name(name) for name in sorted(self.structs)]
		lines.extend(f'\t"{name}",' for name in exported)
		lines.extend(["]", ""])

		for decl in self.schema.enums():
			lines.extend(self._enum(decl))

		# The item records first, at module scope. A nested class would work in
		# Python and would put a type a caller names inside the class it is
		# reached through, which reads as private.
		for struct, placement in self._tlv_items():
			assert placement.tlv_grammar is not None
			lines.extend(self._tlv_item_class(struct, placement,
			                                  placement.tlv_grammar))

		for name in self._order():
			lines.extend(self._struct(self.resolved.structs[name]))

		return self._unshadow("\n".join(lines) + "\n")

	def _tlv_items(self) -> list[tuple[ResolvedStruct, Placement]]:
		"""Every walkable tlv region, with the struct that holds it."""
		return [(struct, entry.placement)
		        for struct in self.resolved.structs.values()
		        for entry in struct.entries
		        if entry.placement.kind == "tlv"
		        and entry.placement.tlv_grammar is not None
		        and entry.placement.tlv_grammar.walkable]

	def _delimited_imports(self) -> list[str]:
		"""The section 8.6 helpers, imported only where something uses one.

		A generated module that imports what it does not use is noise a reader
		learns to skim, and `digits_minimal` in particular would have said the
		schema asked for a canonicity check when it had not (invariant 23).
		"""
		placements = [entry.placement
		              for struct in self.resolved.structs.values()
		              for entry in struct.entries]

		needed = []
		if any(p.delimiter is not None for p in placements):
			needed.append("scan")
		if any(p.radix is not None for p in placements):
			needed.append("parse_uint")
		if any(p.radix_minimal for p in placements):
			needed.append("digits_minimal")
		if any(p.trimmed for p in placements):
			needed.append("trim")
		if any(p.case_insensitive for p in placements):
			needed.append("ascii_ci_eq")

		return [f"\t{', '.join(sorted(needed))},"] if needed else []

	def _order(self) -> list[str]:
		"""Contained structs first: a class body names them at definition time."""
		return containment_order(self.resolved.structs, sorted(self.structs))

	# -- enums ---------------------------------------------------------

	def _enum(self, decl: ast.EnumDecl) -> list[str]:
		values = self.resolved.layout.env.enums[decl.name]
		lines  = [
			"",
			f"class {py_name(decl.name)}(enum.IntEnum):",
			f'\t"""enum {decl.name} : {decl.backing.name} --'
			f' unknown values are {decl.effective_default.value}."""',
			"",
		]
		lines.extend(f"\t{py_name(member.name)} = {values[member.name]}"
		             for member in decl.members)
		lines.append("")

		# An alias, where some struct has a member of the same name. Inside a
		# class body the member's property shadows the enum, so every
		# annotation written *after* it resolves to the property object --
		# `def set_protocol(self, value: protocol | int)` in an IPv4 header,
		# where `protocol` is both the enum and the field. Decision 0025 makes
		# the same move in C++ for the same reason: rename the reference, not
		# the name the schema chose.
		if py_name(decl.name) in self._shadowed_enums():
			lines.extend([
				f"#: `{decl.name}` again, for annotations inside a class that",
				"#: has a member of that name and so shadows it.",
				f"_situ_{py_name(decl.name)} = {py_name(decl.name)}",
				"",
			])
		return lines

	#: Builtins the generated annotations name. A member may be called any of
	#: these -- `bytes` is an ordinary field name -- and a property of that
	#: name is a class-scope binding that hides the builtin for every
	#: annotation after it, so `data: bytes | bytearray | memoryview` stops
	#: being a type. The runtime `View` also owns `bytes`, so such a member
	#: overrode it and the generated `self._span` read the member instead.
	#: Found by `std/image.situ` (26.80).
	SHADOWABLE = ("bytes", "bytearray", "memoryview", "int", "str", "bool",
	              "float")

	def _shadowed_builtins(self) -> list[str]:
		"""Builtins some struct also uses as a member name."""
		members = {py_name(entry.placement.name)
		           for struct in self.resolved.structs.values()
		           for entry in struct.entries}
		return [name for name in self.SHADOWABLE if name in members]

	def _unshadow(self, text: str) -> str:
		"""Point the annotations at an alias no member can hide.

		The same mechanism this backend already uses for an enum a member has
		named, extended to the builtins. Applied once over the module this
		emitter has just produced, which is a second pass with complete
		knowledge of the first rather than the re-reading section 25 forbids:
		the names substituted are computed from the AST, and only where a
		member actually took one.
		"""
		shadowed = self._shadowed_builtins()
		if not shadowed:
			return text

		import re as _re
		alias = "".join(f"_situ_{name} = {name}\n" for name in shadowed)
		text  = text.replace("\nfrom situ_runtime import",
		                     f"\n{alias}\nfrom situ_runtime import", 1)
		for name in shadowed:
			text = _re.sub(rf"(:|->) {name}\b", rf"\1 _situ_{name}", text)
		return text

	def _shadowed_enums(self) -> set[str]:
		"""Enum names some struct also uses as a member name."""
		members = {py_name(entry.placement.name)
		           for struct in self.resolved.structs.values()
		           for entry in struct.entries}
		return {py_name(name) for name in self.enums} & members

	# -- structs -------------------------------------------------------

	def _struct(self, struct: ResolvedStruct) -> list[str]:
		layout = struct.layout
		name   = py_name(struct.name)

		if layout.register is not None:
			return self._register(struct)

		lines = ["", f"class {name}(View):", *self._class_doc(struct)]
		lines.extend(self._acquire(struct))
		lines.extend(self._dirty_constants(struct))
		lines.extend(self._offsets(struct))

		for entry in own_entries(struct):
			lines.extend(self._explained(struct, entry))

		lines.extend(self._region_runs(struct))
		lines.extend(self._nested_text_values(struct))

		lines.extend(self._covered_nested_setters(struct))
		lines.extend(self._arm_accessors(struct))
		lines.extend(self._extent_property(struct))
		lines.extend(self._required(struct))
		lines.extend(self._invariants(struct))
		lines.extend(self._validate(struct))
		lines.extend(self._gates(struct))
		lines.append("")
		return lines

	def _region_runs(self, struct: ResolvedStruct) -> list[str]:
		"""A run of records inside a region, walked from out here.

		The interior is reached through the gate; how far the run *reaches* is
		a different question, and it is what places every member after the
		region and what the tag covering it spans. `own_entries` drops a
		dotted path, so this backend had no walk while its own accessors named
		one -- an `AttributeError` at the first read. The other three emit the
		same family for the same reason.
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

	def _covered_nested_setters(self, struct: ResolvedStruct) -> list[str]:
		"""A covered write for a field of a *nested* struct, on the parent.

		`own_entries` drops a dotted path and a nested struct's fields have
		one, so a covered field inside one never reached the branch that marks
		the bit -- and the only way to write it was the nested type's own
		setter, which marks nothing. The map says `auth = Covered(t)` about
		that field and 14.2 says a covered write leaves `t` stale (26.35).

		The nested type keeps its plain setter: it may sit where nothing
		covers it, and the type cannot know.
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

			# Not a sealed member. This wrote one at its plaintext offset from
			# the *outer* view, so a caller could put bytes inside a sealed
			# region without ever verifying its tag -- section 14.3's stage
			# gate, on the side that escapes the object (invariant 28). C has
			# demanded the gate here since the machinery landed. The gated
			# setter is emitted with the rest of the interior.
			if placement.sealed_by is not None:
				continue

			name = py_name(local_name(struct, placement))
			tags = ", ".join(placement.covered_by)
			hint = "int"
			# The offset may be the message's: a nested struct behind a
			# variable-length member has one, and asking the placement for a
			# constant crashed the compiler on the assertion inside
			# `offset_bytes`. Three backends did; C reads its own offset
			# function here and always has.
			start = (None if placement.offset_bits is not None
			         else self._offset_expression(struct, placement))
			# ...and a field of a nested struct behind a variable-length
			# member has a *frame-relative* one, measured from the nested
			# struct rather than from this view. Adding it to the parent's
			# base wrote `s.head.seq` over the top of `s.n`.
			owner = dynamic_frame_owner(struct, placement)
			if owner is not None:
				where = self._offset_expression(struct, owner)
				start = (None if where is None
				         else f"({where}) + {placement.offset_bytes}")
			if (placement.offset_bits is None or owner is not None) \
					and start is None:
				lines.extend([
					"",
					f"\t# No set_{name}(): this backend cannot resolve where",
					f"\t# {placement.path} starts.",
				])
				continue
			width = max(1, scalar.bits // BITS_PER_BYTE)
			guard = ([] if start is None else [
				f"\t\tif not (self._len - ({start}) >= {width}):",
				"\t\t\t# Its offset is a sum of lengths the message chose, and",
				"\t\t\t# the frame does not reach it.",
				"\t\t\treturn",
			])
			lines.extend([
				"",
				f"\tdef set_{name}(self, msg: Message, value: {hint}) -> None:",
				f'\t\t"""Write {placement.path} and mark {tags} stale.',
				"",
				"\t\tOn the parent, because the nested type's own setter marks",
				'\t\tnothing -- it may sit where nothing covers it."""',
				*guard,
				f"\t\t{self._store(placement, scalar, start)}",
				f"\t\tmsg.mark_dirty({self._tag_bit(struct, placement)})",
			])

		return lines

	# -- invariants (section 16.1) --------------------------------------

	def _invariants(self, struct: ResolvedStruct) -> list[str]:
		"""A derived field, and the one thing allowed to write it.

		The property has no setter -- `mutate` is Immutable and the docstring
		says which invariant decided that -- so without this the schema could
		state a relationship and never satisfy it.

		`recompute_x` rather than a `x.setter` on purpose. Assignment syntax
		for something that also clears a dirty bit reads as though it were
		free, and the C backend makes the same refusal for the same reason: a
		schema that means one thing in C must not mean another here.
		"""
		lines: list[str] = []

		for decl in derived_by(self.schema, struct):
			field = decl.derived.partition(".")[2]
			entry = next((e for e in struct.entries
			              if e.placement.path == f"{struct.name}.{field}"), None)
			if entry is None or entry.placement.scalar is None:
				continue

			name  = py_name(field)
			value = invariant_expression(struct, decl.expr, self)
			if value is None:
				lines.extend([
					"",
					f"\t# No recompute_{name}: this backend cannot evaluate",
					f"\t# `{unparse_expr(decl.expr)}` at run time. The refusal to",
					"\t# write the field directly still stands, so the invariant",
					"\t# cannot be broken -- only left unsatisfiable here.",
				])
				continue

			held = obligation(self.schema, struct, f"invariant {field}")
			assert held is not None, "the layout solver recorded this"
			# Named, like the other three. It was the literal here.
			bit = f"self.DIRTY_{py_name(held.name).upper()}"

			lines.extend([
				"",
				f"\tdef recompute_{name}(self) -> None:",
				f'\t\t"""{decl.derived} == {unparse_expr(decl.expr)}.',
				"",
				"\t\tWriting anything the right side reads marks this stale, and",
				"\t\t`Message.transmittable` refuses until this has run.",
				'\t\t"""',
				"\t\tself._check()",
				f"\t\tvalue = {value}",
				f"\t\t{self._store(entry.placement, entry.placement.scalar)}",
				f"\t\tself._msg.clear_dirty({bit})",
				"",
				f"\tdef {name}_is_stale(self) -> bool:",
				f"\t\treturn bool(self._msg.dirty & {bit})",
			])

		return lines

	# -- invariant.Terms, in Python -------------------------------------

	def literal(self, value: int) -> str:
		return str(value)

	def binary(self, op: str, left: str, right: str) -> str:
		# `/` is float division in Python and integer division everywhere else
		# situ emits. A size is a whole number of bytes in every backend, so
		# this is the one place the same expression needs different spelling
		# rather than a different meaning.
		return f"({left} {'//' if op == '/' else op} {right})"

	def offset(self, struct: ResolvedStruct, placement: Placement) -> str | None:
		return (str(placement.offset_bytes) if placement.offset_bits is not None
		        else None)

	def size(self, struct: ResolvedStruct, placement: Placement) -> str | None:
		if placement.is_fixed_size:
			return str(placement.size_bits // BITS_PER_BYTE)
		return self._length_expression(struct, placement)

	def count(self, struct: ResolvedStruct, placement: Placement) -> str | None:
		return self._count_expression(struct, placement)

	def value(self, struct: ResolvedStruct,
			placement: Placement) -> str | None:
		"""What a sibling holds, for a bound that names one."""
		if placement.scalar is None or placement.array_count is not None:
			return None
		if not is_own_member(struct, placement):
			return None
		return f"int(self.{py_name(local_name(struct, placement))})"

	def bound_literal(self, value: int) -> str:
		"""Plain, because a bound is compared against a widened value."""
		return str(value)


	def _register(self, struct: ResolvedStruct) -> list[str]:
		"""A register's word composition, without pretending to be a driver.

		Python cannot promise `volatile` -- there is no way to tell the
		interpreter that a read has a side effect. What it can do exactly is the
		arithmetic: compose a whole word from its fields and hand it back for
		the caller to write however they reach the bus. That is the shape
		section 15's headline asks for anyway, since a partial-width field in a
		`no_rmw` register cannot be written alone.
		"""
		info = struct.layout.register
		assert info is not None

		name  = py_name(struct.name)
		lines = [
			"",
			f"class {name}:",
			f'\t"""register {struct.name}'
			+ (f" at {info.address:#x}" if info.address is not None else "")
			+ f': {info.width} bits.',
			"",
			"\tPython cannot promise `volatile`, so this composes words and does",
			"\tnot drive a bus. Read and write through whatever addresses the",
			"\tdevice -- an mmap of /dev/mem, a probe, a simulator -- and use",
			'\t`word` to build what you send."""',
			"",
			f"\tWIDTH = {info.width}",
		]
		if info.address is not None:
			lines.append(f"\tADDRESS = {info.address:#x}")

		lines.extend([
			"",
			"\tclass word:",
			'\t\t"""A copy of the bits. Composing costs no transaction."""',
			"",
			"\t\t__slots__ = (\"raw\",)",
			"",
			"\t\tdef __init__(self, raw: int = 0) -> None:",
			"\t\t\tself.raw = raw",
		])

		for entry in own_entries(struct):
			lines.extend(self._register_field(entry))

		lines.append("")
		return lines

	def _register_field(self, entry: Resolved) -> list[str]:
		placement = entry.placement
		scalar    = placement.scalar

		if placement.kind == "reserved":
			return ["",
			        f"\t\t# {placement.path} is reserved: no accessor, and its",
			        "\t\t# bits are carried through a compose untouched."]
		if scalar is None:
			return []

		name  = py_name(placement.path.rsplit(".", 1)[-1])
		mode  = placement.access_mode or ast.AccessMode.RW
		shift = placement.offset_bits or 0
		mask  = (1 << scalar.bits) - 1
		lines = ["", f"\t\t# {placement.path}: {mode.value},"
		         f" bits {shift}..{shift + scalar.bits - 1}"]

		if mode.readable:
			lines.extend([
				"\t\t@property",
				f"\t\tdef {name}(self) -> int:",
				f"\t\t\treturn (self.raw >> {shift}) & {mask:#x}",
			])
		else:
			lines.append(f"\t\t# No {name}: the mode is {mode.value}, so a read"
			             f" returns nothing the field holds.")

		if mode.writable and mode.is_assignment:
			lines.extend([
				f"\t\tdef with_{name}(self, value: int) -> \"{{}}\":".format(
					py_name(entry.placement.path.split(".")[0]) + ".word"),
				f'\t\t\t"""A new word with {name} set; the rest untouched."""',
				f"\t\t\treturn type(self)(compose(self.raw, int(value),"
				f" {shift}, {mask:#x}))",
			])
		elif mode.writable:
			lines.append(f"\t\t# No with_{name}: `{mode.value}` is not an"
			             f" assignment.")
		else:
			lines.append(f"\t\t# No with_{name}: the mode is {mode.value}.")
		return lines

	def _class_doc(self, struct: ResolvedStruct) -> list[str]:
		layout = struct.layout
		extent = (f"{layout.size_bytes} bytes, fixed" if layout.is_fixed_size
		          else f"{layout.size_bytes} bytes and up")
		return [f'\t"""struct {struct.name}: {extent}."""', ""]

	def _acquire(self, struct: ResolvedStruct) -> list[str]:
		layout = struct.layout

		if layout.is_fixed_size:
			return [
				f"\tSIZE_BYTES = {layout.size_bytes}",
				"",
				"\t@classmethod",
				f"\tdef at(cls, msg: Message, offset: int = 0) -> \"{py_name(struct.name)}\":",
				'\t\t"""The one bounds check. Everything after it trusts the extent."""',
				"\t\treturn acquire(cls, msg, offset, cls.SIZE_BYTES)"
				f"  # type: ignore[return-value]",
			]

		return [
			f"\tSIZE_MIN = {layout.size_bytes}",
			"",
			"\t@classmethod",
			f"\tdef at(cls, msg: Message, offset: int, length: int)"
			f" -> \"{py_name(struct.name)}\":",
			'\t\t"""Nothing in the bytes says where the frame ends, so the',
			"\t\tcaller supplies it. That is the one bounds check, and the",
			"\t\tminimum is part of it: every constant-offset accessor below",
			"\t\ttrusts that the fixed members are here (20.2), which a frame",
			'\t\tshorter than the minimum does not carry."""',
			"\t\tif length < cls.SIZE_MIN:",
			"\t\t\traise BoundsError(",
			f'\t\t\t\tf"{struct.name} needs at least {{cls.SIZE_MIN}} bytes;'
			' {length} given")',
			"\t\treturn acquire(cls, msg, offset, length)"
			f"  # type: ignore[return-value]",
		]

	# -- members -------------------------------------------------------

	def _has_marker(self) -> bool:
		"""Whether anything here resolves its byte order from the data.

		`sys.byteorder` is what the host constant is built from, and importing
		it where nothing uses one is the noise `_delimited_imports` exists to
		avoid.
		"""
		return any(entry.placement.kind == "marker"
		           for struct in self.resolved.structs.values()
		           for entry in struct.entries)

	def _has_pad(self) -> bool:
		"""Whether any placement is `pad_to(n)` padding (0043), so the import
		is added only where the generated code uses it."""
		return any(entry.placement.pad_to is not None
		           for struct in self.resolved.structs.values()
		           for entry in struct.entries)

	def _uses_utf16(self) -> tuple[bool, bool]:
		"""Whether any `[encoding]` names utf16le or utf16be (0044), each
		imported only where used for `_delimited_imports`' reason -- an unused
		import is a warning every caller's linter would raise (invariant 23)."""
		names = {getattr(attr.value, "name", None)
		         for struct in self.resolved.structs.values()
		         for entry in struct.entries
		         for attr in entry.placement.attrs
		         if attr.name == "encoding"}
		return ("utf16le" in names, "utf16be" in names)

	def _has_native_order(self) -> bool:
		"""Whether anything here is `endian native` (8.3).

		Imported only where used, for `_delimited_imports`' reason: an unused
		import is a warning in every linter a caller might run over this, and
		invariant 23 says generated code that warns teaches a reader to ignore
		warnings.
		"""
		return any(entry.placement.endian is ast.Endian.NATIVE
		           for struct in self.resolved.structs.values()
		           for entry in struct.entries)

	def _tlv_imports(self) -> list[str]:
		"""`varint_get`, where something walks a tlv region."""
		placements = [entry.placement
		              for struct in self.resolved.structs.values()
		              for entry in struct.entries]
		varints    = [p for p in placements if p.varint is not None]
		declared   = {decl.name: decl for decl in self.schema.varints()}

		def held(name: str) -> list[ast.VarintDecl]:
			return [declared[p.varint] for p in varints
			        if p.varint in declared
			        and declared[p.varint].encoding.value == name]

		needed = []
		if self._tlv_items() or held("leb128"):
			needed.append("varint_get")
		if held("be128"):
			needed.append("varint_be_get")
		if any(decl.minimal for decl in held("leb128")):
			needed.append("varint_len")
		if any(decl.minimal for decl in held("be128")):
			needed.append("varint_be_len")
		if any(declared[p.varint].transform is not None for p in varints
		       if p.varint in declared):
			needed.append("zigzag_decode")
		return [f"\t{', '.join(needed)}," ] if needed else []

	def _indexed_region(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""An offset table, then the elements it reaches (section 9.3).

		The region answered `REGION` in the shared classifier and this backend
		said "not emitted by this backend yet" -- the fallthrough note, for the
		last construct no backend reached into.
		"""
		table = placement.index_table
		if table is None:
			return []

		start = self._offset_expression(struct, placement)
		if start is None:
			return ["", f"\t# {placement.path}: this backend cannot resolve"
			        " where the region starts."]

		name    = py_name(local_name(struct, placement))
		width   = table.entry_bits // BITS_PER_BYTE
		element = self.resolved.structs.get(table.element or "")
		order   = _byte_order(placement.endian)

		count = self._count_expression(struct, placement)
		if count is None:
			if table.count_fixed is None:
				return ["", f"\t# {placement.path}: this backend cannot resolve"
				        " how many entries",
				        "\t# the table holds, so nothing below could be"
				        " bounded."]
			count = str(table.count_fixed)

		lines = [
			"",
			"\t@property",
			f"\tdef {name}_count(self) -> int:",
			f'\t\t"""How many entries the table holds.',
			"",
			f"\t\t`{placement.path}` is an offset table of {width}-byte"
			f" entries, then",
			"\t\tthe elements it reaches. Element N is one read of entry N"
			" plus a",
			"\t\tbase, whatever the elements weigh -- which is why `access`"
			" stays",
			"\t\tRandom through a region whose elements need not be the same"
			" size.",
			"",
			"\t\tInsertion is not an operation here: every offset after the",
			'\t\tinsertion point would have to move."""',
			f"\t\treturn {count}",
			"",
			f"\tdef {name}_offset(self, index: int) -> int:",
			f'\t\t"""The offset held in entry `index`, as written -- measured',
			f'\t\tfrom {self._index_base_noun(table)}."""',
			"\t\tself._check()",
			f"\t\tif not 0 <= index < self.{name}_count:",
			f'\t\t\traise IndexError(f"{placement.path}[{{index}}]")',
			"",
			f"\t\tat = {start} + index * {width}",
			f"\t\tif at + {width} > self._len:",
			f'\t\t\traise BoundsError(f"entry {{index}} runs past the region")',
			"",
			f"\t\treturn int.from_bytes(self._span[at:at + {width}],"
			f" {order})",
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
		"""`at`: the element an entry reaches."""
		if element is None or (not element.layout.is_fixed_size
		                       and self._extent_expression(element) is None):
			held = table.element or placement.type_name
			return ["", f"\t# No `{name}_at`: one `{held}` has no extent this"
			        " backend can",
			        "\t# compute, so an entry gives a position and not a view."
			        " The offsets",
			        "\t# are still readable above."]

		inner  = py_name(element.name)
		origin = (self._index_member_base(struct, table)
		          if table.base == "member" else
		          "0" if table.base == "message" else start)
		# A message-relative offset is measured from the buffer, not from this
		# frame, so the element is taken over the message rather than over the
		# region. `self._at` is where this view begins.
		anchor = ("0" if table.base == "message" else "self._at")

		return [
			"",
			f"\tdef {name}_at(self, index: int) -> {inner}:",
			f'\t\t"""Element `index`, whose offset is measured from',
			f'\t\t{self._index_base_noun(table)}."""',
			f"\t\tstart = {origin} + self.{name}_offset(index)",
			*self._index_element_extent(element, inner, anchor),
		]

	def _index_member_base(self, struct: ResolvedStruct,
			table: IndexTable) -> str:
		found = self.resolved.find(f"{struct.name}.{table.base_member}")
		if found is None:
			return "0"
		return self._offset_expression(struct, found.placement) or "0"

	def _index_element_extent(self, element: ResolvedStruct, inner: str,
			anchor: str) -> list[str]:
		"""Narrow to one element, measuring it first where it varies."""
		if element.layout.is_fixed_size:
			return [
				f"\t\treturn {inner}(self._msg, {anchor} + start,"
				f" {inner}.SIZE_BYTES)",
			]

		return [
			"",
			"\t\t# The extent is in the element's own bytes, so it takes a"
			" view to",
			"\t\t# read and a view is what it decides. Measure over the rest"
			" of the",
			"\t\t# region, then narrow.",
			"\t\tif start > self._len:",
			f'\t\t\traise BoundsError(f"element {{index}} starts past the'
			f' region")',
			f"\t\tprobe = {inner}(self._msg, {anchor} + start,"
			f" self._len - start)",
			f"\t\treturn {inner}(self._msg, {anchor} + start, probe._extent)",
		]

	def _tlv_region(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""A run of tag-length-value items, walked as the schema describes them.

		The region answered `REGION` in the shared classifier and this backend
		said "not emitted by this backend yet" -- the fallthrough note, for the
		one construct section 9.7 makes the conformance gate.

		Iteration is a generator, which is where this backend departs from the
		other three on purpose: `for item in msg.fields()` is the shape a
		Python caller reaches for, and `first`/`next` is a cursor protocol
		three languages need because they have no other way to say it. Both are
		here -- the cursor because parity across backends is what makes them
		comparable, the generator because a module nobody enjoys using is a
		module nobody checks against the wire.
		"""
		grammar = placement.tlv_grammar
		if grammar is None or not grammar.walkable:
			return ["", f"\t# No accessors for {placement.path}: the region"
			        " says how its items are",
			        "\t# tagged and not how long their values are, so a walk"
			        " has nowhere to",
			        "\t# put the second item."]

		start = self._offset_expression(struct, placement)
		if start is None:
			return ["", f"\t# {placement.path}: this backend cannot resolve"
			        " where the region starts."]

		name = py_name(local_name(struct, placement))

		item  = self._tlv_item_name(placement)
		lines = self._tlv_read(placement, grammar, name)
		lines.extend(self._tlv_cursor(name, start, item))
		lines.extend(self._tlv_by_name(grammar, name, item))
		return lines

	def _tlv_item_class(self, struct: ResolvedStruct, placement: Placement,
			grammar: TlvGrammar) -> list[str]:
		"""The item, at module scope beside the struct classes.

		`__slots__` and a written-out `__init__` rather than a dataclass. A
		dataclass resolves its annotations through `sys.modules[cls.__module__]`
		under `from __future__ import annotations`, so a module loaded any way
		that does not register it there -- `exec_module` on a spec, which is
		how the example suite loads these -- raises on the class body. A
		generated module should not care how it was imported.
		"""
		fields = (["at", "next", "tag"]
		          + [part.name for part in grammar.tag_decode]
		          + ["value_at", "value_len"])
		width  = max(len(name) for name in fields)
		listed = ", ".join(f'"{name}"' for name in fields)
		noted  = {part.name: part.source for part in grammar.tag_decode}

		return [
			"",
			"",
			f"class {self._tlv_item_name(placement)}:",
			f'\t"""One item of {placement.path}, and where the next starts.',
			"",
			"\tThe decoded parts are named by the schema; a backend inventing",
			'\tits own would be describing protobuf rather than this region."""',
			"",
			f"\t__slots__ = ({listed},)",
			"",
			f"\tdef __init__(self, {', '.join(f'{name}: int' for name in fields)}"
			f") -> None:",
			*[f"\t\tself.{name.ljust(width)} = {name}"
			  + (f"\t# {noted[name]}" if name in noted else "")
			  for name in fields],
			"",
			"\tdef __repr__(self) -> str:",
			f'\t\treturn ("{self._tlv_item_name(placement)}("',
			*[f'\t\t        f"{name}={{self.{name}}}'
			  + (')")' if name == fields[-1] else ', "') for name in fields],
			"",
		]

	def _tlv_tag_bytes(self, placement: Placement) -> int:
		declared = next((decl for decl in self.schema.varints()
		                 if decl.name == placement.tlv_tag_varint), None)
		bits = declared.max_bits if declared is not None else 64
		return (bits + 6) // 7

	def _tlv_item_name(self, placement: Placement) -> str:
		holder = placement.path.partition(".")[0]
		return f"{py_name(holder)}_{py_name(placement.name)}_item"

	def _tlv_read(self, placement: Placement, grammar: TlvGrammar,
			name: str) -> list[str]:
		"""Read the item at `at`: its tag, its parts, and where its value ends."""
		item     = self._tlv_item_name(placement)
		max_tag  = self._tlv_tag_bytes(placement)
		decoded  = [f"\t\t\t{part.name} = ({part.source}),"
		            for part in grammar.tag_decode]

		lines = [
			"",
			f"\tdef _{name}_read(self, at: int) -> \"{item}\":",
			f'\t\t"""The item at `at`.',
			"",
			"\t\tRaises BoundsError where the region ends or an item runs past",
			"\t\tit, and ConstraintError for a wire type this schema does not",
			'\t\tdescribe."""',
			"\t\tdata = self._span",
			"\t\tif at >= len(data):",
			f'\t\t\traise BoundsError(f"no item at {{at}}: the region ends at'
			f' {{len(data)}}")',
			"",
			f"\t\tread = varint_get(data, at, {max_tag})",
			"\t\tif read is None:",
			f'\t\t\traise BoundsError(f"the tag at {{at}} runs past the region")',
			"\t\ttag, used = read",
			"\t\tstart = at",
			"\t\tat = at + used",
			"",
		]

		lines.extend(self._tlv_value_extent(grammar, max_tag, placement.endian))
		lines.extend([
			"",
			"\t\tif size > len(data) - at:",
			f'\t\t\traise BoundsError(f"an item of {{size}} bytes at {{at}}'
			f' runs past the region")',
			"",
			f"\t\treturn {item}(",
			"\t\t\tat = start,",
			"\t\t\tnext = at + size,",
			"\t\t\ttag = tag,",
			*decoded,
			"\t\t\tvalue_at = at,",
			"\t\t\tvalue_len = size,",
			"\t\t)",
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
		lines = [f"\t\tchosen = ({selector.source if selector else 'tag'})"]

		first = True
		for rule in grammar.rules:
			if rule.label is None:
				continue
			lines.append(f"\t\t{'if' if first else 'elif'} chosen"
			             f" == {rule.label}:")
			lines.extend(self._tlv_one_rule(rule, max_tag, endian))
			first = False

		default = next((rule for rule in grammar.rules if rule.label is None),
		               None)
		lines.append("\t\telse:" if not first else "\t\tif True:")
		if default is None or default.kind == "error":
			lines.extend([
				"\t\t\t# `default: error`: a wire type this schema does not",
				"\t\t\t# describe, so where the value ends is not knowable.",
				"\t\t\traise ConstraintError(",
				'\t\t\t\tf"wire type {chosen} is not one this schema sizes")',
			])
		else:
			lines.extend(self._tlv_one_rule(default, max_tag, endian))
		return lines

	def _tlv_one_rule(self, rule: ValueRule, max_tag: int,
			endian: ast.Endian | None) -> list[str]:
		if rule.kind == "fixed":
			return [f"\t\t\tsize = {rule.size}"]
		if rule.kind == "error":
			return ["\t\t\traise ConstraintError(",
			        '\t\t\t\tf"wire type {chosen} is not one this schema sizes")']
		if rule.kind == "self_delimiting":
			# The value carries its own extent, so reading it is measuring it.
			return [
				f"\t\t\tcarried = varint_get(data, at, {max_tag})",
				"\t\t\tif carried is None:",
				'\t\t\t\traise BoundsError(',
				'\t\t\t\t\tf"a self-delimiting value at {at} runs past the'
				' region")',
				"\t\t\tsize = carried[1]",
			]
		return self._tlv_prefixed_size(rule.length_type or "u8", "\t\t\t",
		                               endian)

	def _tlv_prefixed_size(self, length_type: str, indent: str,
			endian: ast.Endian | None) -> list[str]:
		"""`prefixed(T)`: a length in T, then that many bytes."""
		declared = next((decl for decl in self.schema.varints()
		                 if decl.name == length_type), None)

		if declared is not None:
			width = (declared.max_bits + 6) // 7
			return [
				f"{indent}prefix = varint_get(data, at, {width})",
				f"{indent}if prefix is None:",
				f'{indent}\traise BoundsError(',
				f'{indent}\t\tf"a length prefix at {{at}} runs past the region")',
				f"{indent}length, used = prefix",
				f"{indent}at = at + used",
				f"{indent}if length > len(data) - at:",
				f'{indent}\traise BoundsError(',
				f'{indent}\t\tf"a value of {{length}} bytes at {{at}} runs past'
				f' the region")',
				f"{indent}size = length",
			]

		scalar = lookup(length_type)
		width  = (scalar.bits + 7) // 8 if scalar is not None else 1
		order  = _byte_order(endian)
		return [
			f"{indent}if len(data) - at < {width}:",
			f'{indent}\traise BoundsError(',
			f'{indent}\t\tf"a length prefix at {{at}} runs past the region")',
			f"{indent}size = int.from_bytes(data[at:at + {width}], {order})",
			f"{indent}at = at + {width}",
		]

	def _tlv_cursor(self, name: str, start: str, item: str) -> list[str]:
		"""The cursor, the generator over it, and the value's bytes.

		Annotated, like everything else this backend emits. These were not,
		so a caller who runs mypy over the module -- which is the only way a
		type hint is worth writing -- got `no-untyped-def` on the walk and
		`no-untyped-call` at every use of it (26.35).
		"""
		return [
			"",
			f"\tdef {name}_first(self) -> {item}:",
			f'\t\t"""The first item. Raises BoundsError if the region is'
			f' empty."""',
			f"\t\treturn self._{name}_read({start})",
			"",
			f"\tdef {name}_next(self, item: {item}) -> {item}:",
			f'\t\t"""The item after this one."""',
			f"\t\treturn self._{name}_read(item.next)",
			"",
			f"\tdef {name}_value(self, item: {item}) -> memoryview:",
			f'\t\t"""This item\'s value. Zero copy, like every other read'
			f' here."""',
			"\t\treturn self._span[item.value_at:item.value_at + item.value_len]",
			"",
			f"\tdef {name}(self) -> Iterator[{item}]:",
			f'\t\t"""Every item, in order.',
			"",
			"\t\tA generator: the region is walked either way, and stopping",
			"\t\tearly should not have cost a walk to the end. Ends at the",
			'\t\tfirst item that does not parse, like the cursor does."""',
			f"\t\ttry:",
			f"\t\t\titem = self.{name}_first()",
			"\t\t\twhile True:",
			"\t\t\t\tyield item",
			f"\t\t\t\titem = self.{name}_next(item)",
			"\t\texcept (BoundsError, ConstraintError):",
			"\t\t\treturn",
			"",
			# A property, like every other count this backend emits. It was a
			# method here alone -- `view.cells_count` beside
			# `view.fields_count()` -- which is one question with two spellings
			# in one language, found by asking the four backends the same
			# question and having to spell this one differently.
			"\t@property",
			f"\tdef {name}_count(self) -> int:",
			f'\t\t"""How many items are present. A walk: nothing in the region',
			'\t\trecords a count."""',
			f"\t\treturn sum(1 for _ in self.{name}())",
		]

	def _tlv_by_name(self, grammar: TlvGrammar, name: str,
			item: str) -> list[str]:
		"""`find`, and one accessor per tag the schema names."""
		if not grammar.known:
			return []

		keyed = f"item.{grammar.identity}" if grammar.identity else "item.tag"
		named = (f"the part `{grammar.identity}` decodes to" if grammar.identity
		         else "the raw tag")

		lines = [
			"",
			f"\tdef {name}_find(self, tag: int) -> {item}:",
			f'\t\t"""The first item whose tag is `tag`, matched against {named}',
			"\t\t(decision 0023).",
			"",
			"\t\tO(n): the region is walked from the start, which is what",
			'\t\t`access = Sequential` costs."""',
			f"\t\tfor item in self.{name}():",
			f"\t\t\tif {keyed} == tag:",
			"\t\t\t\treturn item",
			f'\t\traise BoundsError(f"no item with tag {{tag}} in this region")',
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
				f"\tdef {py_name(known.name)}(self) -> {item}:",
				f'\t\t"""`{known.name}`: {described}."""',
				f"\t\treturn self.{name}_find({known.tag})",
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

	def _dirty_constants(self, struct: ResolvedStruct) -> list[str]:
		"""One class constant per obligation over this struct's bytes.

		The numbering is `traverse.obligations`, shared with the other three:
		a caller reading a bit out of one language's generated code and
		checking it against another's must find the same answer. This backend
		had no constants at all and wrote the literal into each setter --
		`msg.mark_dirty(1)` -- so the bit was a magic number here and a named
		one everywhere else.
		"""
		held = obligations(self.schema, struct)
		if not held:
			return []

		lines = ["",
		         "\t# Dirty bits. A covered write sets one; the message is not",
		         "\t# transmittable until it is cleared -- a tag by being",
		         "\t# recomputed and finalized, a derived field by its recompute."]
		lines.extend(f"\tDIRTY_{py_name(one.name).upper()} = {hex(1 << one.bit)}"
		             for one in held)
		lines.append(f"\tDIRTY_MASK = {hex((1 << len(held)) - 1)}")
		return lines

	def _opaque(self, struct: ResolvedStruct, placement: Placement) -> list[str]:
		"""Treat-as-bytes, the whole of what an `opaque` region supports (9.4).

		It reached the fallthrough note, which claims the language does not
		support the construct.
		"""
		name   = py_name(local_name(struct, placement))
		start  = self._offset_expression(struct, placement)
		length = self._length_expression(struct, placement)
		if start is None or length is None:
			return ["", f"\t# {placement.path}: this backend cannot resolve"
			        " where the region is."]

		return [
			"",
			"\t@property",
			f"\tdef {name}(self) -> memoryview:",
			f'\t\t"""{placement.path}: bytes and nothing more.',
			"",
			"\t\tAn opaque region has no interior to address -- that is what",
			'\t\tit trades for carrying anything at all (9.4)."""',
			"\t\tself._check()",
			f"\t\tstart = self._at + ({start})",
			f"\t\treturn self._msg.buffer[start:start + ({length})]",
		]

	def _tag(self, struct: ResolvedStruct, placement: Placement) -> list[str]:
		"""A tag's bytes, the span it covers, and its dirty bit (14.2).

		This backend marked the bit on a covered write and then said "not
		emitted by this backend yet" about the tag itself, so a caller could be
		told a write left the tag stale and had no way to reach the tag, ask
		whether it was stale, or say it no longer was.
		"""
		name  = py_name(local_name(struct, placement))
		count = placement.array_count or 0
		start = self._offset_expression(struct, placement)
		if start is None:
			return ["", f"\t# {placement.path}: this backend cannot resolve"
			        " where the tag sits."]

		lines = [
			"",
			"\t@property",
			f"\tdef {name}(self) -> memoryview:",
			f'\t\t"""{placement.path}: {count} bytes.',
			"",
			"\t\tThe algorithm is the caller's to run -- situ says which bytes",
			"\t\tit covers and when the result has gone stale, not how to",
			'\t\tcompute it."""',
			"\t\tself._check()",
			*([] if self._fits(struct, placement, count) is None else [
				f"\t\tif not ({self._fits(struct, placement, count)}):",
				"\t\t\t# Its offset is a sum of lengths the message chose,"
				" and the",
				"\t\t\t# frame does not reach it. `validate` reports such a"
				" message.",
				"\t\t\treturn self._msg.buffer[0:0]",
			]),
			f"\t\tstart = self._at + ({start})",
			f"\t\treturn self._msg.buffer[start:start + {count}]",
		]

		run = covered_run(struct, placement)
		if run is not None:
			first, last = run
			lines.extend([
				"",
				f"\tdef {name}_covered(self) -> tuple[int, int]:",
				f'\t\t"""The bytes {placement.name} covers:'
				f' {", ".join(placement.tag_covers)}.',
				"",
				"\t\tWrite the result over the slice above and then call",
				f"\t\t{name}_finalize. A gap in the coverage raises rather than",
				"\t\tbeing papered over with a range covering bytes the tag",
				'\t\tdoes not."""',
				"\t\tself._check()",
				f"\t\tstart = {self._offset_expression(struct, first) or '0'}",
				f"\t\tend   = {self._region_end(struct, last)}",
				"",
				"\t\tif end < start or end > self._len:",
				f'\t\t\traise BoundsError(',
				f'\t\t\t\tf"{placement.name} covers {{start}}..{{end}},'
				f' which is not inside this frame")',
				"\t\treturn start, end - start",
			])

		filler = _self_as(placement.attrs)
		if filler is not None:
			# A checksum defined over its own field runs the algorithm with
			# those bytes taken as a constant. They are still there, so what
			# the compiler hands out is where they are and what they read as
			# (14.2); substituting them is the caller's loop.
			lines.extend([
				"",
				f"	SELF_AS_{py_name(placement.name).upper()} = {filler:#04x}",
				"",
				f"	def {name}_self_span(self) -> tuple[int, int]:",
				f'		"""Where {placement.name}\'s own bytes sit inside what it',
				"		covers. Sum the covered span, substituting",
				f"		SELF_AS_{py_name(placement.name).upper()} for these bytes.",
				'		RFC 1071 is the case this exists for."""',
				"		self._check()",
				f"		at = {self._offset_expression(struct, placement) or '0'}",
				f"		n  = {placement.size_bits // BITS_PER_BYTE}",
				"",
				"		if at + n > self._len:",
				f'			raise BoundsError("{placement.name}: outside this frame")',
				"		return at, n",
			])

		held = obligation(self.schema, struct, placement.name)
		if held is not None:
			bit = f"self.DIRTY_{py_name(placement.name).upper()}"
			lines.extend([
				"",
				f"\tdef {name}_is_dirty(self) -> bool:",
				f'\t\t"""Whether {placement.name} no longer matches the bytes'
				f' it covers."""',
				f"\t\treturn bool(self._msg.dirty & {bit})",
				"",
				f"\tdef {name}_finalize(self) -> None:",
				f'\t\t"""Say it does again, once it has been recomputed."""',
				f"\t\tself._msg.clear_dirty({bit})",
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
				return found
		return "self._len"

	def _offsets(self, struct: ResolvedStruct) -> list[str]:
		"""Every dynamic offset in this struct, resolved in one pass.

		A scan makes reaching member N a rescan of the N-1 before it, and the
		per-member offset does that on every call. This is that sum once.

		A dict rather than a record type, which is where this backend departs
		from the other three: the caller has one already and a class per struct
		would be three lines of ceremony for a mapping Python spells inline.
		The keys are the member names, so a reader of one language's generated
		code recognises the other's.
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
			        f"\t# No offset cache for {struct.name}: a member has no",
			        "\t# length this can compute, so the offsets after it",
			        "\t# cannot be resolved in one pass any more than one at",
			        "\t# a time."]

		steps: list[str] = []
		for step in plan:
			if step.kind == "record":
				assert step.placement is not None
				name = py_name(local_name(struct, step.placement))
				steps.append(f'\t\tfound["{name}"] = at')
			elif step.kind == "align":
				steps.append(f"\t\tat = align_up(at, {step.size}, self._len)")
			elif step.placement is None:
				steps.append(f"\t\tat += {step.size}")
			else:
				length = self._length_expression(struct, step.placement,
				                                 running="at")
				steps.append(f"\t\tat += {length}")

		listed = ", ".join(f"`{py_name(local_name(struct, held))}`"
		                   for held in dynamic)

		return [
			"",
			"\tdef resolve_offsets(self) -> dict[str, int]:",
			f'\t\t"""Where each dynamically-placed member of {struct.name}'
			f' starts: {listed}.',
			"",
			"\t\tThe per-member offset resolves one by summing what precedes",
			"\t\tit, so it rescans every delimited member ahead of the one",
			"\t\tasked for, on every call. This is that sum once, for all of",
			'\t\tthem."""',
			"\t\tself._check()",
			"\t\tfound: dict[str, int] = {}",
			"\t\tat = 0",
			"",
			*steps,
			"",
			"\t\treturn found",
		]

	def _fixed_text_number(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""Digits in a field of declared width, padded (section 8.6.2).

		The bracket is a width in bytes and not a count, which is what the
		array branch read it as.
		"""
		scalar = placement.scalar
		if scalar is None:
			return []

		name  = py_name(local_name(struct, placement))
		width = placement.array_count or 0
		limit = placement.radix_max or 0
		start = self._offset_expression(struct, placement)
		if start is None:
			return ["", f"\t# {placement.path}: this backend cannot resolve"
			        " where the digits start."]

		return [
			"",
			"\t@property",
			f"\tdef {name}_digits(self) -> memoryview:",
			f'\t\t"""{placement.path}: the {width} bytes as written."""',
			"\t\tself._check()",
			f"\t\tstart = self._at + ({start})",
			f"\t\treturn self._msg.buffer[start:start + {width}]",
			"",
			"\t@property",
			f"\tdef {name}(self) -> int:",
			f'\t\t"""{placement.path}: {width} digits, padded, holding'
			f' 0..{limit}.',
			"",
			f"\t\tThe range is the field's rather than {scalar.name}'s:"
			f" {width} bytes",
			f"\t\tcannot hold what {scalar.name} can, and a check against the",
			"\t\ttype would accept a value the field cannot represent.",
			"",
			'\t\tRaises ConstraintError where the bytes are not digits."""',
			f"\t\tvalue = parse_uint(self.{name}_digits,"
			f" {placement.radix}, {limit})",
			"\t\tif value is None:",
			f'\t\t\traise ConstraintError(',
			f'\t\t\t\tf"{placement.path} is not {width} digits in base'
			f' {placement.radix}")',
			"\t\treturn value",
		]

	def _varint_field(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""Decode a varint field, and say how wide it turned out to be.

		It classified as `NOTHING` and this backend emitted nothing at all --
		not an accessor and not a note. A property that raises is the shape
		here: a caller who ignores a return code is the thing this backend
		refuses to make easy, and a truncated varint is exactly that case.
		"""
		declared = next((decl for decl in self.schema.varints()
		                 if decl.name == placement.varint), None)
		if declared is None:
			return []

		name  = py_name(local_name(struct, placement))
		start = self._offset_expression(struct, placement)
		if start is None:
			return ["", f"\t# {placement.path}: this backend cannot resolve"
			        " where it starts."]

		width   = declared.max_bytes
		signed  = declared.transform is ast.VarintTransform.ZIGZAG
		decoded = "zigzag_decode(raw)" if signed else "raw"
		big     = declared.encoding is ast.VarintEncoding.BE128

		read = (f"varint_be_get(data, at, {width}, {declared.terminal_bits})"
		        if big else f"varint_get(data, at, {width})")
		encoded = (f"varint_be_len(raw, {width}, {declared.terminal_bits})"
		           if big else "varint_len(raw)")

		minimal = ([
			"",
			"\t\t# `minimal` is declared, so a padded encoding is a second",
			"\t\t# encoding of one value and this schema does not admit it.",
			"\t\tif used != varint_len(raw):",
			"\t\t\traise ConstraintError(",
			f'\t\t\t\tf"{placement.path} is encoded in {{used}} bytes and needs"',
			f'\t\t\t\tf" {{{encoded}}}; `minimal` admits one encoding")',
		] if declared.minimal else [])

		return [
			"",
			"\t@property",
			f"\tdef {name}(self) -> int:",
			f'\t\t"""{placement.path}: a `{placement.varint}`, 1 to {width}'
			f' bytes, and',
			"\t\thow many is in the bytes themselves.",
			"",
			"\t\tRaises BoundsError where the frame ends mid-value"
			+ (", and\n\t\tConstraintError where a padded encoding is refused."
			   if declared.minimal else ".")
			+ '"""',
			"\t\tself._check()",
			f"\t\tat   = {start}",
			"\t\tdata = self._span",
			"",
			"\t\tif at >= len(data):",
			f'\t\t\traise BoundsError(f"{placement.path} starts at {{at}},'
			f' past the frame")',
			"",
			f"\t\tread = {read}",
			"\t\tif read is None:",
			f'\t\t\traise BoundsError(f"{placement.path} runs past the frame")',
			f"\t\traw, {'used' if declared.minimal else '_'} = read",
			*minimal,
			"",
			f"\t\treturn {decoded}",
			"",
			"\t@property",
			f"\tdef {name}_len(self) -> int:",
			f'\t\t"""How many bytes {placement.path} occupies. Zero where it',
			"\t\tcannot be read at all, which keeps every offset derived from",
			'\t\tit inside the frame."""',
			"\t\tself._check()",
			f"\t\tat   = {start}",
			"\t\tdata = self._span",
			"",
			"\t\tif at >= len(data):",
			"\t\t\treturn 0",
			f"\t\tread = {read}",
			"\t\treturn 0 if read is None else read[1]",
			"",
			"\t@property",
			f"\tdef {name}_value(self) -> int:",
			f'\t\t"""The same value where an exception cannot be raised: the',
			"\t\tlength arithmetic downstream is not fallible, and making it",
			"\t\tso would put a try around every accessor after this one.",
			"",
			"\t\tPublic, like the same accessor in the other three. It was",
			"\t\t`_"
			f"{name}_value` here, so the number every length in this",
			"\t\tstruct is derived from was the one thing a Python caller",
			'\t\tcould not ask for without touching a private name."""',
			"\t\ttry:",
			f"\t\t\treturn self.{name}",
			"\t\texcept (BoundsError, ConstraintError):",
			"\t\t\treturn 0",
		]

	def _undecoded_note(self, placement: object) -> list[str]:
		"""Name the entry point a caller must reach for, having declined it.

		The lines above say the region is not decoded here and, until this,
		did not say what to call instead -- a refusal naming no remedy. The
		symbol is decided in `traverse.codec_entry_point` so that the four
		backends cannot drift back to the three different answers they gave
		before it existed.
		"""
		codec = getattr(placement, "codec", None)
		if codec is None:
			return []
		symbol = codec_entry_point(self.schema, codec)
		if symbol is None:
			return [f"\t# `{codec}` has no `impl`, so there is nothing to"
			        f" call yet."]

		unit = ("bits" if decode_counts_bits(self.codecs.get(codec))
		        else "bytes")
		return [f"\t# Decode the bytes above by calling `{symbol}` from"
		        f" the C runtime;",
		        f"\t# its count is in {unit} (decision 0017)."]

	def _coded_region(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""The encoded bytes of a coded region, and why the decode is not here.

		The bytes need no transform to reach and this backend hands them
		back. The *decode* is a call into the C implementation, which
		decision 0017 makes the only one there is -- and where C++ links it
		for free and Rust declares it `extern "C"`, this one would have to
		load a shared object at run time, from a path situ has no convention
		for. Inventing one here would be a policy decision made in a code
		generator, so the accessor says what to do instead.
		"""
		name  = py_name(local_name(struct, placement))
		start = self._offset_expression(struct, placement)
		if start is None or placement.size_max_bits is None \
				or placement.size_bits % BITS_PER_BYTE:
			return ["", f"\t# No accessor for {placement.path}: its encoded",
			        f"\t# extent is {placement.codec}'s to report.",
			        *self._undecoded_note(placement)]

		# The interior's extent through the codec's expansion
		# (`traverse.region_extent`), not the region's minimum: a region whose
		# interior the data sizes reported zero bytes, in all four backends
		# and with no refusal (26.35). A fixed interior is unaffected -- its
		# minimum is its extent.
		size = self._region_length(struct, placement)
		if size is None:
			return ["", f"\t# No accessor for {placement.path}: its encoded",
			        f"\t# extent is {placement.codec}'s to report.",
			        *self._undecoded_note(placement)]

		lines = [
			"", "\t@property",
			f"\tdef {name}(self) -> memoryview:",
			f'\t\t"""{placement.path}: `{placement.codec}` output.',
			"",
			"\t\tThe bytes on the wire rather than the value. What they mean",
			'\t\tis behind the transform (13.5)."""',
			"\t\tself._check()",
			f"\t\tstart = self._at + ({start})",
			# Clamped to the view: the length is the interior's, and the
			# interior is sized by fields the message chose.
			f"\t\tstop  = start + min({size},"
			f" max(0, self._len - ({start})))",
			"\t\treturn self._msg.buffer[start:stop]",
		]

		return lines + self._decode_note(struct, placement)

	def _coded_delimited(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""A region found by scanning and then decoded (section 13.6).

		Scan first, decode second: a stuffing code protects its own terminator,
		so the sequence is unambiguous in the encoded bytes and would not be in
		the decoded ones. This backend emitted the bytes and said nothing about
		the transform, so a reader had no way to know they were not the value.
		"""
		return [
			"",
			f"\t# {placement.path} is `{placement.codec}` output, and the bytes",
			"\t# above are the encoded form. The scan runs on those, which is",
			"\t# the order the format specifies -- a stuffing code protects its",
			"\t# own terminator, so the sequence is unambiguous there and would",
			"\t# not be after decoding.",
			*self._decode_note(struct, placement),
		]

	def _decode_note(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""Where the decoded bytes are, which for this backend is elsewhere.

		Not emitted, deliberately and unchanged: the codec is C's (0017), and
		calling it from here means loading a shared object from a path this
		generator would have to invent. What the note can do is name the symbol
		and the size, and it now does so for a delimited region too -- which
		got no note at all, so a Python reader had nothing saying the bytes
		were stuffed.
		"""
		codec = self.codecs.get(placement.codec or "")
		if codec is None:
			return []

		name   = py_name(local_name(struct, placement))
		bound  = decode_bound(codec, placement)
		sized  = (f"{bound} bytes is what it needs" if bound is not None
		          else f"it needs the encoded length scaled by the codec's"
		               f" ratio, which `{name}_len` gives")

		# A tier-1 codec is the user's own, bound to a symbol and to 13.2a's
		# ABI. The note names that rather than a `situ_` function nobody
		# wrote: the other three backends call it now, and a Python reader
		# should be told what they call (26.35).
		symbol = extern_symbol(self.schema, placement.codec or "")
		if symbol is not None:
			# A `covers` clause changes which bytes go through the codec, so
			# the note has to say so: a Python reader comparing this against
			# the C the other three backends emit would otherwise see a call
			# over a span this comment did not describe (14.1a).
			preamble = [
				"",
				f"\t# No `{name}_decode`: the codec is the caller's, and",
				"\t# calling it from here means loading a shared object from",
				"\t# a path this generator would have to invent (0017).",
			]

			if not placement.coded_covers:
				return [*preamble,
					f"\t# `{symbol}_decode(in, in_len, out, out_cap,"
					" &out_len)`",
					f"\t# is the ABI this schema binds (13.2a); {sized}.",
				]

			# A `covers` clause binds the *scattered* pair instead (13.2b),
			# so naming 13.2a's signature here would send a Python reader to
			# write the wrong two functions.
			runs   = coded_spans(struct, placement)
			covers = ", ".join(f"`{one}`" for one in placement.coded_covers)
			if runs is None:
				return [*preamble,
					f"\t# The schema asks it to cover {covers} as well, and",
					"\t# one of those has no offset this layout can fix, so",
					"\t# no span list can be built for it.",
				]

			# 14.1b: where a tag covers what this transforms, the schema
			# had to say which order, and the note repeats it -- a Python
			# reader sequencing calls by hand needs it more than the three
			# backends whose signatures carry it.
			order = next((attr.value.name for attr in placement.attrs
			              if attr.name == "tag_order"
			              and isinstance(attr.value, ast.NameRef)), None)
			tags = sorted({tag for held in struct.layout.placements
			               if held is placement
			               or held.name in placement.coded_covers
			               for tag in held.covered_by})
			sequence = ([] if not tags else
			            ["\t# `tag_order = before`: the tag covers this"
			             " transform's",
			             f"\t# output, so applying it leaves"
			             f" {', '.join(tags)} stale.",
			             "\t# Transform first, then recompute."]
			            if order == "before" else
			            ["\t# `tag_order = after`: the tag covers this"
			             " transform's",
			             f"\t# input. Compute {', '.join(tags)} first, then"
			             " transform."])
			return [*preamble,
				f"\t# `{symbol}_encode_spans(spans, count)` and"
				f" `{symbol}_decode_spans`",
				"\t# are the ABI this schema binds (13.2b): in place, over a",
				f"\t# list of spans, because the transform also runs over"
				f" {covers}.",
				f"\t# {len(runs)} span(s) here, which the other three backends"
				" build",
				"\t# and pass in one call.",
				*sequence,
			]

		if not decodes_here(codec):
			return []

		return [
			"",
			f"\t# No `{name}_decode`: the codec is C's (decision 0017), and",
			"\t# calling it from here means loading a shared object from a",
			"\t# path this generator would have to invent. Build the C",
			f"\t# runtime and call `situ_{py_name(placement.codec or '')}_decode`",
			f"\t# through ctypes; {sized}.",
		]

	def _arm_accessors(self, struct: ResolvedStruct) -> list[str]:
		"""Each variant arm's members, guarded by the discriminant (9.6).

		Walked explicitly because an arm's member is nested by path and the
		own-member walk rightly leaves it out -- an arm is not a type, so
		there is no other class it could be emitted on.
		"""
		lines: list[str] = []
		for variant in own_members(struct):
			if variant.kind != "variant":
				continue
			for arm, member in arm_members(struct, variant):
				if member is not None:
					lines.extend(self._arm_member(struct, variant, arm, member))
		return lines

	def _arm_hint(self, placement: Placement, scalar: ScalarType | None) -> str:
		"""What an arm accessor hands back, as an annotation."""
		if scalar is not None and placement.array_count is None \
				and placement.sized_by is None:
			return "int"
		if scalar is not None:
			return "memoryview"
		return py_name(placement.type_name or "object")

	def _arm_member(self, struct: ResolvedStruct, variant: Placement,
			arm: Arm, placement: Placement) -> list[str]:
		"""One arm member, raising where the arm is not the one present.

		Raises rather than returning a code, which is this backend's
		convention: `VersionError` is what an unrecognised discriminant gets,
		and reading the arm that is not there is the same mistake from the
		other end.
		"""
		held = self._over_fields(struct, variant.discriminant or "", "self")
		if arm.value is None:
			matched = matched_values(variant)
			if not matched:
				return []
			test = " or ".join(f"{held} == {one.value}" for one in matched)
		else:
			test = f"{held} != {arm.value}"

		name   = py_name(local_name(struct, placement))
		scalar = placement.scalar
		start  = self._offset_expression(struct, placement)
		if start is None:
			return []

		head = [
			"", "\t@property",
			f"\tdef {name}(self) -> {self._arm_hint(placement, scalar)}:",
			f'\t\t"""{placement.path}, present when the discriminant selects',
			f'\t\t`{arm.source or arm.value}`. Raises VersionError otherwise."""',
			f"\t\tif {test}:",
			f'\t\t\traise VersionError("{placement.path}: that arm is not'
			' the one present")',
		]

		# `data_sized` in the guard, not just the two spellings that name a
		# count: `i32 run[n + 1]` sets neither `array_count` nor `sized_by`,
		# so an arm that is a run of values looked exactly like a scalar arm
		# and got a getter for the first element. The same sentence 26.47
		# wrote about the ordinary member dispatch, in the parallel one an
		# arm has.
		if scalar is not None and placement.array_count is None \
				and placement.sized_by is None \
				and not data_sized(placement):
			return [*head, f"\t\treturn {self._raw_load(placement, scalar)}"]

		if scalar is not None and indexed_elements(placement):
			# A run of values wider than a byte, which the slice below is not
			# available to: the element is ValueConverted, so the bytes handed
			# back whole would not be the values. The count and the indexed
			# getter an ordinary run gets, with the arm test on the count --
			# which the getter reaches through, so both refuse.
			width  = scalar.bits // BITS_PER_BYTE
			length = (self._length_expression(struct, placement)
			          if placement.array_count is None
			          else str(placement.array_count * width))
			if length is not None:
				load = self._load(placement, scalar,
				                  offset=f"({start}) + index * {width}")
				return [
					"", "\t@property",
					f"\tdef {name}_count(self) -> int:",
					f'\t\t"""How many {scalar.name} elements of'
					f' {placement.path} are here,',
					f'\t\twhen the discriminant selects'
					f' `{arm.source or arm.value}`."""',
					f"\t\tif {test}:",
					f'\t\t\traise VersionError("{placement.path}: that arm is'
					' not the one present")',
					f"\t\treturn min(({length}),",
					f"\t\t\tmax(0, self._len - ({start}))) // {width}",
					"", f"\tdef {name}(self, index: int) -> int:",
					f'\t\t"""Element `index`, an {scalar.name}.',
					"",
					"\t\tNo slice accessor: the element is ValueConverted, so"
					" bytes",
					'\t\thanded back whole would not be the values."""',
					f"\t\tif not 0 <= index < self.{name}_count:",
					f'\t\t\traise IndexError(f"{placement.path}[{{index}}]")',
					f"\t\treturn {load}",
				]

		if scalar is not None and scalar.bits == BITS_PER_BYTE:
			# A constant count is a length too, and `_length_expression`
			# answers only for the ones the data decides -- so `u8
			# gateway[4]`, an ICMP redirect's whole payload, got no accessor
			# and no note in three backends out of four.
			length = (self._length_expression(struct, placement)
			          if placement.array_count is None
			          else str(placement.array_count))
			if length is None:
				return [*head, f"\t\t# ...and its length is not one this"
				        " backend can compute."]
			return [
				*head,
				"\t\tself._check()",
				f"\t\tstart = self._at + ({start})",
				# Clamped to the *view*, not to the buffer. A slice already
				# stops at the end of the message, which is why this backend
				# was the one that did not crash or overrun -- but a view is
				# a window on a larger buffer, and bytes after its limit are
				# not this member's however many the arm declares.
				f"\t\tstop  = start + min(({length}),"
				f" max(0, self._len - ({start})))",
				"\t\treturn self._msg.buffer[start:stop]",
			]

		# A struct-typed arm -- `case msg_type.hello: Hello hello;`, section
		# 9.6's own example. Its members belong to its type, so handing back
		# one of those is the whole of the work.
		nested = self.resolved.structs.get(placement.type_name or "")

		# A *variable-size* struct arm, measured from its own bytes. C has
		# emitted one since variants landed and the other three required the
		# arm's type to have a single size -- while `validate` called the
		# accessor regardless, so a schema with such an arm produced a module
		# that raised `AttributeError` on the first check. MQTT is four of
		# them: CONNECT, PUBLISH, SUBSCRIBE and UNSUBSCRIBE all end in
		# something the data sizes (26.55).
		if nested is not None and not nested.layout.is_fixed_size \
				and has_computable_extent(self.resolved.structs, nested):
			inner = py_name(nested.name)
			base  = py_name(local_name(struct, placement))
			return [
				# How many bytes this arm occupies, for the switch that places
				# whatever follows the variant. The length chain names it and
				# only the ordinary nested member emitted one, so the first
				# schema with a variable-size arm named an attribute nothing
				# defines -- MQTT's CONNECT, three times over.
				"", "\t@property",
				f"\tdef {base}_extent(self) -> int:",
				f'\t\t"""How many bytes {placement.path} occupies here."""',
				f"\t\tstart = {start}",
				f"\t\treturn {inner}(self._msg, self._at + start,",
				"\t\t\tmax(0, self._len - start))._extent",
				*head,
				f"\t\twhole = {inner}(self._msg, self._at + ({start}),",
				f"\t\t\tmax(0, self._len - ({start})))",
				"\t\tsize  = whole._extent",
				f"\t\tif self._len - ({start}) < size:",
				f'\t\t\traise BoundsError("{placement.path}: the frame does'
				' not reach it")',
				f"\t\treturn {inner}(self._msg, self._at + ({start}), size)",
			]

		if nested is not None and nested.layout.is_fixed_size:
			inner = py_name(nested.name)
			return [
				*head,
				# The same bounds question a nested member asks (26.31): an arm
				# sits at an offset the discriminant chose, and a view claiming
				# the struct's size whatever the frame holds is 20.2's
				# acquisition check skipped one level in.
				f"\t\tif self._len - ({start}) < {inner}.SIZE_BYTES:",
				f'\t\t\traise BoundsError("{placement.path}: the frame does'
				' not reach it")',
				f"\t\treturn {inner}(self._msg, self._at + ({start}),",
				f"\t\t\t{inner}.SIZE_BYTES)",
			]

		# Said rather than skipped, which is C's rule next door: "a header
		# that simply lacked the accessor would leave a reader looking for a
		# typo". This returned `[]`, so an arm whose type has no measurable
		# extent -- `packet.body.publish` in `example/mqtt`, and four more --
		# appeared in the generated Python as neither an accessor nor a note.
		# C and C++ both wrote one; this backend and Rust wrote nothing, and
		# the test that compares what the four refuse could not see the
		# difference because an empty refusal set reads as "emitted it"
		# (26.190).
		return [
			f"\t# No accessor for `{placement.path}`: one "
			f"`{placement.type_name}` has no extent this backend can "
			f"compute, so the arm cannot be reached into yet.",
		]

	def _nested_text_values(self, struct: ResolvedStruct) -> list[str]:
		"""The non-failing read of a *nested* text number.

		A member of a nested struct can drive a length -- `u8
		name[header.namesize]` in a cpio entry -- and an expression over it
		names a helper on *this* struct. The nested struct has its own, at its
		own offsets; this one reads the digits where they sit here. Nothing
		emitted it, so the generated code called a function that does not
		exist.
		"""
		lines: list[str] = []
		for entry in struct.entries:
			placement = entry.placement
			if placement.radix is None or placement.offset_bits is None:
				continue
			# Nested *or* the struct's own. Restricting this to nested
			# members assumed the fixed-width form beside it emitted its own
			# `_value`, and it does not: `decimal u32 n[4]; u16 d[n]` named a
			# property nothing defined. Every text driver in `example/` is
			# either delimited or nested, which are the two forms that had it.
			scalar = placement.scalar
			if scalar is None or placement.array_count is None:
				continue

			name  = py_name(local_name(struct, placement))
			limit = (1 << scalar.bits) - 1
			at    = placement.offset_bits // BITS_PER_BYTE
			lines.extend([
				"",
				"\t@property",
				f"\tdef {name}_value(self) -> int:",
				f'\t\t"""{placement.path}, where an error cannot be raised:',
				"\t\tthe offset arithmetic after it is not fallible, and",
				"\t\t`validate` refuses a frame whose digits are not"
				' digits."""',
				"\t\tself._check()",
				f"\t\traw = self._msg.buffer[self._at + {at}:"
				f"self._at + {at + placement.array_count}]",
				f"\t\tvalue = parse_uint(raw, {placement.radix}, {limit})",
				"\t\treturn 0 if value is None else value",
			])
		return lines

	def _explained(self, struct: ResolvedStruct, entry: Resolved) -> list[str]:
		"""One member, and where it has no setter, why.

		Section 1: the absence of an operation is "deliberate, explained, and
		assertable". This backend explained a weakened `mutate` for a scalar
		and said nothing for a delimited or variable member, which is the
		commonest case there is (26.35).
		"""
		lines  = self._member(struct, entry)
		mutate = entry.vector.get(Axis.MUTATE)

		if not lines or mutate.base in ("InPlaceFixed", "InPlaceSlack"):
			return lines
		if any("mutate is" in line or "setter" in line for line in lines):
			return lines
		# A ValueConverted run has no memoryview but is read through its
		# indexed getter, so it is read-accessible-but-unwritable the same way
		# a byte run is -- and said so about nobody until a `u16` utf16 run was
		# the only unwritable member in a schema (0044).
		wide_run = any("would not be the values" in line for line in lines)
		if not (any("memoryview" in line for line in lines) or wide_run):
			return lines

		name = py_name(local_name(struct, entry.placement))
		if wide_run and not any("memoryview" in line for line in lines):
			return lines + [
				"",
				f"\t# No {name} setter: mutate is {mutate.render()}. The indexed",
				"\t# getter above reads it; making room for more elements is a",
				"\t# rewrite of the frame rather than a store.",
			]
		return lines + [
			"",
			f"\t# No {name} setter: mutate is {mutate.render()}. The bytes",
			"\t# above are where they are; making room for more of them is a",
			"\t# rewrite of the frame rather than a store.",
		]

	def _member(self, struct: ResolvedStruct, entry: Resolved) -> list[str]:
		bounds = self._value_bounds(struct, entry.placement) \
			if entry.placement.kind == "field" else []
		return bounds + self._member_body(struct, entry)

	def _value_bounds(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""`[min]`/`[max]` as class constants a caller can share (26.125).
		The decision is `declared_value_bounds`; this is Python's spelling."""
		low, high = declared_value_bounds(placement, self.resolved.layout.env)
		if low is None and high is None:
			return []
		name  = py_name(local_name(struct, placement)).replace(".", "_").upper()
		lines = [""]
		if low is not None:
			lines.append(f"\t{name}_VALUE_MIN = {low}")
		if high is not None:
			lines.append(f"\t{name}_VALUE_MAX = {high}")
		return lines

	def _member_body(self, struct: ResolvedStruct, entry: Resolved) -> list[str]:
		placement = entry.placement

		kind = classify(struct, placement, self.structs)

		if kind is Member.OPAQUE:
			return self._opaque(struct, placement)
		if kind is Member.TAG:
			return self._tag(struct, placement)
		if kind is Member.MARKER:
			return self._marker(struct, placement)
		if kind is Member.RESERVED:
			return ["", f"\t# {placement.path} is reserved: no accessor, and",
			        "\t# validate() holds it to the pattern the schema declares."]
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
		if kind is Member.NESTED:
			return self._nested(struct, placement)
		if kind is Member.TEXT_NUMBER:
			return self._fixed_text_number(struct, placement)
		if kind is Member.ARRAY:
			return self._array(struct, placement)
		if kind is Member.VARINT:
			return self._varint_field(struct, placement)
		if kind is Member.SCALAR:
			return self._scalar(struct, entry)
		if kind is Member.NOTHING:
			return []

		# A sealed region has no accessor of its own: its interior is behind
		# the gate, which is emitted below. The fallthrough note read as a
		# missing feature while sitting directly above the thing that supports
		# it -- the same contradiction the coded-region note had.
		if placement.kind == "sealed":
			return ["",
			        f"\t# {placement.path} is sealed by"
			        f" {placement.codec}: it has no accessor",
			        f"\t# of its own, and its interior is reached through the"
			        " gate below,",
			        f"\t# which opens only once the tag has verified (14.3)."]

		if placement.kind == "variant":
			return ["",
			        f"\t# {placement.path} is a variant: exactly one arm is",
			        "\t# present, and each is above behind the discriminant",
			        "\t# that selects it. The variant itself has no accessor."]
		return ["", f"\t# {placement.path}: not emitted by this backend yet."]

	def _marker(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""The byte-order marker's constants and accessors (section 8.3).

		Compared as a byte sequence rather than decoded as a number: it has to
		be readable before its own byte order is known.

		This backend said "not emitted by this backend yet" for the marker and
		then read every field it governs big-endian regardless, so a
		little-endian TIFF -- which is most of them -- came back byte-swapped
		with no exception raised. The map said
		`ConditionallyConverted(byte_order)` on those fields the whole time.
		"""
		marker = self.markers.get(placement.name)
		scalar = placement.scalar
		if marker is None or scalar is None:
			return []

		env    = self.resolved.layout.env
		little = evaluate(marker.little, env)
		big    = evaluate(marker.big, env)
		width  = scalar.bits
		name   = py_name(local_name(struct, placement))
		digits = width // 4
		size   = width // BITS_PER_BYTE

		return [
			"",
			f"\t{name.upper()}_LITTLE = 0x{little:0{digits}X}",
			f"\t{name.upper()}_BIG    = 0x{big:0{digits}X}",
			f"\t{name.upper()}_HOST   = ("
			f"{name.upper()}_BIG if sys.byteorder == \"big\"",
			f"\t\telse {name.upper()}_LITTLE)",
			"",
			"\t@property",
			f"\tdef {name}_is_little(self) -> bool:",
			f'\t\t"""{placement.path}: which byte order the rest of this frame',
			"\t\tuses, read from the data (8.3). Compared as a byte sequence",
			"\t\trather than decoded as a number -- it has to be readable",
			'\t\tbefore its own order is known."""',
			f"\t\treturn self._read({placement.offset_bytes}, {size},"
			f" signed=False, big=True) \\",
			f"\t\t\t== self.{name.upper()}_LITTLE",
		]

	def _marker_predicate(self, placement: Placement) -> str:
		return f"self.{py_name(placement.marker or '')}_is_little"

	def _scalar(self, struct: ResolvedStruct, entry: Resolved) -> list[str]:
		placement = entry.placement
		scalar    = placement.scalar
		assert scalar is not None

		name   = py_name(local_name(struct, placement))
		hint   = self._hint(placement)
		offset = (None if placement.offset_bits is not None
		          else self._offset_expression(struct, placement))

		# The layout solver refuses a bit-packed field at a dynamic offset, so
		# this asserts that rule rather than declaring a gap this backend has.
		assert not (placement.offset_bits is None and scalar.is_bit_packed), \
			f"{placement.path}: bit-packed at a dynamic offset"

		if placement.offset_bits is None and offset is None:
			return ["", f"\t# {placement.path}: its offset cannot be resolved."]

		# A scalar whose offset the message decides answers zero where it does
		# not fit. Python cannot read out of bounds -- the slice is short --
		# but a short slice read as an integer is a number nobody wrote, and
		# the other three answer zero (26.27).
		fits = self._fits(struct, placement,
		                  max(1, (scalar.bits + BITS_PER_BYTE - 1)
		                      // BITS_PER_BYTE))

		if placement.since is not None and placement.version_field is not None:
			lines = self._versioned(placement, entry, name, hint, scalar,
			                        offset, fits)
		elif fits is not None:
			lines = ["", "\t@property", f"\tdef {name}(self) -> {hint}:",
			         *self._field_doc(entry),
			         f"\t\tif not ({fits}):",
			         "\t\t\t# Its offset is a sum of lengths the message"
			         " chose, and the",
			         "\t\t\t# frame does not reach it. `validate` reports"
			         " such a message.",
			         "\t\t\treturn 0",
			         f"\t\treturn {self._load(placement, scalar, offset)}"]
		else:
			lines = ["", "\t@property", f"\tdef {name}(self) -> {hint}:",
			         *self._field_doc(entry),
			         f"\t\treturn {self._load(placement, scalar, offset)}"]

		if entry.vector.get(Axis.MUTATE).base != "InPlaceFixed":
			lines.extend([
				"",
				f"\t# No {name} setter: mutate is"
				f" {entry.vector.get(Axis.MUTATE).render()}.",
			])
			return lines

		# A covered write leaves a tag stale, so it is not an assignment and is
		# not spelled as one. The C backend makes the same refusal, and the two
		# have to agree: a schema that means one thing in C must not mean
		# another here.
		if placement.covered_by:
			tags = ", ".join(placement.covered_by)
			# ...and where the offset is the message's, the frame is not known
			# to hold it. The plain setter below has said so since 26.27 and
			# the covered one beside it did not, so the one write that carries
			# a security obligation was the one without the bound.
			guard = ([] if fits is None else [
				f"\t\tif not ({fits}):",
				"\t\t\t# Its offset is a sum of lengths the message chose, and",
				"\t\t\t# the frame does not reach it.",
				"\t\t\treturn",
			])
			lines.extend([
				"",
				f"\t# No {name} setter: writing it leaves {tags} stale.",
				f"\t# Use set_{name}(msg, value), which marks the bit.",
				"",
				f"\tdef set_{name}(self, msg: Message, value: {hint}) -> None:",
				f'\t\t"""Write {placement.path} and mark {tags} stale."""',
				*guard,
				f"\t\t{self._store(placement, scalar, offset)}",
				f"\t\tmsg.mark_dirty({self._tag_bit(struct, placement)})",
			])
			return lines

		gate = []
		if placement.since is not None and placement.version_field is not None:
			# Writing it to an earlier message puts these bytes past that
			# message's end. The getter refused this from the start and the
			# setter did not, in every backend, until one of them was checked.
			version = py_name(placement.version_field)
			gate = [
				f"\t\tif self.{version} < {placement.since}:",
				"\t\t\traise VersionError(",
				f'\t\t\t\tf"{placement.path} is not in a version '
				f'{{self.{version}}} message")',
				# The declared version is the message's claim, not a
				# fact about the buffer: `ver = 2` in three bytes passed
				# this and stored into a slice that stops before it.
				#
				# `fits` carries the same check where the offset is one the
				# message chose rather than a constant -- the case that had
				# no check at all on either the read or the write.
				*self._versioned_bounds(placement, fits),
			]

		# A write at an offset the message chose does nothing, which is what
		# the other backends do: the slice assignment would raise here rather
		# than corrupt anything, and four backends answering three ways about
		# one message is the disagreement they exist to avoid (26.27).
		lines.extend([
			"",
			f"\t@{name}.setter",
			f"\tdef {name}(self, value: {hint}) -> None:",
			*gate,
			*([f"\t\t{self._store(placement, scalar, offset)}"]
			  if fits is None else [
				f"\t\tif not ({fits}):",
				"\t\t\t# Its offset is a sum of lengths the message chose,"
				" and the",
				"\t\t\t# frame does not reach it. `validate` reports such a"
				" message.",
				"\t\t\treturn",
				f"\t\t{self._store(placement, scalar, offset)}",
			]),
		])
		return lines

	def _versioned(self, placement: Placement, entry: Resolved, name: str,
			hint: str, scalar: ScalarType, offset: str | None,
			fits: str | None) -> list[str]:
		"""A member present only from a given version (section 19.4).

		A property that raises, not one that returns a sentinel. There is no
		value to hand back when the field is not there, and returning the
		bytes that follow would give another member's -- which is the bug the
		construct exists to prevent, so it is not available to write.
		"""
		version = py_name(placement.version_field or "")
		self._emitted.add(placement.path)
		return [
			"",
			"\t@property",
			f"\tdef {name}(self) -> {hint}:",
			*self._field_doc(entry),
			f"\t\tif self.{version} < {placement.since}:",
			"\t\t\traise VersionError(",
			f'\t\t\t\tf"{placement.path} arrives in version '
			f'{placement.since}; this message is version "',
			f"\t\t\t\tf\"{{self.{version}}}\")",
			*self._versioned_bounds(placement, fits),
			*(["\t\tif not (" + fits + "):",
			   "\t\t\t# Its offset is a sum of lengths the message chose,",
			   "\t\t\t# and the frame does not reach it. Zero, because that",
			   "\t\t\t# is what every other scalar the message places",
			   "\t\t\t# answers (26.27) -- a versioned member is not a",
			   "\t\t\t# second convention, it is the same one with a gate",
			   "\t\t\t# in front (26.73).",
			   "\t\t\treturn 0"]
			  if placement.offset_bits is None and fits is not None else []),
			f"\t\treturn {self._load(placement, scalar, offset)}",
		]

	def _versioned_bounds(self, placement: Placement,
			fits: str | None) -> list[str]:
		"""And the frame has to hold it, which the acquiring check did not say.

		The one place 20.2's argument does not reach: a versioned struct's
		minimum is its *first* version's, so a message declaring version 2 in
		three bytes is a well-formed question about a member that is not
		there. All four backends asked the version and stopped -- C read past
		the view, Rust panicked, and this read a short slice as an integer,
		which is a number nobody wrote.

		That was fixed for a member at a constant offset and not for one the
		message places, because `offset_bytes` is None there and this returned
		nothing at all. So a versioned member behind a delimiter or a counted
		run had no check in this backend while the other three kept theirs,
		and the four disagreed about what a short frame holds -- which is the
		composed sweep's versioning axis reporting on its first sample.
		`fits` is the same expression the unversioned path guards with.
		"""
		scalar = placement.scalar
		if scalar is None:
			return []

		# Only where the offset is a constant. A member the *message* places
		# clamps instead, in the getter above and through the setter's own
		# guard below -- 26.73 settled that a versioned member at a dynamic
		# offset follows 26.27 like every other dynamically placed scalar,
		# rather than being a second convention that four backends then have
		# to agree about.
		if placement.offset_bits is None:
			return []

		width = max(1, (scalar.bits + BITS_PER_BYTE - 1) // BITS_PER_BYTE)
		return [
			f"\t\tif self._len - {placement.offset_bytes} < {width}:",
			f'\t\t\traise BoundsError("{placement.path}: the frame stops'
			' before it")',
		]

	def _tag_bit(self, struct: ResolvedStruct, placement: Placement) -> str:
		"""The bits every obligation over this field stands for, ORed.

		Every one of them, not the first. A field under a tag *and* an
		invariant leaves both stale when it is written, and marking one of the
		two is a message that reports itself ready to send while a covered byte
		no longer matches what authenticates it.

		The numbering is `traverse.obligations`, which is also where C gets it.
		This used to index a list of tags alone and fall back to bit 0 for
		anything it did not find -- and bit 0 is the first tag's, so an
		invariant marked the tag dirty and nothing else.
		"""
		# Named rather than numbered. The constants are emitted on the class
		# and the other three backends name theirs; this wrote the literal, so
		# a reader comparing `mark_dirty(1)` here against `DIRTY_MAC` there had
		# to work out that they were the same bit.
		named = [f"self.DIRTY_{py_name(one.name).upper()}"
		         for label in placement.covered_by
		         if (one := obligation(self.schema, struct, label)) is not None]
		return " | ".join(named) if named else "0x1"

	def _field_doc(self, entry: Resolved) -> list[str]:
		"""What the property syntax hides, said where a reader will find it."""
		vector = entry.vector
		axes   = " ".join(f"{axis.value}={vector.get(axis).render()}"
		                  for axis in (Axis.OFFSET, Axis.SIZE, Axis.REPR,
		                               Axis.MUTATE))
		return [f'\t\t"""{entry.placement.path}: {axes}."""']

	def _load(self, placement: Placement, scalar: ScalarType,
			offset: str | None = None) -> str:
		raw = self._raw_load(placement, scalar, offset)

		if scalar.is_bcd:
			raw = f"bcd_decode({raw}, {scalar.digits})"
		if placement.type_name in self.enums:
			return f"as_enum({py_name(placement.type_name)}, {raw})"
		return raw

	def _raw_load(self, placement: Placement, scalar: ScalarType,
			offset: str | None = None) -> str:
		big = _order(placement.endian)

		if scalar.is_bit_packed:
			if placement.offset_bits is None:
				return "0  # dynamic bit offset: not resolved by this backend"
			msb = placement.bit_order is not ast.BitOrder.LSB_FIRST
			return (f"self._bits({placement.offset_bits}, {scalar.bits},"
			        f" msb={msb}, signed={scalar.signed})")

		at = offset if offset is not None else str(placement.offset_bytes)

		# A field the data decides the order of. Without this it read every
		# one big-endian whatever the marker said.
		if placement.marker is not None:
			return (f"self._read({at}, {scalar.bits // BITS_PER_BYTE},"
			        f" signed={scalar.signed},"
			        f" big=not {self._marker_predicate(placement)})")

		return (f"self._read({at}, {scalar.bits // BITS_PER_BYTE},"
		        f" signed={scalar.signed}, big={big})")

	def _store(self, placement: Placement, scalar: ScalarType,
			offset: str | None = None) -> str:
		big   = _order(placement.endian)
		value = "int(value)"

		if scalar.is_bcd:
			value = f"bcd_encode({value}, {scalar.digits})"

		if scalar.is_bit_packed:
			msb = placement.bit_order is not ast.BitOrder.LSB_FIRST
			return (f"self._set_bits({placement.offset_bits}, {scalar.bits},"
			        f" {value}, msb={msb})")

		at = offset if offset is not None else str(placement.offset_bytes)

		# The write has to agree with the read, or a round trip through this
		# view swaps the value: the getter branched on the marker and the
		# setter did not.
		if placement.marker is not None:
			return (f"self._write({at}, {scalar.bits // BITS_PER_BYTE},"
			        f" {value}, signed={scalar.signed},"
			        f" big=not {self._marker_predicate(placement)})")

		return (f"self._write({at}, {scalar.bits // BITS_PER_BYTE}, {value},"
		        f" signed={scalar.signed}, big={big})")

	def _hint(self, placement: Placement) -> str:
		"""An enum field may hold a value that is not a member.

		Section 8.7's `default = error` says such a value is rejected on parse,
		not that a getter cannot return it -- and a `default = pass` schema
		admits it outright. So the hint is honest about both.
		"""
		if placement.type_name in self.enums:
			name = py_name(placement.type_name)
			if name in self._shadowed_enums():
				name = f"_situ_{name}"
			return f"{name} | int"
		return "int"

	def _located(self, struct: ResolvedStruct, placement: Placement) -> list[str]:
		"""A member the data positions, reached from the message (9.8).

		This backend needs no extra parameter, unlike C, C++ and Rust: a
		`View` already holds the `Message` it came from, so where offset zero
		is is something it can already answer. The asymmetry is worth naming
		rather than hiding -- the other three carry a frame and nothing else.
		"""
		name   = py_name(local_name(struct, placement))
		offset = self._over_fields(struct, placement.located or "", "self")
		length = self._length_expression(struct, placement)
		if length is None and placement.is_fixed_size \
				and placement.size_bits % BITS_PER_BYTE == 0:
			length = str(placement.size_bits // BITS_PER_BYTE)
		if length is None:
			return ["",
			        f"\t# No accessor for {placement.path}: it is placed by",
			        f"\t# `{placement.located}`, and how long it is is not",
			        "\t# something this backend can work out."]

		return [
			"", "\t@property",
			f"\tdef {name}(self) -> memoryview:",
			f'\t\t"""{placement.path}, at `{placement.located}` bytes from the',
			"",
			"\t\tstart of the *message* rather than of this view. The offset is",
			"\t\tthe message's, so nothing about this frame says it is inside",
			"\t\tthe buffer -- checked here, on every read, which is what",
			'\t\t`offset = DataPlaced` in the map costs."""',
			"\t\tself._check()",
			f"\t\tat = {offset}",
			f"\t\tn  = {length}",
			"\t\tif at + n > len(self._msg.buffer):",
			f'\t\t\traise BoundsError("{placement.path}: `{placement.located}`'
			' points outside the message")',
			"\t\treturn self._msg.buffer[at:at + n]",
		]

	def _nested(self, struct: ResolvedStruct, placement: Placement) -> list[str]:
		name   = py_name(local_name(struct, placement))
		nested = py_name(placement.type_name or "")
		inner  = self.resolved.structs.get(placement.type_name or "")
		start  = self._offset_expression(struct, placement)

		if inner is not None and not inner.layout.is_fixed_size and start is not None:
			# `SIZE_BYTES` is a class attribute only where a struct has one
			# size, so this raised `AttributeError` on the first access -- a
			# crash at the point of use rather than at generation, which is
			# the worst place for it to arrive. `_extent` is the same trap one
			# round further on: emitted only where the struct can be measured
			# from its own bytes, and read here whether it was or not.
			if not has_computable_extent(self.resolved.structs, inner):
				return [
					"",
					f"\t# No accessor for {placement.path}: one "
					f"`{placement.type_name}` has no",
					"\t# extent this backend can compute, so nothing can say"
					" where it ends.",
				]
			return [
				"", "\t@property",
				f"\tdef {name}_extent(self) -> int:",
				f'\t\t"""How many bytes {placement.path} occupies here.',
				"",
				"\t\tRead from the bytes: the member has no one size, and",
				'\t\tneither does the offset of whatever follows it."""',
				f"\t\tstart = self._at + ({start})",
				f"\t\treturn {nested}(self._msg, start,",
				f"\t\t\tself._len - ({start}))._extent",
				"",
				"\t@property",
				f"\tdef {name}(self) -> {nested}:",
				f'\t\t"""{placement.path}, sized from its own contents.',
				"",
				"\t\tRaises BoundsError where the frame does not contain it:",
				"\t\tevery accessor on the result trusts that its own bytes",
				"\t\tare all here, which is 20.2's acquisition check one",
				'\t\tlevel in (26.31)."""',
				f"\t\tif self._len - ({start}) < self.{name}_extent:",
				f'\t\t\traise BoundsError("{placement.path}: the frame does'
				' not reach it")',
				f"\t\treturn {nested}(self._msg, self._at + ({start}),",
				f"\t\t\tself.{name}_extent)",
			]

		# A fixed-size nested struct, at whatever offset the members before it
		# leave. This asked the placement for a constant one and crashed the
		# compiler on the assertion inside `offset_bytes` for the most
		# ordinary shape a protocol has -- a header, a variable-length field,
		# and another header. C reads its own offset function here; the other
		# three each had this line.
		if start is None:
			return ["", f"\t# No accessor for {placement.path}: this backend"
			        " cannot resolve where it starts."]
		where = f"({start})"

		return [
			"", "\t@property", f"\tdef {name}(self) -> {nested}:",
			f'\t\t"""{placement.path} at {placement.offset_bytes}.'
			if placement.offset_bits is not None else
			f'\t\t"""{placement.path}, at an offset the message decides.',
			"",
			"\t\tRaises BoundsError where the frame does not contain it",
			'\t\t(26.31)."""',
			f"\t\tif self._len - {where} < {nested}.SIZE_BYTES:",
			f'\t\t\traise BoundsError("{placement.path}: the frame does not'
			' reach it")',
			f"\t\treturn {nested}(self._msg, self._at + {where},",
			f"\t\t\t{nested}.SIZE_BYTES)",
		]

	def _scalar_array(self, struct: ResolvedStruct, placement: Placement,
			scalar: ScalarType) -> list[str]:
		"""`u16 samples[4]`: one getter taking an index.

		No slice: the element is `ValueConverted`, so bytes handed back whole
		would not be the values. Index them individually, which is C's rule and
		the reason it gives.
		"""
		name  = py_name(local_name(struct, placement))
		count = placement.array_count or 0
		width = scalar.bits // BITS_PER_BYTE
		start = self._offset_expression(struct, placement)
		if start is None:
			return ["", f"\t# {placement.path}: this backend cannot resolve"
			        " where the array starts."]

		load = self._load(placement, scalar,
		                  offset=f"({start}) + index * {width}")

		return [
			"",
			f"\tCOUNT_{name.upper()} = {count}",
			"",
			f"\tdef {name}(self, index: int) -> int:",
			f'\t\t"""Element `index` of {count}, an {scalar.name}.',
			"",
			"\t\tNo slice accessor: the element is ValueConverted, so bytes",
			'\t\thanded back whole would not be the values."""',
			f"\t\tif not 0 <= index < {count}:",
			f'\t\t\traise IndexError(f"{placement.path}[{{index}}]'
			f' of {count}")',
			f"\t\treturn {load}",
		]

	def _array(self, struct: ResolvedStruct, placement: Placement) -> list[str]:
		name   = py_name(local_name(struct, placement))
		scalar = placement.scalar
		count  = placement.array_count or 0

		# A wide scalar element: an indexed getter, which is what C emits.
		# No pointer, for the reason C gives -- the element is ValueConverted,
		# so a view into it would alias bytes that are not the value.
		if scalar is not None and scalar.bits != BITS_PER_BYTE \
				and not scalar.is_bit_packed \
				and scalar.bits % BITS_PER_BYTE == 0:
			return self._scalar_array(struct, placement, scalar)

		if scalar is None or scalar.bits != BITS_PER_BYTE:
			nested = py_name(placement.type_name or "")
			if placement.type_name not in self.structs:
				return ["", f"\t# {placement.path}: element type"
				        f" {placement.type_name} is not emitted yet."]
			return [
				"", f"\tdef {name}(self, index: int) -> {nested}:",
				f'\t\t"""Element `index` of {count}. Bounded by the count as',
				'\t\twell as the extent: bytes after the array are inside the',
				'\t\tview and are not elements."""',
				f"\t\tif not 0 <= index < {count}:",
				f"\t\t\traise IndexError(f\"{placement.path}[{{index}}] of {count}\")",
				f"\t\treturn {nested}(self._msg,",
				f"\t\t\tself._at + {placement.offset_bytes}"
				f" + index * {nested}.SIZE_BYTES, {nested}.SIZE_BYTES)",
			]

		lines = [
			"", "\t@property", f"\tdef {name}(self) -> memoryview:",
			f'\t\t"""{placement.path}: {count} bytes, zero copy and writable."""',
			"\t\tself._check()",
			f"\t\tstart = self._at + {placement.offset_bytes}",
			f"\t\treturn self._msg.buffer[start:start + {count}]",
		]

		if any(attr.name == "nul_terminated" for attr in placement.attrs):
			lines.extend([
				"", "\t@property", f"\tdef {name}_len(self) -> int:",
				f'\t\t"""Content length: to the first zero byte, or {count}."""',
				f"\t\treturn nul_len(self.{name}, {count})",
			])
		return lines

	def _variable(self, struct: ResolvedStruct, placement: Placement) -> list[str]:
		name   = py_name(local_name(struct, placement))
		start  = self._offset_expression(struct, placement)
		length = self._length_expression(struct, placement)

		nested = self.resolved.structs.get(placement.type_name or "")

		# The length is the member's total extent, which a walk does not need:
		# it steps element by element. Asking for it first refused a run of
		# variable-size records before the branch that walks them ran (26.36).
		if start is None or (length is None and (nested is None
		                                         or nested.layout.is_fixed_size)):
			return ["", f"\t# {placement.path}: sized by"
			        f" `{placement.sized_by}`, which this backend cannot",
			        "\t# resolve yet."]
		# Accumulating, like the delimited members' own offset elsewhere in
		# this file. This summed instead, and every term in the sum re-derived
		# its base by rescanning the members before it -- so the fix of 26.30
		# had reached the delimited members here and nothing placed after them.
		lines  = ["", "\t@property", f"\tdef {name}_offset(self) -> int:",
		          f'\t\t"""{placement.path}: offset and extent both from the data."""',
		          *(self._offset_body(struct, placement)
		            or [f"\t\treturn {start}"])]

		if nested is None:
			# An element wider than a byte, which is the same array `x[4]` is
			# with its count in the message. This handed back a memoryview of
			# the raw bytes, three lines under a generated comment saying why
			# that is not the member: the element is ValueConverted, so a
			# caller casting the slice gets host byte order for a schema that
			# names its own. C indexed it from the start.
			if indexed_elements(placement):
				# Not None here: the refusal above only lets a missing length
				# through for a variable-size struct element, which this is not.
				assert length is not None
				lines.extend(self._variable_elements(struct, placement, length))
				return lines

			lines.extend([
				"", "\t@property", f"\tdef {name}(self) -> memoryview:",
				"\t\tself._check()",
				f"\t\tstart = self._at + self.{name}_offset",
				f"\t\treturn self._msg.buffer[start:start + ({length})]",
			])
			return lines

		count = self._count_expression(struct, placement)
		inner = py_name(placement.type_name or "")

		# `x[remaining]` over elements with no single size: the bytes left are
		# known and how many elements are in them is not, so the count is the
		# walk. Without it `_count_expression` had no answer, the property
		# read `None`, and the span every member after the run is placed by
		# was never emitted at all -- an `AttributeError` at the first access
		# rather than a wrong number, which is the good failure and still a
		# schema this backend could not read.
		walked = (placement.sized_by == "remaining"
		          and not nested.layout.is_fixed_size
		          and has_computable_extent(self.resolved.structs, nested))
		if walked:
			count = f"self.{name}_span_from(self.{name}_offset, count_only=True)"

		lines.extend([
			"", "\t@property", f"\tdef {name}_count(self) -> int:",
			f"\t\treturn {count}",
			"", f"\tdef {name}(self, index: int) -> {inner}:",
			f'\t\t"""Element `index`, bounded by the count as well as the extent."""',
			f"\t\tif not 0 <= index < self.{name}_count:",
			f"\t\t\traise IndexError(f\"{placement.path}[{{index}}]\")",
			*([f"\t\treturn {inner}(self._msg,",
			   f"\t\t\tself._at + self.{name}_offset"
			   f" + index * {inner}.SIZE_BYTES,",
			   f"\t\t\t{inner}.SIZE_BYTES)"]
			  if nested.layout.is_fixed_size else [
				# No stride to index by, so the run is walked -- the
				# terminated run's walk with the count as its stopping rule.
				f"\t\tat = self.{name}_offset",
				"\t\tn  = 0",
				"",
				"\t\twhile at < self._len:",
				f"\t\t\telement = {inner}(self._msg, self._at + at,",
				"\t\t\t\tself._len - at)",
				"\t\t\tsize    = element._extent",
				"\t\t\tif size == 0 or at + size > self._len:",
				"\t\t\t\t# A zero-extent element would walk here forever,",
				"\t\t\t\t# and one past the limit was never in this frame.",
				"\t\t\t\tbreak",
				"\t\t\tif n == index:",
				f"\t\t\t\treturn {inner}(self._msg, self._at + at, size)",
				"\t\t\tat += size",
				"\t\t\tn  += 1",
				f"\t\traise IndexError(f\"{placement.path}[{{index}}]\")",
			  ]),
		])

		if not nested.layout.is_fixed_size and count is not None:
			lines.extend(self._counted_run_span(
				struct, placement, None if walked else count))
		return lines

	def _variable_elements(self, struct: ResolvedStruct, placement: Placement,
			length: str) -> list[str]:
		"""`u16 x[n]`: a count the message gives, and a getter taking an index.

		The count is clamped to what the frame holds, which is what the byte
		spelling gets for free from slicing and this has to say: a caller
		looping to the declared count would otherwise read past the end of the
		message on a length the message chose (invariant 41).
		"""
		name   = py_name(local_name(struct, placement))
		scalar = placement.scalar
		assert scalar is not None
		width  = scalar.bits // BITS_PER_BYTE
		load   = self._load(placement, scalar,
		                    offset=f"self.{name}_offset + index * {width}")

		return [
			"", "\t@property", f"\tdef {name}_count(self) -> int:",
			f'\t\t"""How many {scalar.name} elements of {placement.path} are'
			' here.',
			"",
			"\t\tThe message says how many there are; this says how many the",
			'\t\tframe holds, which is the one a caller may loop to."""',
			f"\t\treturn min({length},",
			f"\t\t\tmax(self._len - self.{name}_offset, 0)) // {width}",
			"", f"\tdef {name}(self, index: int) -> int:",
			f'\t\t"""Element `index`, an {scalar.name}.',
			"",
			"\t\tNo slice accessor: the element is ValueConverted, so bytes",
			'\t\thanded back whole would not be the values."""',
			f"\t\tif not 0 <= index < self.{name}_count:",
			f'\t\t\traise IndexError(f"{placement.path}[{{index}}]")',
			f"\t\treturn {load}",
		]

	def _counted_run_span(self, struct: ResolvedStruct, placement: Placement,
			count: str | None) -> list[str]:
		"""How far a counted run of variable elements reaches.

		The count says how many there are and each one says how long it is, so
		this is the indexing walk with no index to stop at -- and it is what
		places every member after the run. Nothing emitted it, so this backend
		declined those members and said their offsets could not be resolved,
		which was true only because the walk that resolves them was missing.
		"""
		name  = py_name(local_name(struct, placement))
		inner = py_name(placement.type_name or "")

		# `None` where the run is `[remaining]`: there is no count to stop at,
		# the frame is the bound, and the walk is what *produces* the count.
		# `count_only` is that second caller -- one walk with two questions,
		# rather than two walks that would have to agree.
		stop = "" if count is None else f"n < {count} and "

		return [
			"",
			f"	def {name}_span_from(self, start: int,",
			"			count_only: bool = False) -> int:",
			f'		"""The walk, from a base the caller already knows -- the'
			' same',
			'		helper every other walked run has."""',
			"		self._check()",
			"		at = start",
			"		n  = 0",
			"",
			f"		while {stop}at < self._len:",
			f"			element = {inner}(self._msg, self._at + at,"
			" self._len - at)",
			"			size    = element._extent",
			"			if size == 0 or at + size > self._len:",
			"				break",
			"			at += size",
			"			n  += 1",
			"",
			"		return n if count_only else at - start",
			"",
			"	@property",
			f"	def {name}_span(self) -> int:",
			f"		return self.{name}_span_from(self.{name}_offset)",
		]

	# -- dynamic arithmetic --------------------------------------------

	# -- delimited members (section 8.6) --------------------------------

	def _scan_call(self, placement: Placement, data: str, limit: str) -> str:
		delim = placement.delimiter
		assert delim is not None
		args = [data, limit, repr(delim)]

		if placement.delimiter_quote is not None:
			args.append(f"quote={placement.delimiter_quote}")
		if placement.delimiter_escape is not None:
			args.append(f"escape={placement.delimiter_escape}")
		return f"scan({', '.join(args)})"

	def _delimited(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""The same three numbers every backend gives, as properties.

		`_len` is the content, `_span` is content plus delimiter -- what the
		next member's offset is computed from -- and `_terminated` is neither.
		Properties rather than methods for the reason the rest of this backend
		gives: a caller who has to write `packet.line_len()` writes the parser
		by hand instead, and a backend nobody uses enforces nothing.
		"""
		assert placement.delimiter is not None
		name  = py_name(local_name(struct, placement))
		delim = placement.delimiter
		start = self._offset_expression(struct, placement)
		if start is None:
			return ["", f"\t# {placement.path}: this backend cannot resolve"
			        " where the scan starts."]

		# Saturating. `start` is a sum of length fields the message chooses,
		# so it can exceed the frame -- `u16 n` claiming 65535 in a ten-byte
		# view puts the scan base past the end, and an unsaturating
		# subtraction then hands `scan` about four billion bytes to search.
		# Measured as an AddressSanitizer SEGV before this line changed; the
		# C backend has been saturating here since the `[remaining]` fix and
		# these three were not.
		room  = f"max(0, self._len - ({start}))"
		limit = (room if placement.delimiter_cap is None
		         else f"min({placement.delimiter_cap}, {room})")
		data  = f"self._msg.buffer[self._at + ({start}):]"

		# The same two over a base handed in, so the loops that accumulate
		# offsets stop re-deriving one per term.
		room_at  = "max(0, self._len - at)"
		limit_at = (room_at if placement.delimiter_cap is None
		            else f"min({placement.delimiter_cap}, {room_at})")
		data_at  = "self._msg.buffer[self._at + at:]"

		# With `[trim]` the framing and the value are different numbers: the
		# scan says where the next member starts, and the value is what is
		# left after the whitespace at either end.
		scan = f"{name}_raw_len" if placement.trimmed else f"{name}_len"

		lines = [
			"",
			f"\t@property",
			f"\tdef {name}_offset(self) -> int:",
			f'\t\t"""{placement.path}: where the scan starts."""',
			*(self._offset_body(struct, placement) or [f"\t\treturn {start}"]),
			"",
			f"\tdef {scan}_from(self, at: int) -> int:",
			f'\t\t"""The scan, from a base the caller already knows."""',
			f"\t\treturn {self._scan_call(placement, data_at, limit_at)}",
			"",
			"\t@property",
			f"\tdef {scan}(self) -> int:",
			f'\t\t"""To the first {render_delimiter(delim)}, or the whole run."""',
			f"\t\treturn self.{scan}_from(self.{name}_offset)",
			"",
			f"\tdef {name}_terminated_from(self, at: int) -> bool:",
			f"\t\treturn self.{scan}_from(at) < ({limit_at})",
			"",
			f"\tdef {name}_span_from(self, at: int) -> int:",
			f'\t\t"""Content plus delimiter, from a known base."""',
			f"\t\treturn self.{scan}_from(at) + ({len(delim)}"
			f" if self.{name}_terminated_from(at) else 0)",
			"",
			"\t@property",
			f"\tdef {name}_terminated(self) -> bool:",
			f'\t\t"""Whether the delimiter is there at all. It is not when the'
			' frame was cut short."""',
			f"\t\treturn self.{scan} < ({limit})",
			"",
			"\t@property",
			f"\tdef {name}_span(self) -> int:",
			f'\t\t"""Content plus delimiter: where the next member starts."""',
			f"\t\treturn self.{name}_span_from(self.{name}_offset)",
			"",
			"\t@property",
			f"\tdef {name}_raw(self) -> memoryview:",
			f'\t\t"""The member\'s bytes, before anything is trimmed."""',
			"\t\tself._check()",
			f"\t\tstart = self._at + self.{name}_offset",
			f"\t\treturn self._msg.buffer[start:start + self.{scan}]",
		]

		if placement.trimmed:
			lines.extend([
				"",
				"\t@property",
				f"\tdef {name}_len(self) -> int:",
				f'\t\t"""The value\'s length: `[trim]` makes the whitespace at',
				'\t\teither end framing rather than value."""',
				f"\t\treturn len(trim(self.{name}_raw))",
			])

		value = f"trim(self.{name}_raw)" if placement.trimmed else f"self.{name}_raw"

		if placement.radix is None:
			lines.extend([
				"",
				"\t@property",
				f"\tdef {name}(self) -> memoryview | bytes:",
				f"\t\treturn {value}",
				"",
				f"\tdef {name}_eq(self, other: bytes) -> bool:",
				f'\t\t"""Whether {placement.path} is a given token, compared '
				+ ("folding" if placement.case_insensitive else "byte for"),
				("\t\tASCII case." if placement.case_insensitive
				 else "\t\tbyte."),
				"",
				"\t\tThe length is part of the comparison, so a prefix is not",
				'\t\ta match."""',
				(f"\t\treturn ascii_ci_eq({value}, other)"
				 if placement.case_insensitive
				 else f"\t\treturn bytes({value}) == other"),
			])
			return lines

		return lines + self._text_number(struct, placement, value)

	def _text_number(self, struct: ResolvedStruct, placement: Placement,
			value: str) -> list[str]:
		"""Digits, read through something that can say they are not digits.

		Raising rather than returning None, for the reason `validate` raises:
		idiom is not a capability, and a None a caller silently drops is worse
		than an exception they have to catch. `_value` is the same digits
		where nothing can be raised -- the offset arithmetic after this field
		is not fallible.
		"""
		assert placement.radix is not None
		scalar = placement.scalar
		assert scalar is not None

		name  = py_name(local_name(struct, placement))
		limit = (1 << scalar.bits) - 1
		raw   = value

		return [
			"",
			"\t@property",
			f"\tdef {name}(self) -> int:",
			f'\t\t"""{placement.path}: digits, in the range of {scalar.name}.',
			"",
			"\t\tThe only property here that can raise. Every other conversion",
			"\t\tis total; a decimal parse is not, and returning 0 for `12x4`",
			'\t\twould hand back a number nobody wrote."""',
			"\t\tself._check()",
			f"\t\tvalue = parse_uint({raw}, {placement.radix}, {limit})",
			"\t\tif value is None:",
			f"\t\t\traise ConstraintError(",
			f'\t\t\t\tf"{placement.path} is not a {scalar.name} written in '
			f'base {placement.radix}")',
			"\t\treturn value",
			"",
			"\t@property",
			f"\tdef {name}_value(self) -> int:",
			f'\t\t"""The same digits where nothing may raise.',
			"",
			"\t\tThe offset arithmetic after this field is not fallible, and",
			"\t\tmaking it so would put a try/except in every accessor after",
			"\t\tit. `validate` refuses a frame whose digits are not digits,",
			'\t\tso a validated one always parses here."""',
			f"\t\tvalue = parse_uint({raw}, {placement.radix}, {limit})",
			"\t\treturn 0 if value is None else value",
		]

	def _over_fields(self, struct: ResolvedStruct, source: str,
			held: str, bounded: bool = False) -> str:
		"""`bounded` holds every leaf to `LEAF_MAX`, which a size expression
		asks for (14.2b). Python's integers do not overflow, so this is here
		to *agree* with the three backends that do rather than for its own
		sake -- a bound applied in three places and not the fourth is a
		disagreement waiting for a lying message."""
		# Its own scalars and its nested structs': `at file.pixel_offset`
		# names a field of a header nested in this struct, and the dotted
		# path was emitted verbatim -- an attribute Python does not have.
		def leaf(text: str) -> str:
			return f"leaf({text})" if bounded else text

		by_local = {local_name(struct, placement): placement
		            for placement in readable_names(struct)}
		# Constants too. A `const` is a compile-time value and the renderer
		# only rewrote *fields*, so `align_up(HEADER_BYTES + n, 4)` reached
		# the target as `HEADER_BYTES` -- an identifier that exists in the
		# schema and in no generated file. Folding it here keeps the emitted
		# arithmetic the arithmetic the schema wrote.
		consts = self.resolved.layout.env.consts

		def read(local: str) -> str:
			if local in consts:
				return str(consts[local])
			placement = by_local[local]
			if "." not in local:
				# A varint's own property raises on a truncated encoding;
				# `_value` is the read that cannot, which is what the count
				# form already uses -- and a text number's raises for the
				# same reason, which only the nested branch below knew.
				suffix = ("_value"
				          if placement.varint is not None
				          or placement.radix is not None else "")
				return leaf(f"{held}.{py_name(local)}{suffix}")
			# A text number is digits, not bytes of an integer. Reading it
			# where it sits gave `situ_get_be32` over eight ASCII characters
			# -- a plausible number nobody wrote, which is the shape 26.32
			# rates worst. The value helper parses them.
			if placement.radix is not None:
				return leaf(f"{held}.{py_name(local)}_value")
			# A nested member has no attribute of this struct's own, and its
			# offset is a constant here, so it is read where it sits.
			assert placement.scalar is not None
			return f"({self._raw_load(placement, placement.scalar)})"

		return expand_calls(
			_pythonic(over_fields([*by_local, *consts], source, read)),
			python_spelling)

	def _repeat_while(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""A run ending after the element that fails a condition (8.6.6)."""
		element = self.resolved.structs.get(placement.type_name or "")
		if element is None or self._extent_expression(element) is None:
			return ["", f"\t# No accessors for {placement.path}: one"
			        f" `{placement.type_name}` has no extent",
			        "\t# this backend can compute."]

		name  = py_name(local_name(struct, placement))
		inner = py_name(placement.type_name or "")
		start = self._offset_expression(struct, placement)
		if start is None:
			return ["", f"\t# {placement.path}: this backend cannot resolve"
			        " where the run starts."]

		cond = self._over_fields(element, placement.repeat_while or "", "element")
		cap  = ("" if placement.repeat_cap is None
		        else f" and len(starts) <= {placement.repeat_cap}")

		return [
			"",
			f"\tdef _{name}_walk_from(self, start: int) -> list[int]:",
			f'\t\t"""Where each `{placement.type_name}` starts, and where the'
			" run ends,",
			"\t\tfrom a base the caller already knows.",
			"",
			f"\t\tThe run ends after the element for which",
			f"\t\t`{placement.repeat_while}` is false -- that element is part",
			"\t\tof it, because the condition is asked once it has been read.",
			"",
			"\t\tBounded twice, by the buffer and by refusing to advance on a",
			'\t\tzero-extent element."""',
			"\t\tself._check()",
			"\t\tat     = start",
			"\t\tstarts: list[int] = []",
			"",
			f"\t\twhile at < self._len{cap}:",
			f"\t\t\telement = {inner}(self._msg, self._at + at, self._len - at)",
			"\t\t\tsize    = element._extent",
			"\t\t\tif size == 0 or at + size > self._len:",
			"\t\t\t\tbreak",
			"\t\t\tstarts.append(at)",
			"\t\t\tat += size",
			f"\t\t\tif not ({cond}):",
			"\t\t\t\tbreak",
			"",
			"\t\tstarts.append(at)",
			"\t\treturn starts",
			"",
			f"\tdef _{name}_walk(self) -> list[int]:",
			f"\t\treturn self._{name}_walk_from({start})",
			"",
			"\t@property",
			f"\tdef {name}_count(self) -> int:",
			f"\t\treturn len(self._{name}_walk()) - 1",
			"",
			f"\tdef {name}_span_from(self, at: int) -> int:",
			'\t\t"""The walk, from a base the caller already knows: the same',
			"\t\thelper every delimited member has. An accumulating pass holds",
			"\t\tthe base already, and the plain `_span` re-resolves it by",
			'\t\trescanning every member before the run."""',
			f"\t\treturn self._{name}_walk_from(at)[-1] - at",
			"",
			"\t@property",
			f"\tdef {name}_span(self) -> int:",
			f"\t\treturn self.{name}_span_from({start})",
			"",
			f"\tdef {name}(self, index: int) -> {inner}:",
			f"\t\tstarts = self._{name}_walk()",
			"\t\tif not 0 <= index < len(starts) - 1:",
			f'\t\t\traise IndexError(f"{placement.path}[{{index}}]")',
			f"\t\treturn {inner}(self._msg, self._at + starts[index],",
			"\t\t\tstarts[index + 1] - starts[index])",
			*self._run_index(struct, placement, inner),
		]

	def _run_index(self, struct: ResolvedStruct, placement: Placement,
			inner: str) -> list[str]:
		"""The second family for a run: every element, walked once (0022).

		`x(i)` rebuilds the walk on every call, so visiting all of them is
		quadratic. This walks once and hands back the elements.

		No `max` is needed, unlike C. The cap there is how many offsets to
		hold, because generated C never allocates; here the list is the
		language's and the schema's bound on the walk still bounds it. The
		same decision reaches a different construct in each -- which is what
		it means for the family to be the consumer's rather than the
		schema's.
		"""
		if not self.materialize:
			return []

		name = py_name(local_name(struct, placement))
		return [
			"",
			f"\tdef {name}_all(self) -> list[{inner}]:",
			f'\t\t"""Every `{placement.type_name}` in `{placement.path}`,',
			"",
			"\t\twalked once. The map calls this run `access = Sequential`,",
			"\t\twhich is the cost of reaching element N by reading the N-1",
			'\t\tbefore it -- paid once here rather than once per index."""',
			f"\t\tstarts = self._{name}_walk()",
			f"\t\treturn [{inner}(self._msg, self._at + starts[i],",
			"\t\t\tstarts[i + 1] - starts[i])",
			"\t\t\tfor i in range(len(starts) - 1)]",
		]

	def _record_run(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""A run of records, ending where the terminator would be an element."""
		assert placement.delimiter is not None
		element = self.resolved.structs.get(placement.type_name or "")
		if element is None or self._extent_expression(element) is None:
			return ["", f"\t# No accessors for {placement.path}: one"
			        f" `{placement.type_name}` has no extent",
			        "\t# this backend can compute, so the run cannot be walked."]

		name  = py_name(local_name(struct, placement))
		delim = placement.delimiter
		inner = py_name(placement.type_name or "")
		start = self._offset_expression(struct, placement)
		if start is None:
			return ["", f"\t# {placement.path}: this backend cannot resolve"
			        " where the run starts."]

		return [
			"",
			f"\tdef _{name}_walk_from(self, start: int) -> list[int]:",
			f'\t\t"""Where each `{placement.type_name}` starts, and where the run'
			" ends,",
			"\t\tfrom a base the caller already knows.",
			"",
			f"\t\tThe terminator only terminates where an element would start;",
			"\t\tinside one it is that element's own byte. Bounded twice over,",
			"\t\tby the buffer and by refusing to advance on a zero-extent",
			"\t\telement -- a record whose members are all delimited and all",
			'\t\tempty occupies no bytes, and this would not return."""',
			"\t\tself._check()",
			"\t\tat     = start",
			"\t\tstarts: list[int] = []",
			"",
			f"\t\twhile at + {len(delim)} <= self._len:",
			f"\t\t\tif bytes(self._msg.buffer[self._at + at:"
			f"self._at + at + {len(delim)}]) == {delim!r}:",
			"\t\t\t\tbreak",
			f"\t\t\telement = {inner}(self._msg, self._at + at, self._len - at)",
			"\t\t\tsize    = element._extent",
			"\t\t\tif size == 0 or at + size > self._len:",
			"\t\t\t\tbreak",
			"\t\t\tstarts.append(at)",
			"\t\t\tat += size",
			"",
			f"\t\tstarts.append(at + ({len(delim)} if at + {len(delim)}"
			" <= self._len else 0))",
			"\t\treturn starts",
			"",
			f"\tdef _{name}_walk(self) -> list[int]:",
			f"\t\treturn self._{name}_walk_from({start})",
			"",
			"\t@property",
			f"\tdef {name}_count(self) -> int:",
			f"\t\treturn len(self._{name}_walk()) - 1",
			"",
			f"\tdef {name}_span_from(self, at: int) -> int:",
			'\t\t"""Every element plus the terminator, from a base the caller',
			"\t\talready knows -- the same helper every delimited member has,",
			'\t\tand for the same reason."""',
			f"\t\treturn self._{name}_walk_from(at)[-1] - at",
			"",
			"\t@property",
			f"\tdef {name}_span(self) -> int:",
			f'\t\t"""Every element plus the terminator: where the next member'
			' starts."""',
			f"\t\treturn self.{name}_span_from({start})",
			"",
			f"\tdef {name}(self, index: int) -> {inner}:",
			f'\t\t"""Element `index`. Walked, not indexed: a view is a value and',
			"\t\tnothing here keeps a table of offsets, which is what",
			'\t\t`access = Sequential` in the map is telling you."""',
			f"\t\tstarts = self._{name}_walk()",
			"\t\tif not 0 <= index < len(starts) - 1:",
			f'\t\t\traise IndexError(f"{placement.path}[{{index}}]")',
			f"\t\treturn {inner}(self._msg, self._at + starts[index],",
			"\t\t\tstarts[index + 1] - starts[index])",
		]

	def _required(self, struct: ResolvedStruct) -> list[str]:
		"""Framing: is a whole message here, and if not how many bytes? (20.3)

		Raises rather than returning a pair, which is this backend's
		convention everywhere else -- and `TruncatedError` carries `needed`,
		so the caller who wants the number has it without a tuple to unpack.

		A classmethod over raw bytes: framing is asked before there is a view
		to ask. The length expressions are instance reads, so it builds one
		over the prefix, which costs a `Message` and a `View` and nothing
		else.
		"""
		if struct.layout.register is not None:
			return []

		name = py_name(struct.name)
		head = [
			"", "\t@classmethod",
			"\tdef required(cls, data: bytes | bytearray | memoryview) -> int:",
			f'\t\t"""How many bytes a whole `{struct.name}` needs, given these.',
			"",
			"\t\tReturns the total length when a complete one is present, and",
			"\t\traises `TruncatedError` otherwise -- whose `needed` is a lower",
			'\t\tbound on that total, so the next read can be sized."""',
			"\t\thave = len(data)",
		]

		if struct.layout.is_fixed_size and struct.layout.is_byte_sized:
			return [*head,
			        "\t\tif have < cls.SIZE_BYTES:",
			        f'\t\t\traise TruncatedError("{struct.name}: "',
			        '\t\t\t\tf"{cls.SIZE_BYTES} bytes needed, {have} here",',
			        "\t\t\t\tcls.SIZE_BYTES)",
			        "\t\treturn cls.SIZE_BYTES"]

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

			local = py_name(local_name(struct, placement))
			steps.extend([
				"",
				f"\t\t# {placement.path}: reading its length means reading",
				"\t\t# bytes that have to be here first.",
				"\t\tif have < at:",
				f'\t\t\traise TruncatedError("{placement.path}: '
				'incomplete", at)',
			])
			if placement.delimiter is not None:
				steps.extend([
					f"\t\tif not probe.{local}_terminated:",
					"\t\t\t# The delimiter is not in what we have, and how"
					" much more",
					"\t\t\t# is the sender's to know. One byte is the honest"
					" bound.",
					f'\t\t\traise TruncatedError("{placement.path}: '
					'no delimiter yet", have + 1)',
				])
			steps.append(f"\t\tat += {length.replace('self.', 'probe.')}")

		return [
			*head,
			f"\t\tat = {constant}",
			"\t\tif have < cls.SIZE_MIN:",
			f'\t\t\traise TruncatedError("{struct.name}: incomplete",'
			" cls.SIZE_MIN)",
			"",
			"\t\t# A view over what has arrived, so every length below reads",
			"\t\t# through the same bounds the accessors do.",
			"\t\tprobe = cls(Message(bytearray(data)), 0, have)",
			*steps,
			"",
			"\t\tif have < at:",
			f'\t\t\traise TruncatedError("{struct.name}: incomplete", at)',
			"\t\treturn at",
		]

	def _framing_walk(self, struct: ResolvedStruct,
			placement: Placement) -> list[str] | None:
		"""Framing a run: one element at a time, through the element's own
		`required`.

		The walk the accessors use stops at the end of the bytes as readily as
		at the end of the run, and those are opposite answers to "is a whole
		one here?". The element's own framing is what separates them, being
		the same question one level down.

		`TruncatedError` carries the bound, so the element's own is re-raised
		with this member's offset added to it rather than replaced.
		"""
		element = self.resolved.structs.get(placement.type_name or "")
		if element is None or not frameable(self.resolved.structs, element):
			return None

		inner = py_name(element.name)
		read  = [
			"\t\t\ttry:",
			f"\t\t\t\tpart = {inner}.required(data[at:])",
			"\t\t\texcept TruncatedError as short:",
			f'\t\t\t\traise TruncatedError("{placement.path}: incomplete",',
			"\t\t\t\t\tat + short.needed) from None",
		]
		cap  = placement.repeat_cap if placement.repeat_while else None
		body: list[str] = []

		if placement.repeat_while is not None:
			cond = self._over_fields(element, placement.repeat_while or "",
			                         "element")
			body.extend([
				*read,
				f"\t\t\telement = {inner}(probe._msg, at, part)",
				"\t\t\tat += part",
				"",
				"\t\t\t# The condition is asked about the element just"
				" read, which",
				"\t\t\t# is the whole difference from a delimiter -- and"
				" only once",
				"\t\t\t# that element is known to be entirely here.",
				f"\t\t\tif not ({cond}):",
				"\t\t\t\tbreak",
				*([] if cap is None else [
					"\t\t\tseen += 1",
					f"\t\t\tif seen == {cap}:",
					"\t\t\t\tbreak",
				]),
			])
		else:
			delim = placement.delimiter
			assert delim is not None
			body.extend([
				f"\t\t\tif have < at + {len(delim)}:",
				f'\t\t\t\traise TruncatedError("{placement.path}:'
				' no terminator yet",',
				f"\t\t\t\t\tat + {len(delim)})",
				"",
				"\t\t\t# The terminator only terminates where an element"
				" would",
				"\t\t\t# start. It belongs to this member, as a delimiter"
				" does.",
				f"\t\t\tif bytes(data[at:at + {len(delim)}]) == {delim!r}:",
				f"\t\t\t\tat += {len(delim)}",
				"\t\t\t\tbreak",
				"",
				*read,
				"\t\t\tat += part",
			])

		loop = ["\t\twhile True:", *body]
		if cap is not None:
			loop = ["\t\tseen = 0",
			        *loop]

		return [
			"",
			f"\t\t# {placement.path}: a run of `{element.name}`, framed one",
			"\t\t# element at a time -- the walk the accessors use cannot"
			" tell the",
			"\t\t# end of the run from the end of the bytes, and this asks"
			" each",
			"\t\t# element whether it is whole.",
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
			f"\t# No `required`: {reason}.",
			"\t# Framing such a message is the layer below's job.",
		]

	def _extent_expression(self, struct: ResolvedStruct) -> str | None:
		"""How many bytes one instance of a variable struct occupies."""
		# The arithmetic and the refusals are shared
		# (traverse.extent_parts); rendering one length is Python's business.
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

	def _extent_property(self, struct: ResolvedStruct) -> list[str]:
		"""Emitted only for a type something walks a run of."""
		# A run walks them and a nested member sizes itself from one -- and a
		# *count*-driven run of elements with no single size walks them too,
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
			"\t@property",
			"\tdef _extent(self) -> int:",
			f'\t\t"""How many bytes one `{struct.name}` occupies.',
			"",
			"\t\tA run of these is walked, and the walk needs to know where",
			'\t\teach one ends."""',
			f"\t\treturn {extent}",
		]

	def _offset_expression(self, struct: ResolvedStruct,
			placement: Placement) -> str | None:
		if placement.offset_bits is not None:
			return str(placement.offset_bits // BITS_PER_BYTE)

		parts = preceding_parts(struct, placement)
		if parts is None:
			return None

		constant = sum(part for part in parts if isinstance(part, int))
		terms: list[str | tuple[str, int]] = []

		for other in parts:
			if isinstance(other, int):
				continue
			pad = pad_alignment(other)
			if pad is not None:
				terms.append(("align", pad))
				continue
			length = self._length_expression(struct, other)
			if length is None:
				return None
			terms.append(length)

		if not terms:
			return str(constant)

		# Saturating, term by term. Python is the one backend where this is
		# not a safety question -- a short slice is short, not a read past the
		# buffer -- and it is still the same arithmetic, because four backends
		# that disagree about where a member is mean the schema means four
		# things (26.27).
		folded = str(constant)
		for term in terms:
			if isinstance(term, tuple):
				folded = f"align_up({folded}, {term[1]}, self._len)"
			else:
				folded = f"advance({folded}, {term}, self._len)"
		return folded

	def _fits(self, struct: ResolvedStruct, placement: Placement,
			bytes_: int) -> str | None:
		"""Whether a fixed-size member at a *dynamic* offset is in the view."""
		if placement.offset_bits is not None:
			return None
		start = self._offset_expression(struct, placement)
		if start is None:
			return None
		return f"self._len - ({start}) >= {bytes_}"

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
				f"\t\t# {placement.path}: its offset is a sum of lengths the",
				"\t\t# message chose, so the frame is not known to contain it.",
				f"\t\tif not ({fits}):",
				f'\t\t\traise BoundsError("{placement.path}: outside the'
				' frame")',
			]

		declared = self._raw_length_expression(struct, placement)
		start    = self._offset_expression(struct, placement)
		if declared is None or start is None:
			return []
		lines = [
			f"\t\t# {placement.path}: the length the message declares has to",
			"\t\t# fit the frame it is in.",
			f"\t\tif self._len - ({start}) < ({declared}):",
			# `BoundsError`, which is what the other three report here: a
			# length that does not fit the frame is the message claiming bytes
			# that are not there, not a value the schema forbids. This raised
			# a constraint failure, so a receiver told the same message was
			# malformed in one language and out of bounds in three -- found by
			# diffing all four over random buffers.
			f'\t\t\traise BoundsError("{placement.path}: declared length'
			' does not fit")',
		]

		# And within the pin, which the frame check does not cover: it
		# compares with what is left in the buffer, and anything after this
		# member makes that larger than the member itself (0039).
		pinned = pinned_bytes(placement)
		if pinned is not None:
			lines += [
				f"\t\t# {placement.path}: and within the {pinned} bytes"
				" `[size]` pins it to.",
				f"\t\tif ({declared}) > {pinned}:",
				f'\t\t\traise BoundsError("{placement.path}: declared length'
				f' exceeds its pinned {pinned} bytes")',
			]

		return lines

	def _discriminant_check(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""`default: error` -- the discriminant must select an arm.

		Section 14.5 says an unrecognised discriminant is rejected on parse,
		and no backend rejected it. It stayed invisible while a variant had no
		computable extent, because nothing walked one.

		`VersionError`, not `ConstraintError`: the runtime has named this
		condition "unknown version or variant discriminant" since it was
		written, and the other three raise it. This one raised a constraint
		failure, which tells a receiver the message is malformed where the
		other three tell it the message is newer than this code -- opposite
		remedies for the same bytes (19.4). Found by handing random buffers to
		all four and diffing what they said about `example/dnsname`, whose
		label form `2` is the reserved encoding.
		"""
		values = matched_values(placement)
		if not values or placement.discriminant is None:
			return []

		held = self._over_fields(struct, placement.discriminant, "self")
		test = " and ".join(f"{held} != {arm.value}" for arm in values)
		named = ", ".join(arm.source or str(arm.value) for arm in values)
		return [
			f"\t\t# {placement.path}: an arm for {named}, and"
			f" `default: error` for the rest.",
			f"\t\tif {test}:",
			f'\t\t\traise VersionError("{placement.path}: no arm for'
			f' this {placement.discriminant}")',
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
				length = f"({rendered})"

			chain = (length if arm.value is None
			         else f"({length} if {held} == {arm.value} else {chain})")
		return chain

	def _offset_body(self, struct: ResolvedStruct,
			placement: Placement) -> list[str] | None:
		"""The offset accessor's body, accumulating rather than summing.

		`_offset_expression` builds `0 + a_span + b_span`, and each of those
		re-derives its own base by rescanning everything before it, so the
		expression costs far more than the terms in it. An expression cannot
		hold a running total; this is the same sum as statements, with each
		span given the offset already reached.
		"""
		if placement.offset_bits is not None:
			return None

		parts = preceding_parts(struct, placement)
		if parts is None:
			return None

		lines = ["\t\tat = 0"]
		for other in parts:
			if isinstance(other, int):
				if other:
					lines.append(f"\t\tat += {other}")
				continue
			length = self._length_expression(struct, other, running="at")
			if length is None:
				return None
			# Saturating, like the expression form: the four have to place a
			# member at the same offset for the same bytes, hostile ones
			# included.
			lines.append(f"\t\tat = advance(at, {length}, self._len)")
		return [*lines, "\t\treturn at"]

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
			return f"(({inner}) * {rule.out}) // {rule.into}"
		return (f"((({inner}) + {rule.group_in - 1})"
		        f" // {rule.group_in}) * {rule.group_out}")

	def _length_expression(self, struct: ResolvedStruct,
			placement: Placement, running: str | None = None) -> str | None:
		"""The length a caller sees, clamped to `[size = N]` where one is.

		A pinned member holds N bytes whatever the length field says, so a
		declared 127 inside a 16-byte pin is 16 here. `validate` wants the
		raw value instead -- it reports the 127 as malformed -- and asks
		`_raw_length_expression` for it.

		The clamp lives here rather than at the accessor sites because
		there are four of them per backend and the differential found the
		one that was missed (0039).
		"""
		found = self._raw_length_expression(struct, placement, running)
		pin   = pinned_bytes(placement)
		if pin is None or found is None:
			return found
		return f"min({found}, {pin})"

	def _raw_length_expression(self, struct: ResolvedStruct,
			placement: Placement, running: str | None = None) -> str | None:
		if placement.kind == "variant":
			return self._variant_length(struct, placement)

		# A delimited member's extent is wherever the delimiter turns out to
		# be, and `_span` is the member's own answer. One name for "how far
		# this member reaches", whether it is a byte run or a run of records.
		if (placement.delimiter is not None or placement.repeat_while is not None
				or is_counted_run(self.resolved.structs, placement)):
			name = py_name(local_name(struct, placement))
			# Every kind that reaches here has the `_from` form: a byte array's
			# scan, a record run's walk and a `while` run's. The runs were the
			# exception, and it cost a rescan of everything before the run on
			# every accumulating pass over it.
			if running is not None:
				return f"self.{name}_span_from({running})"
			return f"self.{name}_span"

		# Arithmetic over a field rather than a reference to one. Without this
		# the member fell through to the scalar case and this backend read one
		# byte and called it the field.
		if placement.size_expr is not None:
			# Bounded leaves and one clamp, matching the other three (14.2b).
			counted = self._over_fields(struct, placement.size_expr, "self",
			                            bounded=True)
			each    = element_bytes(placement)
			signed  = counted if each == 1 else f"({counted}) * {each}"
			return f"nonneg({signed})"

		# A nested struct with no single size, which the sum treated as zero
		# bytes wide -- so whatever followed it was placed on top of it.
		inner = self.resolved.structs.get(placement.type_name or "")
		if (inner is not None and not inner.layout.is_fixed_size
				and placement.kind == "field"
				and placement.array_count is None
				and placement.sized_by is None):
			if not has_computable_extent(self.resolved.structs, inner):
				return None		# and so nothing after it can be placed
			return f"self.{py_name(local_name(struct, placement))}_extent"

		if placement.kind in ("coded", "sealed"):
			return self._region_length(struct, placement)

		if placement.varint is not None:
			if not self._reads_varint(placement):
				return None
			return f"self.{py_name(local_name(struct, placement))}_len"

		# An opaque region's size expression is already a byte count: there are
		# no elements to multiply by, and asking for an element width finds no
		# scalar and gives up. C has had this branch all along.
		if placement.kind == "opaque":
			count = self._count_expression(struct, placement)
			return None if count is None else f"({count})"

		if placement.sized_by == "remaining":
			start = self._offset_expression(struct, placement)
			return None if start is None else f"(self._len - ({start}))"
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
		if count is None:
			return None

		element = self._element_bytes(placement)
		if element is None:
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
			return f"self.{py_name(local_name(struct, driver.placement))}_value"

		if driver.placement.scalar is None:
			return None

		# A text driver holds digits, not bits, and sits behind the scans of
		# everything before it -- so neither the offset check below nor the
		# raw load after it applies.
		if driver.placement.radix is not None:
			return f"self.{py_name(local_name(struct, driver.placement))}_value"

		if driver.placement.offset_bits is None:
			# The driver is itself behind a variable-length member, so there
			# is no constant to read it at -- but its own accessor knows where
			# it is, and this backend has always emitted one. Reading at a
			# static offset was the only thing tried, so the member it sizes
			# was dropped with a note, which is the whole of what "cannot
			# resolve" meant. All three backends had it; C did not.
			name = py_name(local_name(struct, driver.placement))
			return f"self.{name}"
		return self._raw_load(driver.placement, driver.placement.scalar)

	def _element_bytes(self, placement: Placement) -> int | None:
		nested = self.resolved.structs.get(placement.type_name or "")
		if nested is not None:
			return (int(nested.layout.size_bytes)
			        if nested.layout.is_fixed_size else None)
		if placement.scalar is not None and placement.scalar.bits % BITS_PER_BYTE == 0:
			return max(placement.scalar.bits // BITS_PER_BYTE, 1)
		return None

	# -- validation ----------------------------------------------------

	def _validate(self, struct: ResolvedStruct) -> list[str]:
		checks: list[str] = []
		for entry in own_entries(struct):
			checks.extend(self._check(struct, entry))

		lines = [
			"", "\tdef validate(self) -> None:",
			'\t\t"""Every constraint the schema declares, on parse.',
			"",
			"\t\tRaises ConstraintError rather than returning a code: a Python",
			'\t\tcaller drops a return value far too easily."""',
		]
		if not checks:
			lines.append("\t\t# Nothing in this struct is constrained.")
			lines.append("\t\treturn")
		lines.extend(checks)
		return lines

	def _delimiter_check(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""The delimiter is there, and a text number's digits are digits.

		Terminated first, and deliberately: for a frame cut short before the
		digits both are wrong, and "this frame stops early" is the more useful
		of the two answers. C reports it in that order and so does this.
		"""
		if "." in placement.path[len(struct.name) + 1:]:
			return []		# checked under the element's own struct

		name  = py_name(local_name(struct, placement))
		delim = placement.delimiter
		assert delim is not None

		lines = [
			f"\t\tif not self.{name}_terminated:",
			f"\t\t\traise ConstraintError(",
			# `bytes` repr rather than `render_delimiter`, which is the one
			# place the two differ: the readable form holds both backslashes
			# and double quotes, and this goes inside a double-quoted Python
			# literal in generated source. `b'\r\n'` says the same thing and
			# survives the trip.
			f'\t\t\t\t"{placement.path} has no '
			f'{repr(delim).replace(chr(92), chr(92) * 2)}: '
			'the frame stops first")',
		]
		# The encoding, over the span the scan found. See the C backend for
		# why it lives here rather than with the fixed-width check, which
		# needs a static offset and a declared count.
		named = next((attr for attr in placement.attrs
		              if attr.name == "encoding"), None)
		spelling = getattr(named.value, "name", None) if named else None
		if spelling in ("ascii", "utf8", "utf16le", "utf16be"):
			lines.extend([
				f"\t\tif not {spelling}_valid(self.{name}_raw):",
				"\t\t\traise ConstraintError(",
				f'\t\t\t\t"{placement.path} is not {spelling}")',
			])

		if placement.radix_minimal:
			# The *digits*, which is what the predicate reads. This passed
			# `self.{name}` -- the parsed number -- and `bytes(6)` in Python is
			# six zero bytes rather than the digit `6`, so the check was
			# vacuous in both directions: `007` was minimal because none of
			# those NULs is an ASCII zero, and a code of `0` was not, because
			# `bytes(0)` is empty and no digits is not a number. No exception
			# anywhere, and three backends passing the bytes.
			digits = (f"trim(self.{name}_raw)" if placement.trimmed
			          else f"self.{name}_raw")
			lines.extend(self._minimal_check(placement, name, digits))
		if placement.radix is not None:
			# Reading it is the check: the property raises for digits that are
			# not digits, which is the whole of what it is for.
			lines.append(f"\t\t_ = self.{name}")
			# And whatever the schema declared about the number. This branch
			# returns before the scalar path that emits those, so a delimited
			# text number's `[min]` and `[max]` reached no backend.
			lines.extend(self._attr_checks(struct, placement, f"self.{name}"))
		return lines

	def _attr_checks(self, struct: ResolvedStruct, placement: Placement,
			read: str) -> list[str]:
		"""`[must_eq]`, `[min]` and `[max]`, against whatever reads the value.

		Both routes into a constrained member need these; only `read`
		differs -- a property, or a local bound behind a version gate.
		"""
		from situc.expr import evaluate

		from situc.diagnostics import SituError
		from situc.invariant import bound as bound_expression
		from situc.invariant import bound_refusal, bound_widening

		lines: list[str] = []
		for attr in placement.attrs:
			if attr.name not in ("must_eq", "max", "min") or attr.value is None:
				continue
			operator = {"must_eq": "!=", "max": ">", "min": "<"}[attr.name]
			try:
				expected = str(evaluate(attr.value, self.resolved.layout.env))
				read_as  = read
			except SituError as why:
				# A bound naming a sibling is checked against a message that
				# is in front of you, so the value is there to read.
				rendered = bound_expression(struct, attr.value, self)
				if rendered is None:
					raise bound_refusal(struct, attr.value, why) from why
				too_wide = bound_widening(struct, placement, attr.value)
				if too_wide is not None:
					raise too_wide from why
				expected = f"({rendered})"
				read_as  = f"int({read})"

			lines.extend([
				f"\t\tif {read_as} {operator} {expected}:",
				f"\t\t\traise ConstraintError("
				f"f\"{placement.path} is {{{read}}},"
				f" {attr.name} {expected}\")",
			])
		return lines

	def _minimal_check(self, placement: Placement, name: str,
			digits: str) -> list[str]:
		"""`[minimal]`: one spelling per value.

		Shared by both forms of the text number so that the two cannot start
		disagreeing about what the predicate reads -- which they have before:
		this was passed `self.{name}`, the parsed number, and `bytes(6)` is
		six zero bytes rather than the digit `6`.
		"""
		if not placement.radix_minimal:
			return []
		return [
			f"\t\tif not digits_minimal({digits}, {placement.radix}):",
			"\t\t\traise ConstraintError(",
			f'\t\t\t\t"{placement.path} is not the minimal spelling of '
			'its value")',
		]

	def _check(self, struct: ResolvedStruct, entry: Resolved) -> list[str]:
		"""Everything `validate` says about one member.

		The bounds question first, and it does not replace the rest: a member
		can be both outside the frame and constrained, and returning on the
		first left `[must_eq]` unchecked for every dynamically placed field.
		"""
		return [*self._fits_check(struct, entry.placement),
		        *self._arm_checks(struct, entry.placement),
		        *self._member_check(struct, entry)]

	def _arm_checks(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""Every arm of this variant, in declaration order.

		Asked at the variant rather than at the arm: `own_entries` drops a
		dotted path, and every arm member has one, so an arm never reaches
		the validate loop on its own.
		"""
		if placement.kind != "variant":
			return []
		found: list[str] = []
		for _, member in arm_members(struct, placement):
			if member is not None:
				found.extend(self._arm_fits_check(struct, member))
				found.extend(self._arm_validation(struct, member))
		return found

	def _arm_validation(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""Validate the arm the discriminant selects, through its own type.

		Nothing did, in any backend: a variant's check was the discriminant
		and nothing else, so every constraint inside an arm was declared by
		the schema and enforced by nobody. Through the arm's own accessor,
		which already refuses the arm that is not present.
		"""
		inner = self.resolved.structs.get(placement.type_name or "")
		if inner is None:
			return []
		if not inner.layout.is_fixed_size \
				and not has_computable_extent(self.resolved.structs, inner):
			return []

		# A name per arm, not one `arm` reused: the arms have different
		# types, and mypy --strict reads a second assignment to the same
		# local as a type error rather than as a new variable.
		name = py_name(local_name(struct, placement)).replace(".", "_")
		return [
			f"\t\t# {placement.path}: the arm the discriminant selects",
			"\t\t# carries its own constraints, and its own validator knows",
			"\t\t# them.",
			"\t\ttry:",
			f"\t\t\tself.{name}.validate()",
			"\t\texcept VersionError:",
			f"\t\t\tpass\t\t# not the arm this message carries",
		]

	def _arm_fits_check(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""The arm the discriminant selects has to fit the frame it is in.

		`_fits_check` skips a dotted path and every arm member has one, so a
		variant was outside the length checks entirely -- and a DNS label
		declaring 55 bytes in a five-byte frame is the same lie
		`u8 opts[hdr.length]` tells. The accessors clamp it (26.35); this is
		the other half, without which clamping turns a lie into a truncation.

		Through the accessor rather than by re-deriving the discriminant test:
		it raises for the arm that is not present and clamps the one that is,
		so a short answer is the mismatch.
		"""
		scalar = placement.scalar
		if placement.sized_by is None or scalar is None:
			return []
		if scalar.bits != BITS_PER_BYTE:
			return []
		declared = self._length_expression(struct, placement)
		if declared is None:
			return []

		name = py_name(local_name(struct, placement))
		return [
			f"\t\t# {placement.path}: the arm the discriminant selects has to",
			"\t\t# fit the frame. The accessor clamps; this is where a message",
			"\t\t# that does not fit is called malformed.",
			"\t\ttry:",
			f"\t\t\tif len(self.{name}) < ({declared}):",
			f'\t\t\t\traise BoundsError("{placement.path}: the arm declares'
			' more than the frame holds")',
			"\t\texcept VersionError:",
			"\t\t\tpass\t\t# another arm is the one present",
		]

	def _member_check(self, struct: ResolvedStruct,
			entry: Resolved) -> list[str]:
		from situc.expr import evaluate

		placement = entry.placement
		scalar    = placement.scalar
		name      = py_name(local_name(struct, placement))

		check = classify_check(struct, placement, self.structs)

		if check is Check.DISCRIMINANT:
			return self._discriminant_check(struct, placement)
		if check is Check.DELIMITED:
			return self._delimiter_check(struct, placement)
		if check is Check.NOTHING:
			return []
		if check is Check.REPEATED:
			return self._array_check(struct, placement, scalar, name)
		if check is Check.NESTED:
			# Only where the accessor exists. Validation reaching for a
			# member the emitter declined to expose is the same crash as any
			# other, arriving on the path people are least likely to test.
			inner = self.resolved.structs.get(placement.type_name or "")
			if inner is not None and not inner.layout.is_fixed_size \
					and not has_computable_extent(self.resolved.structs, inner):
				return [f"\t\t# {placement.path}: no accessor to validate through."]
			return [f"\t\tself.{name}.validate()"]

		assert scalar is not None

		if check is Check.RESERVED:
			# At a dynamic offset there is no constant to load from, and
			# reaching for one crashed the compiler. For a whole-byte scalar
			# the byte comparison says the same thing in either byte order.
			if placement.offset_bits is None:
				return self._reserved_check(struct, placement)
			policy = _reserved_policy(placement.attrs)
			if policy not in ("must_be_zero", "must_be_one"):
				return []
			want = 0 if policy == "must_be_zero" else (1 << scalar.bits) - 1
			return [
				f"\t\tif {self._raw_load(placement, scalar)} != {want}:",
				f"\t\t\traise ConstraintError("
				f"\"{placement.path} is reserved [{policy}]\")",
			]

		lines: list[str] = []

		# A versioned member's property raises `VersionError` where the
		# message is older than the field. Reading it here without saying so
		# made `validate` raise that out of a message which is not malformed
		# at all -- it is simply older than the constraint.
		versioned = placement.since is not None \
		            and placement.version_field is not None

		# ...and only where the property it reads exists, which this emitter
		# recorded when it wrote it rather than working out again (26.74).
		if versioned and placement.path not in self._emitted:
			return []

		read = f"{name}_value" if versioned else f"self.{name}"

		# A fixed-width text number: the property parses the digits and
		# raises for bytes that are not digits of its radix, which is the
		# whole of the check -- and nothing was reading it, so `zzzzzzzz` in
		# a `hex u32 x[8]` validated clean here while three backends refused
		# it. `[minimal]` goes with it, since the two questions are one
		# construct's.
		if check is Check.TEXT_NUMBER:
			lines.extend(self._minimal_check(placement, name,
			                                 f"self.{name}_digits"))
			lines.append(f"\t\t_ = self.{name}")

		enum = self.enums.get(placement.type_name or "")
		if enum is not None and enum.effective_default is ast.EnumDefault.ERROR:
			lines.extend([
				f"\t\tif not known_enum({py_name(enum.name)}, int({read})):",
				f"\t\t\traise ConstraintError("
				f"f\"{placement.path} is {{int({read})}}, not a"
				f" {enum.name}\")",
			])

		lines.extend(self._attr_checks(struct, placement, read))

		if versioned and lines:
			return [
				f"\t\t# {placement.path} arrives in version {placement.since}."
				" A message older",
				"\t\t# than that does not carry it, and a field that is not"
				" there is",
				"\t\t# not a field that is wrong.",
				"\t\ttry:",
				f"\t\t\t{name}_value = self.{name}",
				"\t\texcept VersionError:",
				f"\t\t\t{name}_value = None",
				f"\t\tif {name}_value is not None:",
				*[f"\t{line}" for line in lines],
			]

		return lines

	def _reserved_check(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""Reserved bytes hold their pattern, however many of them there are.

		A run the *message* sizes -- the padding after a variable-length
		member -- reached the scalar branch and read a static offset it has
		not got, which crashed the compiler rather than diagnosing anything.
		"""
		policy = _reserved_policy(placement.attrs)
		scalar = placement.scalar
		if policy not in ("must_be_zero", "must_be_one") or scalar is None:
			return []
		if scalar.is_bit_packed or scalar.bits % BITS_PER_BYTE != 0:
			return []

		start = self._offset_expression(struct, placement)
		if start is None:
			return [f"\t\t# {placement.path}: this backend cannot resolve"
			        " where the reserved bytes are."]

		width = scalar.bits // BITS_PER_BYTE
		pad   = pad_alignment(placement)
		if pad is not None and placement.offset_bits is None:
			count = f"align_up(({start}), {pad}, self._len) - ({start})"
		elif placement.array_count is not None:
			count = str(placement.array_count * width)
		elif data_sized(placement):
			length = self._length_expression(struct, placement)
			if length is None:
				return [f"\t\t# {placement.path}: this backend cannot resolve"
				        " how many reserved bytes there are."]
			count = length
		else:
			count = str(width)

		want = 0 if policy == "must_be_zero" else 0xFF
		return [
			"\t\tself._check()",
			f"\t\tat, n = self._at + ({start}), ({count})",
			f"\t\tif any(b != {want} for b in self._msg.buffer[at:at + n]):",
			f"\t\t\traise ConstraintError("
			f"\"{placement.path} is reserved [{policy}]\")",
		]

	def _array_check(self, struct: ResolvedStruct, placement: Placement,
			scalar: ScalarType | None, name: str) -> list[str]:
		if scalar is None:
			return []

		if placement.kind == "reserved":
			if scalar.bits != BITS_PER_BYTE:
				return []
			return self._reserved_check(struct, placement)

		# utf16's code unit is two bytes, so a `u16` run is validated over a
		# byte slice of the message rather than the parsed values (0044). A
		# plain `u16` run carries no encoding and emits nothing.
		if scalar.bits != BITS_PER_BYTE:
			return self._utf16_check(struct, placement, scalar)

		if placement.sized_by is not None:
			return []

		count = placement.array_count or 0
		lines: list[str] = []

		for attr in placement.attrs:
			if attr.name == "encoding":
				named = getattr(attr.value, "name", None)
				if named in ("ascii", "utf8"):
					lines.extend([
						f"\t\tif not {named}_valid(self.{name}):",
						f"\t\t\traise ConstraintError("
						f"\"{placement.path} is not {named}\")",
					])
			if attr.name == "nul_terminated":
				lines.extend([
					f"\t\tif self.{name}_len >= {count}:",
					f"\t\t\traise ConstraintError("
					f"\"{placement.path} has no terminator\")",
				])
		return lines

	def _utf16_check(self, struct: ResolvedStruct, placement: Placement,
			scalar: ScalarType) -> list[str]:
		"""`[encoding = utf16le|be]` over a `u16` run's bytes.

		The same message slice and length arithmetic `_reserved_check` uses:
		the byte length is the code unit count times two -- a literal for a
		fixed run, the shared length expression for a message-sized one
		(0044) -- read from a static offset the encoding check requires.
		"""
		encoding = next((attr for attr in placement.attrs
		                 if attr.name == "encoding"), None)
		if encoding is None or placement.offset_bits is None:
			return []
		named = getattr(encoding.value, "name", None)
		if named not in ("utf16le", "utf16be"):
			return []
		start = self._offset_expression(struct, placement)
		if start is None:
			return []

		width = scalar.bits // BITS_PER_BYTE
		if placement.array_count is not None:
			count = str(placement.array_count * width)
		elif data_sized(placement):
			length = self._length_expression(struct, placement)
			if length is None:
				return []
			count = length
		else:
			return []
		return [
			"\t\tself._check()",
			f"\t\tat, n = self._at + ({start}), ({count})",
			f"\t\tif not {named}_valid(self._msg.buffer[at:at + n]):",
			"\t\t\traise ConstraintError(",
			f"\t\t\t\t\"{placement.path} is not {named}\")",
		]

	# -- gates ---------------------------------------------------------

	def _gates(self, struct: ResolvedStruct) -> list[str]:
		regions = [entry.placement for entry in struct.entries
		           if entry.placement.kind == "sealed"]
		lines: list[str] = []
		for region in regions:
			lines.extend(self._gate(struct, region))
		return lines

	def _gate(self, struct: ResolvedStruct, region: Placement) -> list[str]:
		name   = py_name(local_name(struct, region))
		holder = f"_{name}_gate"
		sealed = [entry.placement for entry in struct.entries
		          if entry.placement.sealed_by == region.name
		          and entry.placement.kind == "field"]
		# Not `offset_bits is not None`: a scalar behind a variable-length
		# member *inside* the region has an offset the message decides, and
		# dropping it left the member unreachable in three backends while C
		# read it through the gate. The offset expression is the same one the
		# outer accessors use, read through the gate's view.
		inside = [entry for entry in struct.entries
		          if entry.placement.sealed_by == region.name
		          and entry.placement.kind == "field"
		          and entry.placement.scalar is not None
		          # `data_sized`, not `array_count or sized_by`: a length
		          # written as arithmetic over a field -- `fragment[length -
		          # 24]` -- has neither, so it fell through to the scalar
		          # branch and DTLS's whole payload came back as one byte.
		          # That is the fourth place `traverse.data_sized`'s docstring
		          # says the question was asked and answered differently, and
		          # it was still answering the old way (26.188).
		          and (indexed_elements(entry.placement)
		               or not _is_run(entry.placement))
		          and (entry.placement.offset_bits is not None
		               or self._offset_expression(struct, entry.placement)
		               is not None)]
		secret = [placement.path for placement in sealed
		          if any(attr.name == "secret" for attr in placement.attrs)
		          and _is_run(placement)]

		lines = [
			"",
			f"\tclass {holder}(Gate):",
			f'\t\t"""{region.path}: reachable only through a verified open.',
			"",
			"\t\tThe refusal is a run-time one. It is not the C++ guarantee,",
			"\t\twhere forging a gate does not compile -- Python has no access",
			"\t\tcontrol, and `object.__new__` will make one of these whatever",
			'\t\tthis class says. Section 14.3, as far as Python reaches."""',
		]

		for entry in inside:
			placement = entry.placement
			scalar    = placement.scalar
			assert scalar is not None
			field_name = py_name(placement.path.rsplit(".", 1)[-1])

			if any(attr.name == "secret" for attr in placement.attrs):
				lines.extend(["",
				              f"\t\t# {field_name} is [secret]: no accessor is",
				              "\t\t# generated for it at all (section 14.6)."])
				continue

			through = self._offset_expression(struct, placement)
			at = (None if placement.offset_bits is not None
			      else through)

			# A run of values wider than a byte, inside the gate. It has no
			# slice for the reason it has none outside one -- the bytes are
			# not the values -- so it is the count and the indexed getter,
			# read through the gate's own view.
			if placement.array_count is not None or placement.sized_by is not None:
				width  = scalar.bits // BITS_PER_BYTE
				length = (str(placement.array_count * width)
				          if placement.array_count is not None
				          else self._length_expression(struct, placement))
				start  = through if through is not None else "0"
				if length is None:
					lines.extend(["",
					              f"\t\t# {placement.path}: this backend cannot"
					              " resolve its length."])
					continue

				# Everything read inside the gate is read through its view,
				# including the field that says how long the run is: it is
				# plaintext at the same offsets, which is what makes reading
				# it here not a reference to transform output (13.3).
				def through_gate(text: str) -> str:
					return text.replace("self.", "self._view.")

				load = self._load(placement, scalar,
				                  f"({start}) + index * {width}")
				lines.extend([
					"", "\t\t@property",
					f"\t\tdef {field_name}_count(self) -> int:",
					f"\t\t\treturn min(({through_gate(length)}),",
					f"\t\t\t\tmax(0, self._view._len - ({through_gate(start)})))"
					f" // {width}",
					"", f"\t\tdef {field_name}(self, index: int) -> int:",
					f"\t\t\tif not 0 <= index < self.{field_name}_count:",
					f'\t\t\t\traise IndexError(f"{placement.path}[{{index}}]")',
					f"\t\t\treturn {through_gate(load)}",
				])
				continue

			lines.extend([
				"", "\t\t@property",
				f"\t\tdef {field_name}(self) -> {self._hint(placement)}:",
				f"\t\t\treturn {self._load(placement, scalar, at).replace('self.', 'self._view.')}",
			])

		for path in secret:
			lines.extend(["",
			              f"\t\t# {path} is [secret]: no accessor is generated",
			              "\t\t# for it at all (section 14.6)."])

		if not inside and not secret:
			lines.append("\t\tpass")

		lines.extend([
			"",
			f"\tdef open_{name}(self, verified: bool) -> \"{py_name(struct.name)}"
			f".{holder}\":",
			f'\t\t"""Hand out the sealed interior, and only if `verified`."""',
			f"\t\treturn open_gate(type(self).{holder}, self, verified)"
			f"  # type: ignore[return-value]",
		])
		return lines


def _reserved_policy(attrs: tuple[ast.Attr, ...]) -> str | None:
	for attr in attrs:
		if attr.name in ("must_be_zero", "must_be_one"):
			return str(attr.name)
		if attr.name in ("preserve", "unknown"):
			return None
	return "must_be_zero"
