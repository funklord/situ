"""Generate C11 accessors from a resolved schema.

The generated API exposes exactly the operations the capability vector supports
(project.md section 4). Where an operation is refused, the header says why and
what would have to change -- a user grepping for a missing setter has to find
the explanation in the header itself, not only in compiler output (section
20.2).

Principles that shape every line emitted here:

- **Views are values.** A view is a small struct passed by value. No allocation,
  no lifetime management.
- **Field access is a constant offset from the view base.** Accessors are
  `static inline` so they compile to `base + K`.
- **Errors are return codes.** Never errno, never longjmp.
- **Getters for ValueConverted fields return by value only.** A pointer into a
  byte-swapped field is a bug waiting to happen, so none is offered.
- **Nothing allocates, recurses, or uses a VLA.** Ever.
"""

from __future__ import annotations

import re

from math import lcm

from dataclasses import dataclass, field

from situc import ast
from situc.capability import Axis
from situc.codegen.doc import extractable
from situc.codegen.c.names import (
	c_name, check_collisions, ident, macro)
from situc.diagnostics import Diagnostic
from situc.layout import (
	BITS_PER_BYTE, IndexTable, Placement, TlvGrammar, ValueRule,
)
from situc.names import over_fields, render_delimiter
from situc.propagate import Resolved
from situc.invariant import derived as derived_by
from situc.invariant import expression as invariant_expression
from situc.traverse import (
	Check, arm_members, arm_of, classify_check, containment_order,
	covered_run, offset_plan, region_extent,
	decode_counts_bits, decodes_here,
	declares_its_own_length,
	decode_bound,
	extent_parts, frameable,
	has_computable_extent, is_run, matched_values, obligation, obligations,
	own_members,
)
from situc.resolve import ResolvedSchema, ResolvedStruct
from situc.unparse import expr_to_source as unparse_expr
from situc.types import ScalarKind, ScalarType, lookup

WORD_WIDTHS = (8, 16, 32, 64)


@dataclass
class Generated:
	"""The two files a schema compiles to, and what it had to say about them."""

	header: str
	source: str
	basename: str
	warnings: list[Diagnostic] = field(default_factory=list)

	def files(self) -> dict[str, str]:
		return {f"{self.basename}.h": self.header, f"{self.basename}.c": self.source}


def generate(schema: ast.Schema, resolved: ResolvedSchema, basename: str,
		prefix: str = "situ", materialize: bool = False) -> Generated:
	# Before anything is emitted: two constructs that flatten to one C
	# identifier would otherwise surface as a redefinition error in generated
	# code, naming a function nobody wrote.
	warnings = check_collisions(resolved, prefix, [
		*(("struct", decl.name, decl.span) for decl in schema.structs()),
		*(("enum", decl.name, decl.span) for decl in schema.enums()),
	])

	emitter = Emitter(schema, resolved, basename, prefix, materialize)
	return Generated(
		header   = emitter.header(),
		source   = emitter.source(),
		basename = basename,
		warnings = warnings,
	)


class Emitter:
	def __init__(self, schema: ast.Schema, resolved: ResolvedSchema,
			basename: str, prefix: str, materialize: bool = False) -> None:
		self.schema   = schema
		self.resolved = resolved
		self.basename = basename
		self.prefix   = prefix
		#: Emit the second accessor family (decision 0022). The consumer's
		#: choice rather than the schema's: an embedded receiver and a desktop
		#: inspector read the same bytes and want opposite trade-offs, and a
		#: schema that picked would put a deployment decision in the file that
		#: defines the wire contract.
		self.materialize = materialize
		self.structs  = {decl.name: decl for decl in schema.structs()}
		self.enums    = {decl.name: decl for decl in schema.enums()}
		self.markers  = {decl.name: decl for decl in schema.markers()}
		self.codecs   = {decl.name: decl for decl in schema.codecs()}

	# -- header ---------------------------------------------------------

	def header(self) -> str:
		guard = macro(self.prefix, self.basename, "h")
		lines = [
			*self._banner(),
			f"#ifndef {guard}",
			f"#define {guard}",
			"",
			'#include "situ.h"',
			"",
			"#ifdef __cplusplus",
			'extern "C" {',
			"#endif",
		]

		from situc.codegen.c.derived import declarations
		lines.extend(declarations(self.schema, self.prefix))

		for decl in self.schema.enums():
			lines.extend(self._enum(decl))

		# Containment order, not declaration order: a sub-view accessor names
		# the nested struct's SIZE_FIXED macro and an indexed region calls its
		# element's `extent`, both of which are `static inline` and have to be
		# above the call. This trusted the solver's insertion order until the
		# first schema whose indexed element was declared after its container
		# emitted a header that did not compile -- an element is not a layout
		# dependency, so the solver had no reason to place it first.
		for name in containment_order(self.resolved.structs,
		                              list(self.resolved.structs)):
			lines.extend(self._struct_header(self.resolved.structs[name]))

		lines.extend([
			"",
			"#ifdef __cplusplus",
			"}",
			"#endif",
			"",
			f"#endif /* {guard} */",
		])
		return "\n".join(extractable(lines)) + "\n"

	def _banner(self) -> list[str]:
		return [
			f"/* Generated by situc from {self.basename}.situ -- do not edit.",
			" *",
			" * The operations below are exactly the ones this schema's capability",
			" * vectors support. Where one is missing, a comment says why and what",
			" * would have to change; see the committed capability map for the full",
			" * picture.",
			" */",
			"",
		]

	def _enum(self, decl: ast.EnumDecl) -> list[str]:
		backing = decl.backing.scalar
		assert backing is not None
		values = self.resolved.layout.env.enums[decl.name]

		lines = [
			"",
			f"/* enum {decl.name} : {decl.backing.name}"
			f" -- unknown values are {decl.effective_default.value} */",
			f"typedef enum {ident(self.prefix, decl.name)} {{",
		]
		for member in decl.members:
			lines.append(f"\t{macro(self.prefix, decl.name, member.name)}"
			             f" = {values[member.name]},")
		lines.append(f"}} {ident(self.prefix, decl.name)}_t;")
		lines.extend(self._enum_is_known(decl))
		return lines

	def _enum_is_known(self, decl: ast.EnumDecl) -> list[str]:
		"""Whether a value names a member.

		Section 8.7 makes `default = error` the default and says unknown values
		are rejected on parse. Nothing enforced that: the backends emitted the
		rule as a comment and validated nothing, so a field declared to admit
		seven protocol numbers accepted all 256.

		Emitted for both defaults, because a `pass` schema may still want to
		ask -- what changes is whether `validate` calls it.
		"""
		name = ident(self.prefix, decl.name)
		return [
			"",
			f"/* Whether a value names a member of {decl.name}"
			f" (section 8.7). */",
			f"static inline int {name}_is_known({name}_t value)",
			"{",
			"	switch (value) {",
			*[f"	case {macro(self.prefix, decl.name, member.name)}:"
			  for member in decl.members],
			"		return 1;",
			"	default:",
			"		return 0;",
			"	}",
			"}",
		]

	def _struct_header(self, struct: ResolvedStruct) -> list[str]:
		layout = struct.layout

		# A register is a bus transaction rather than bytes in a buffer, so it
		# gets an entirely different API (section 15.1). Everything before this
		# point -- the solver, the lattice, the map -- treated it as a struct,
		# which is the whole reason one lattice answers both.
		if layout.register is not None:
			from situc.codegen.c.mmio import RegisterEmitter
			return RegisterEmitter(self.resolved, self.prefix).register(struct)

		lines = ["", f"/* ---- struct {struct.name} ---- */", ""]

		if not layout.is_byte_sized:
			lines.append(f"/* {struct.name} is {layout.size_bits} bits, not a whole")
			lines.append(" * number of bytes, so no accessors are generated for it. */")
			return lines

		lines.extend(self._size_constants(struct))
		lines.extend(self._tag_constants(struct))
		lines.extend(self._view_acquisition(struct))

		for entry in struct.entries:
			lines.extend(self._field(struct, entry))

		# After the members, not before: it sums their `_span` functions, and
		# the C preprocessor is not a scope. Emitting it beside the size
		# constants -- where a reader would look for it -- put every call
		# ahead of its declaration.
		lines.extend(self._struct_extent(struct))
		lines.extend(self._required(struct))
		lines.extend(self._offsets(struct))
		lines.extend(self._shifting_setters(struct))
		lines.extend(self._covered_setters(struct))
		lines.extend(self._invariants(struct))
		lines.extend(self._validate_decl(struct))
		return lines

	# -- invariants (open question 3) -----------------------------------

	def _invariants(self, struct: ResolvedStruct) -> list[str]:
		"""A derived field, and the one thing allowed to write it.

		The lattice has already refused the plain setter -- `mutate` is
		Immutable and the header says why. What is missing without this is any
		way to make the invariant true again, which would leave a schema that
		can state a relationship and never satisfy it.

		Recompute takes the message rather than the view, as every covered
		write does: it clears a dirty bit, and the bit lives on the message.
		"""
		held = derived_by(self.schema, struct)
		if not held:
			return []

		lines: list[str] = []

		for decl in held:
			field = decl.derived.partition(".")[2]
			entry = next((e for e in struct.entries
			              if e.placement.path == f"{struct.name}.{field}"), None)
			if entry is None or entry.placement.scalar is None:
				continue

			value = invariant_expression(struct, decl.expr, self)
			if value is None:
				lines.extend([
					"",
					f"/* No {field} recompute: this build cannot evaluate",
					f" * `{unparse_expr(decl.expr)}` at run time. The refusal to",
					" * write the field directly still stands, so the invariant",
					" * cannot be broken -- only left unsatisfiable here. */",
				])
				continue

			local  = c_name(self._local(struct, entry.placement))
			scalar = entry.placement.scalar
			bit    = self._invariant_bit(struct, field)

			lines.extend([
				"",
				f"/* {decl.derived} == {unparse_expr(decl.expr)}",
				" *",
				f" * Writing anything the right side reads sets {bit}, and the",
				" * message refuses to be transmittable until this recomputes",
				" * (section 14.2, the same machinery a tag uses). */",
				f"static inline void {ident(self.prefix, struct.name, local, 'recompute')}"
				f"(situ_msg_t *msg, situ_view_t view)",
				"{",
				f"	{self._store_statement(scalar, entry.placement, 'view.base', f'({value})')}",
				f"	situ_msg_clear_dirty(msg, {bit});",
				"}",
				"",
				f"static inline int {ident(self.prefix, struct.name, local, 'is_stale')}"
				f"(const situ_msg_t *msg)",
				"{",
				f"	return (msg->dirty & {bit}) != 0u;",
				"}",
			])

		return lines

	def _invariant_bit(self, struct: ResolvedStruct, field: str) -> str:
		return macro(self.prefix, struct.name, field, "STALE")

	# -- invariant.Terms, in C ------------------------------------------
	#
	# Which expressions are evaluable is the language's answer and lives in
	# `situc.invariant`. These four are this backend's answer to how to spell
	# the ones that are.

	def literal(self, value: int) -> str:
		return f"{value}u"

	def binary(self, op: str, left: str, right: str) -> str:
		return f"({left} {op} {right})"

	def offset(self, struct: ResolvedStruct, placement: Placement) -> str | None:
		return (f"{placement.offset_bytes}u" if placement.offset_bits is not None
		        else None)

	def size(self, struct: ResolvedStruct, placement: Placement) -> str | None:
		if placement.is_fixed_size:
			return f"{placement.size_bits // BITS_PER_BYTE}u"
		return (self._length_expression(struct, placement)
		        if self._has_length(struct, placement) else None)

	def count(self, struct: ResolvedStruct, placement: Placement) -> str | None:
		return self._count_expression(struct, placement)

	# -- the cryptographic model (section 14) ---------------------------

	def _tags(self, struct: ResolvedStruct) -> list[Placement]:
		return [entry.placement for entry in struct.entries
		        if entry.placement.kind in ("tag", "checksum")]

	def _tag_bit(self, struct: ResolvedStruct, label: str) -> str:
		"""The macro naming the bit a `covered_by` entry stands for.

		`label` is what `covered_by` holds, which for an invariant is a phrase
		rather than an identifier. Pasting it straight into a macro name gave
		`SITU_S_INVARIANT TOTAL_DIRTY` -- a space inside an identifier, in a
		header offered to a C compiler.
		"""
		held = obligation(self.schema, struct, label)
		if held is None:
			return macro(self.prefix, struct.name, label, "DIRTY")
		return macro(self.prefix, struct.name, held.name, held.suffix)

	def _tag_constants(self, struct: ResolvedStruct) -> list[str]:
		"""One bit per obligation, plus the mask of all of them.

		The bits are what a covered setter ORs into the message. Ordered as
		`traverse.obligations` orders them -- tags as declared, then invariants
		-- so the numbering is stable across a rebuild and a caller may store
		one.

		All of them are defined here, before any accessor, because the C
		preprocessor is not a scope: a covered setter that ORs a bit defined
		two hundred lines below it does not compile. The invariant bits used to
		be defined beside their recompute, which is where a reader would look
		for them and where the compiler would not.
		"""
		held = obligations(self.schema, struct)
		if not held:
			return []

		lines = ["/* Dirty bits (sections 14.2 and 16.1). A covered write sets one;",
		         " * the message refuses to be transmittable until it is cleared.",
		         " * A tag goes DIRTY, a derived field goes STALE: the same bit,",
		         " * and two different things to say about it. */"]

		for one in held:
			lines.append(
				f"#define {macro(self.prefix, struct.name, one.name, one.suffix)} "
				f"{hex(1 << one.bit)}u")

		mask = (1 << len(held)) - 1
		lines.append(f"#define {macro(self.prefix, struct.name, 'DIRTY_MASK')} "
		             f"{hex(mask)}u")
		lines.append("")
		return lines

	def _tag_support(self, struct: ResolvedStruct, entry: Resolved) -> list[str]:
		"""What a tag needs beyond its bytes: its coverage, and finalize.

		The algorithm is the caller's -- a signature says what a transform does,
		never how (section 13.1) -- so what the compiler contributes is the one
		thing only it knows: which bytes are covered, and whether they are
		currently stale.
		"""
		placement = entry.placement
		local     = c_name(self._local(struct, placement))
		name      = placement.name
		covers    = ", ".join(f"`{region}`" for region in placement.tag_covers)
		spans     = self._covered_spans(struct, placement)

		lines = [
			f"/* {name} covers {covers or 'nothing'}.",
			" *",
			" * The span below is what the algorithm runs over. Write the result",
			f" * through {ident(self.prefix, struct.name, local, 'ptr')}() and then",
			f" * call {ident(self.prefix, struct.name, local, 'finalize')}(), which",
			" * clears the dirty bit. Until then the message is not transmittable. */",
		]

		if spans is None:
			lines.extend([
				f"/* No covered-span accessor for `{name}`: the regions it covers are",
				" * not contiguous in this struct, so there is no single range to",
				" * hand out. Cover a contiguous run of regions instead. */",
			])
		else:
			first, last = spans
			lines.extend([
				f"static inline situ_err_t "
				f"{ident(self.prefix, struct.name, local, 'covered')}"
				"(situ_view_t view, uint32_t *offset, uint32_t *len)",
				"{",
				f"\tuint32_t start = {first};",
				f"\tuint32_t end   = {last};",
				"",
				"\tif (end < start || !situ_in_bounds(view, start, end - start)) {",
				"\t\treturn SITU_ERR_BOUNDS;",
				"\t}",
				"",
				"\t*offset = start;",
				"\t*len    = end - start;",
				"\treturn SITU_OK;",
				"}",
			])

		lines.extend([
			f"static inline int "
			f"{ident(self.prefix, struct.name, local, 'is_dirty')}"
			"(const situ_msg_t *msg)",
			"{",
			f"\treturn (msg->dirty & {self._tag_bit(struct, name)}) != 0u;",
			"}",
			f"static inline void "
			f"{ident(self.prefix, struct.name, local, 'finalize')}(situ_msg_t *msg)",
			"{",
			f"\tsitu_msg_clear_dirty(msg, {self._tag_bit(struct, name)});",
			"}",
		])

		return lines

	def _covered_spans(self, struct: ResolvedStruct,
			tag: Placement) -> tuple[str, str] | None:
		"""The byte range a tag authenticates, as two C expressions.

		Only a contiguous run has one. Nested coverage is contiguous by
		construction and disjoint coverage of adjacent regions usually is, but
		nothing guarantees it, so a gap is reported rather than papered over
		with a range that covers bytes the tag does not.
		"""
		run = covered_run(struct, tag)
		if run is None:
			return None

		first, last = run
		return (self._base_expression(struct, first),
		        self._region_end(struct, last))

	def _region_end(self, struct: ResolvedStruct, region: Placement) -> str:
		"""Where a region stops, as a C expression.

		Taken from where the next member starts rather than by summing the
		region's own contents. A region's extent is its interior put through a
		codec's expansion, and reconstructing that in C would duplicate the
		solver -- badly, since the interior is not addressable from outside the
		gate. The member after it already knows, and if nothing follows, the
		region runs to the end of the view.
		"""
		if region.is_fixed_size and region.offset_bits is not None:
			return f"{region.offset_bytes + region.size_bits // BITS_PER_BYTE}u"

		members = self._top_level(struct)
		index   = next(i for i, held in enumerate(members)
		               if held.path == region.path)

		if index + 1 < len(members):
			return self._base_expression(struct, members[index + 1])
		return "view.limit"

	def _sealed_gate(self, struct: ResolvedStruct, entry: Resolved) -> list[str]:
		"""The stage gate of 14.3, as a type C will not let a caller around.

		The interior accessors take `<struct>_<region>_t` and nothing produces
		one but the open function below, which demands the verification result.
		A caller holding a plain `situ_view_t` cannot reach a single interior
		field -- not by convention, but because the program does not compile.

		The gate wraps the enclosing frame's view rather than a sub-view, so an
		interior field stays the same constant offset it has everywhere else and
		the gate costs nothing at run time.
		"""
		placement = entry.placement
		local     = c_name(self._local(struct, placement))
		gate      = ident(self.prefix, struct.name, local, "t")
		tags      = ", ".join(placement.covered_by) or "its tag"

		if placement.unverified_ok:
			return [
				f"/* `{placement.name}` is `[allow_unverified_read]`: the stage gate",
				" * of section 14.3 is waived, and the interior accessors below take",
				" * an ordinary view. They run on bytes nobody has authenticated.",
				" *",
				" * This is the loud spelling on purpose. Removing the attribute",
				" * makes the interior unreachable before verification, which is the",
				" * single highest-value security property in the design. */",
			]

		return [
			f"/* A verified view into `{placement.name}`.",
			" *",
			" * Every accessor for the interior takes this type, and the only thing",
			f" * that produces one is {ident(self.prefix, struct.name, local, 'open')}(),",
			f" * which will not hand one out until {tags} has verified. Parsing",
			" * attacker-controlled plaintext before authenticating it is therefore",
			" * not discouraged here; it does not compile. */",
			f"typedef struct {gate} {{",
			"\tsitu_view_t view;",
			f"}} {gate};",
			"",
			f"static inline situ_err_t {ident(self.prefix, struct.name, local, 'open')}"
			f"(situ_view_t view, int verified, {gate} *out)",
			"{",
			"\tif (!verified) {",
			"\t\treturn SITU_ERR_TAG;",
			"\t}",
			"",
			"\tout->view = view;",
			"\treturn SITU_OK;",
			"}",
		]

	def _gate_type(self, struct: ResolvedStruct, placement: Placement) -> str | None:
		"""The gated view type an interior field's accessors take, if gated."""
		if placement.sealed_by is None or placement.unverified_ok:
			return None
		return ident(self.prefix, struct.name, c_name(placement.sealed_by), "t")

	def _coverage_noun(self, struct: ResolvedStruct, placement: Placement) -> str:
		"""What goes stale when these bytes are written.

		A tag no longer matches the bytes; a derived field no longer equals
		what it is defined to equal. Both are the `auth` axis and both set a
		bit, but a header that calls an invariant "an authentication tag"
		is telling a reader something that is not so.
		"""
		kinds = {held.kind for label in placement.covered_by
		         if (held := obligation(self.schema, struct, label)) is not None}
		if kinds == {"tag"}:
			return "an authentication tag"
		if kinds == {"invariant"}:
			return "an invariant"
		return "an obligation over these bytes"

	def _covered_setters(self, struct: ResolvedStruct) -> list[str]:
		"""Setters that mark an obligation dirty, for the fields under one.

		These take the message, as the shifting setters do and for the same
		reason: the cost of the write shows up in the signature rather than in a
		comment somebody may not read. A field under a tag cannot be written
		without acknowledging that the tag now has to be recomputed.
		"""
		# Scalars only. An array's bytes are reached through its pointer
		# accessor, and a setter that wrote one element of it would be a worse
		# API than the pointer plus an explicit mark.
		covered = [entry for entry in struct.entries
		           if entry.placement.covered_by
		           and entry.placement.scalar is not None
		           and entry.placement.kind == "field"
		           and entry.placement.array_count is None
		           and entry.placement.sized_by is None
		           and entry.vector.get(Axis.MUTATE).base in ("InPlaceFixed",
		                                                      "InPlaceSlack")]
		if not covered:
			return []

		noun  = self._coverage_noun(struct, covered[0].placement)
		lines = ["", f"/* Writing any of these leaves {noun} stale, so each one",
		         " * takes the message and marks the bit. The message then refuses",
		         " * to be transmittable until that is discharged -- a tag by being",
		         " * recomputed and finalized (section 14.2), a derived field by",
		         " * its recompute (section 16.1). */"]

		for entry in covered:
			placement = entry.placement
			scalar    = placement.scalar
			assert scalar is not None

			local = c_name(self._local(struct, placement))
			ctype = self._field_ctype(placement)
			mask  = " | ".join(self._tag_bit(struct, tag)
			                   for tag in placement.covered_by)
			gate  = self._gate_type(struct, placement)
			view  = f"{gate} gate" if gate else "situ_view_t view"
			base  = self._value_base(struct, placement, gated=gate is not None)

			lines.extend([
				f"static inline void "
				f"{ident(self.prefix, struct.name, local, 'set')}"
				f"(situ_msg_t *msg, {view}, {ctype} value)",
				"{",
				f"\t{self._store_statement(scalar, placement, base, 'value', offset=self._value_offset(placement))}",
				f"\tsitu_msg_mark_dirty(msg, {mask});",
				"}",
			])

		return lines

	def _covered_pointer_note(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""A pointer into covered bytes cannot mark anything by itself.

		The scalar setters take the message and do it for the caller; a pointer
		hands over the bytes and leaves the obligation with them, so the header
		says which bit to set rather than leaving it to be inferred.
		"""
		if not placement.covered_by:
			return []

		mask = " | ".join(self._tag_bit(struct, tag) for tag in placement.covered_by)
		return [
			f"/* COVERAGE: writing through this pointer leaves "
			f"{', '.join(placement.covered_by)} stale.",
			f" * Call situ_msg_mark_dirty(msg, {mask}) after doing so; there is no",
			" * setter that can do it here, because a pointer write is not a call. */",
		]

	def _secret_note(self, struct: ResolvedStruct, entry: Resolved) -> list[str]:
		"""No debug accessor, and a way to erase it (section 14.6)."""
		placement = entry.placement
		local     = c_name(self._local(struct, placement))
		gate      = self._gate_type(struct, placement)
		taken     = f"{gate} gate" if gate else "situ_view_t view"
		held      = "gate.view" if gate else "view"
		length    = self._length_expression(struct, placement, held)

		return [
			f"/* `{placement.name}` is `[secret]`. No debug or format accessor is",
			" * generated for it at all: that is the most common way key material",
			" * reaches a log. The accessors that do exist branch on nothing they",
			" * read, so the access pattern does not depend on the value.",
			" *",
			" * The erase goes through a volatile pointer, so it is observable",
			" * behaviour the compiler may not drop as a dead store. */",
			f"static inline void "
			f"{ident(self.prefix, struct.name, local, 'zeroize')}({taken})",
			"{",
			f"\tsitu_zeroize({held}.base + "
			f"{self._base_expression(struct, placement, gated=gate is not None)}, "
			f"{length});",
			"}",
		]

	def _size_constants(self, struct: ResolvedStruct) -> list[str]:
		"""So callers can size static buffers without running the compiler.

		SIZE_FIXED is emitted only when there is one. A frame has a range, and
		pretending otherwise would hand a caller a number that is wrong for
		every message but the shortest.
		"""
		layout = struct.layout
		name   = struct.name
		lines  = []

		if layout.is_fixed_size:
			lines.append(f"#define {macro(self.prefix, name, 'SIZE_FIXED')} "
			             f"{layout.size_bytes}u")

		lines.append(f"#define {macro(self.prefix, name, 'SIZE_MIN')}   "
		             f"{layout.size_bytes}u")

		if layout.size_max_bytes is not None:
			lines.append(f"#define {macro(self.prefix, name, 'SIZE_MAX')}   "
			             f"{layout.size_max_bytes}u")
		else:
			lines.extend([
				f"/* No {macro(self.prefix, name, 'SIZE_MAX')}: nothing in the "
				"schema bounds this",
				" * struct's length. Give the driving length field a `[max = N]`",
				" * to make it statically allocatable. */",
			])

		lines.append("")
		return lines

	def _view_acquisition(self, struct: ResolvedStruct) -> list[str]:
		"""One bounds check at the frame boundary (section 12.2).

		A fixed struct knows its own extent. A frame does not: how many bytes it
		occupies depends on the data, so the caller supplies what they have and
		the check is against that.
		"""
		layout = struct.layout
		name   = struct.name

		if layout.is_fixed_size:
			return [
				*self._invalidation_note(struct),
				"/* Acquire a view. This is the one bounds check; the field",
				" * accessors below are constant offsets from the view base. */",
				f"static inline situ_err_t {ident(self.prefix, name, 'view')}"
				"(const situ_msg_t *msg, uint32_t offset, situ_view_t *out)",
				"{",
				f"\treturn situ_view_at(msg, offset, "
				f"{macro(self.prefix, name, 'SIZE_FIXED')}, out);",
				"}",
			]

		lines = [
			*self._invalidation_note(struct),
			"/* Acquire a view. This struct is a frame: its extent depends on the",
			" * data, so the caller supplies the length they have and the bounds",
			" * check is made against that. The fields at a static offset are then",
			" * constant offsets from the base; the rest resolve through their own",
			" * offset functions below. */",
			f"static inline situ_err_t {ident(self.prefix, name, 'view')}"
			"(const situ_msg_t *msg, uint32_t offset, uint32_t length,",
			"\t\tsitu_view_t *out)",
			"{",
		]

		# A minimum of zero would compile to `length < 0u`, which no compiler
		# should have to be told is always false. The check is omitted rather
		# than emitted and suppressed: a check that cannot fire is noise.
		if layout.size_bytes > 0:
			lines.extend([
				f"\tif (length < {macro(self.prefix, name, 'SIZE_MIN')}) {{",
				"\t\treturn SITU_ERR_BOUNDS;",
				"\t}",
				"",
			])
		else:
			lines.append("\t/* No minimum: every member of this struct may be empty. */")

		lines.extend(["\treturn situ_view_at(msg, offset, length, out);", "}"])
		return lines

	def _drivers(self, struct: ResolvedStruct) -> list[str]:
		"""Fields whose value decides where later members start."""
		return sorted({
			placement.sized_by
			for placement in self._top_level(struct)
			if placement.sized_by and placement.sized_by != "remaining"
		})

	def _invalidation_note(self, struct: ResolvedStruct) -> list[str]:
		"""Document, per view type, exactly what invalidates it.

		Section 12.3 asks for this by name. The C type system cannot enforce
		view invalidation, so the generated header has to say what does it --
		otherwise the only record of the rule is in the compiler's head.
		"""
		drivers = self._drivers(struct)

		if not drivers:
			return [
				f"/* INVALIDATION: nothing invalidates a {struct.name} view.",
				" * Every member has a fixed size, so no write can move another.",
				" */",
			]

		listed = ", ".join(f"`{name}`" for name in drivers)
		return [
			f"/* INVALIDATION: a {struct.name} view, and every view derived from",
			f" * it, is invalidated by writing {listed} -- those decide where the",
			" * members after them start. Use the setters at the end of this",
			" * struct's section, which bump the message generation; a stale view",
			" * is then caught on use in a SITU_CHECKED build.",
			" *",
			" * Re-acquire the view after any such write.",
			" */",
		]

	def _shifting_setters(self, struct: ResolvedStruct) -> list[str]:
		"""Setters for fields that drive a length in this struct.

		Writing one of these moves every member after the region it sizes, so
		the message's generation has to be bumped: that is the only thing
		standing between a caller and a stale view (section 12.3). The setter
		therefore takes the message, which also makes the cost visible in the
		signature rather than only in a comment.
		"""
		drivers = self._drivers(struct)
		if not drivers:
			return []

		lines = ["", "/* Writing any of these changes where later members start, so each",
		         " * one bumps the message generation and invalidates outstanding",
		         " * views. In a SITU_CHECKED build a stale view is then caught on",
		         " * use; in a release build the check compiles out. */"]

		for path in drivers:
			target = self.resolved.find(f"{struct.name}.{path}")
			if target is None or target.placement.scalar is None:
				continue

			local = c_name(path)

			# A text number has no fixed bytes to store into. Writing 4096
			# where 12 was takes two more digits than the field holds, so the
			# write moves everything after it -- which is a re-encode of the
			# frame, not a store. Emitting the ordinary setter here wrote four
			# raw bytes over the digits, which is not even the wrong number.
			if target.placement.radix is not None:
				lines.extend([
					f"/* No {ident(self.prefix, struct.name, local, 'set')}():"
					f" `{path}` is written as digits, and a",
					" * longer number needs more of them. Rewrite the frame"
					" rather than the",
					" * field -- there is no store here that leaves the bytes"
					" after it where",
					" * they were. */",
				])
				continue

			ctype = self._field_ctype(target.placement)
			store = self._store_statement(
				target.placement.scalar, target.placement,
				self._value_base(struct, target.placement), "value",
				offset=self._value_offset(target.placement))

			lines.extend([
				f"static inline void "
				f"{ident(self.prefix, struct.name, local, 'set')}"
				f"(situ_msg_t *msg, situ_view_t view, {ctype} value)",
				"{",
				f"\t{store}",
				"\tsitu_msg_touch(msg);",
				"}",
			])

		return lines

	# -- one field ------------------------------------------------------

	def _field(self, struct: ResolvedStruct, entry: Resolved) -> list[str]:
		placement = entry.placement

		# An element entry describes every element of an array at once, so it
		# has no accessor of its own: the array field emits an indexed one.
		if placement.kind == "element":
			return []

		# A nested struct's members are emitted under their own struct, so only
		# the aggregate itself gets an accessor here. The interior of a sealed
		# region is the exception: its accessors take the gated view type, which
		# belongs to this struct, and the region's own type is a codec rather
		# than something with a section of its own. Checked before the reserved
		# case, or a nested reserved region would be noted twice.
		# An arm's members are nested by path and are this struct's to emit:
		# an arm is not a type, so there is no other section they could go in.
		# Each is guarded by the discriminant that selects its arm.
		guard = self._arm_guard(struct, placement)
		if guard is not None:
			return self._arm_member(struct, placement, *guard)

		nested = "." in placement.path[len(struct.name) + 1 :]
		if nested and placement.sealed_by is None:
			return []

		if placement.kind == "reserved":
			return self._reserved_note(placement)

		lines = ["", *self._field_comment(entry)]
		lines.extend(self._scale_macros(struct, placement))

		# A located member is reached from the message rather than from the
		# frame, so none of the offset machinery below applies to it: it has no
		# place in the offset chain and nothing follows it.
		if placement.located is not None:
			lines.extend(self._located_accessor(struct, placement))
			return lines

		if placement.kind in ("tag", "checksum"):
			# A tag lands after everything it covers, so its own offset is
			# usually dynamic and has to be resolved before either its bytes or
			# its covered span can be reached.
			if placement.offset_bits is None:
				lines.extend(self._offset_function(struct, placement))
			lines.extend(self._array(struct, entry))
			lines.extend(self._tag_support(struct, entry))
			return lines

		if placement.kind == "sealed":
			lines.extend(self._sealed_gate(struct, entry))
			return lines

		if placement.kind == "authenticated":
			lines.extend(self._authenticated_note(struct, entry))
			return lines

		if placement.kind == "marker":
			lines.extend(self._marker(struct, placement))
			return lines

		if placement.kind == "variant":
			lines.extend(self._variant_note(struct, placement))
			return lines

		if placement.kind == "tlv":
			lines.extend(self._tlv_region(struct, placement))
			return lines

		if placement.kind == "indexed":
			lines.extend(self._indexed_region(struct, placement))
			return lines

		if placement.kind == "opaque":
			lines.extend(self._region_note(struct, entry))
			return lines

		# A dynamically placed member needs its offset worked out at runtime
		# before anything can read it -- and where that cannot be worked out,
		# nothing else about the member can either. Emitting the accessors
		# anyway named an offset function that was never defined, which C
		# happens to refuse; a language that resolved it later would have
		# taken the wrong bytes instead.
		if placement.offset_bits is None:
			blocker = self._offset_blocker(struct, placement)
			if blocker is not None:
				return lines + self._unresolvable_offset(placement, blocker)
			lines.extend(self._offset_function(struct, placement))

		# After the offset block, not before it. A varint at a dynamic offset
		# needs its own `_offset` emitted first -- the accessors below read
		# from it, and returning early named a function that was never
		# defined. The second varint of a pair is exactly that shape.
		if placement.varint is not None:
			lines.extend(self._varint_field(struct, placement))
			return lines

		if placement.radix is not None and placement.delimiter is None:
			lines.extend(self._fixed_text_number(struct, placement))
			return lines

		# A text number with a width rather than a delimiter (8.6.2): three
		# digits, padded, and no scan at all.
		if placement.radix is not None and placement.delimiter is None:
			lines.extend(self._fixed_text_number(struct, placement))
			return lines

		if placement.repeat_while is not None:
			lines.extend(self._repeat_while(struct, entry))
			lines.extend(self._run_index(struct, placement))
			return lines

		if self._is_record_run(placement):
			lines.extend(self._record_run(struct, placement))
			lines.extend(self._run_index(struct, placement))
			return lines

		if placement.delimiter is not None:
			lines.extend(self._delimited(struct, placement))
			if placement.radix is not None:
				lines.extend(self._text_number(struct, placement))
				lines.extend(self._text_value_helper(struct, placement))
			elif placement.codec is not None:
				# The bytes are the transform's output. A token comparison
				# over them would compare ciphertext, or stuffed text, to a
				# literal somebody wrote in the clear -- and the pointer is
				# not the value either, which is the one thing a caller has
				# to be told here (section 13.6).
				lines.extend(self._coded_delimited_note(struct, placement))
			else:
				lines.extend(self._token_compare(struct, placement))
				lines.extend(self._covered_pointer_note(struct, placement))
			return lines

		# A coded region with no delimiter. It fell through every branch
		# below and got a comment header and nothing else -- so the encoded
		# bytes of one were unreachable, which is a strange thing for a
		# treat-as-bytes region. The delimited case has had a pointer all
		# along, because the scan path emits one.
		if placement.kind == "coded" and placement.delimiter is None:
			lines.extend(self._coded_region(struct, placement))
			return lines

		if placement.type_name in self.structs:
			if self._is_array(placement):
				lines.extend(self._element_view(struct, placement))
			else:
				lines.extend(self._sub_view(struct, placement))
			return lines

		if placement.array_count is not None or placement.sized_by is not None:
			lines.extend(self._array(struct, entry))
			lines.extend(self._covered_pointer_note(struct, placement))
			if _has_attr(placement.attrs, "secret"):
				lines.extend(self._secret_note(struct, entry))
			return lines

		lines.extend(self._scalar_get(struct, entry))
		lines.extend(self._scalar_set(struct, entry))
		if _has_attr(placement.attrs, "secret"):
			lines.extend(self._secret_note(struct, entry))
		return lines

	def _authenticated_note(self, struct: ResolvedStruct,
			entry: Resolved) -> list[str]:
		"""A region with no accessor of its own, and a warning that matters.

		Its members keep their own accessors at their own offsets -- the block
		moves nothing. What it changes is the cost of writing them, and where a
		member is reached through a sub-view of another type that cost is not
		visible in that type's accessors, so it is said here.
		"""
		placement = entry.placement
		tags      = ", ".join(f"`{tag}`" for tag in placement.covered_by)

		return [
			f"/* COVERAGE: the bytes of `{placement.name}` are authenticated by",
			f" * {tags or 'no tag'}. Writing any of them through this struct's",
			" * coverage-aware setters marks the tag dirty; writing them through a",
			" * sub-view of another struct type does not, because that type has no",
			" * coverage of its own. Prefer the setters at the end of this",
			" * struct's section. */",
		]

	def _scale_macros(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""Constants a caller needs to do the arithmetic situ will not do for it.

		Emitting `_SCALE` rather than a conversion function is the whole
		position on fixed point: the scale is exact and belongs in the header,
		while the conversion needs a type situ cannot choose for an embedded
		target.
		"""
		scalar = placement.scalar
		if scalar is None or placement.kind != "field":
			return []
		if "." in placement.path[len(struct.name) + 1:]:
			return []

		local = c_name(self._local(struct, placement))

		if scalar.is_fixed_point:
			return [
				f"#define {macro(self.prefix, struct.name, local, 'FRAC_BITS')} "
				f"{scalar.frac_bits}u",
				f"#define {macro(self.prefix, struct.name, local, 'SCALE')} "
				f"{scalar.scale}",
			]
		if scalar.is_bcd:
			return [
				f"#define {macro(self.prefix, struct.name, local, 'DIGITS')} "
				f"{scalar.digits}u",
				f"#define {macro(self.prefix, struct.name, local, 'MAX')} "
				f"{scalar.decimal_max}u",
			]
		return []

	def _field_comment(self, entry: Resolved) -> list[str]:
		placement = entry.placement
		vector    = entry.vector
		offset    = vector.get(Axis.OFFSET).render()
		axes      = " ".join(
			f"{axis.value}={vector.get(axis).render()}"
			for axis in (Axis.SIZE, Axis.ALIGN, Axis.REPR, Axis.ATOMIC, Axis.MUTATE))

		lines = [
			f"/* {placement.path} : {placement.type_name}  at {offset}",
			f" * {axes}",
		]
		lines.extend(self._scale_note(placement))
		lines.append(" */")
		return lines

	def _scale_note(self, placement: Placement) -> list[str]:
		"""What the stored integer means, for a type where that needs saying."""
		scalar = placement.scalar
		if scalar is None:
			return []

		if scalar.is_fixed_point:
			return [" *",
			        " * The accessors carry the stored integer; the value it"
			        " means is that",
			        f" * divided by {scalar.scale}"
			        f" -- {scalar.frac_bits} fractional bits,"
			        f" {scalar.int_bits} integer.",
			        " * No floating point is generated: the target may have"
			        " none, and the",
			        " * scale is exact."]

		if scalar.is_bcd:
			return [" *",
			        f" * {scalar.digits} packed decimal digits: the accessors"
			        f" decode and encode,",
			        f" * and the value runs from 0 to {scalar.decimal_max}."]
		return []

	def _tlv_region(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""A run of tag-length-value items, walked as the schema describes them.

		The walk is generated rather than called into: `situ.h` carries the
		primitives -- a varint read, a bounds-checked sub-view -- and the loop
		that knows what a tag decodes to and how each wire type sizes its value
		belongs to the schema. It used to be the other way round. A cursor with
		`tag >> 3` and protobuf's four wire types hardcoded sat in the runtime,
		generated code never called it, and the one test that used it
		hand-wrote the field dispatch that `known` already declares.

		Every item is found by walking from the start, which is what
		`access = Sequential` means and why the by-name accessors are O(n). The
		cursor is the honest shape of the construct.
		"""
		grammar = placement.tlv_grammar
		if grammar is None or not grammar.walkable:
			return self._unwalkable_tlv(placement)

		local = c_name(self._local(struct, placement))
		item  = ident(self.prefix, struct.name, local, "item_t")

		lines = self._tlv_item_type(struct, placement, grammar, item)
		lines.extend(self._tlv_read(struct, placement, grammar, item, local))
		lines.extend(self._tlv_cursor(struct, placement, item, local))
		lines.extend(self._tlv_by_name(struct, placement, grammar, item, local))
		return lines

	def _unwalkable_tlv(self, placement: Placement) -> list[str]:
		"""A region that does not describe how to find its own items."""
		return [
			f"/* No accessors for `{placement.name}`: the region says how its",
			" * items are tagged and not how long their values are, so a walk",
			" * has nowhere to put the second item. Give it a `value_size`",
			" * dispatch, or a `length_type` for the simple form. */",
		]

	def _tlv_tag_bytes(self, placement: Placement) -> int:
		"""How many bytes the tag varint may occupy, from its declared width.

		Derived rather than the 10 a 64-bit leb128 happens to need: a schema
		that bounds its tags at 16 bits gets a bound the walk can use, and one
		that does not is not silently held to protobuf's.
		"""
		declared = next((decl for decl in self.schema.varints()
		                 if decl.name == placement.tlv_tag_varint), None)
		bits = declared.max_bits if declared is not None else 64
		return (bits + 6) // 7

	def _tlv_item_type(self, struct: ResolvedStruct, placement: Placement,
			grammar: TlvGrammar, item: str) -> list[str]:
		"""The cursor: one item's extent, and what its tag decoded to.

		The decoded parts are members named by the schema. That is the point of
		reading the item grammar: `field` and `wire` are this schema's words for
		them, and a backend that invented its own would be describing protobuf
		rather than the region in front of it.
		"""
		parts = [f"\tuint32_t    {c_name(part.name)};"
		         f"\t/* {part.source} */" for part in grammar.tag_decode]

		return [
			f"/* One item of `{placement.name}`, and where the next one starts.",
			" *",
			" * `at` and `next` bound the item; `value_at` and `value_len` bound",
			" * its value. Both are offsets into the same view, so nothing here",
			" * outlives the bytes it describes. */",
			f"typedef struct {{",
			"\tsitu_view_t view;",
			"\tuint32_t    at;",
			"\tuint32_t    next;",
			f"\tuint64_t    tag;\t/* the raw tag, as read */",
			*parts,
			"\tuint32_t    value_at;",
			"\tuint32_t    value_len;",
			f"}} {item};",
			"",
		]

	def _tlv_read(self, struct: ResolvedStruct, placement: Placement,
			grammar: TlvGrammar, item: str, local: str) -> list[str]:
		"""Read the item at `at`: its tag, its parts, and where its value ends."""
		read     = ident(self.prefix, struct.name, local, "read")
		max_tag  = self._tlv_tag_bytes(placement)

		# The parts are named by the schema, so how wide the column has to be
		# is not knowable until here.
		assigned = ["view", "at", "tag"] + [c_name(part.name)
		                                    for part in grammar.tag_decode]
		width    = max(len(name) for name in assigned)
		stored   = [f"\tout->{name.ljust(width)} = {value};" for name, value in
		            (("view", "view"), ("at", "at"), ("tag", "tag"))]
		stored  += [f"\tout->{c_name(part.name).ljust(width)}"
		            f" = (uint32_t)({part.source});"
		            for part in grammar.tag_decode]

		lines = [
			f"/* Read the item at `at`. SITU_ERR_BOUNDS where the region ends or",
			" * an item runs past it; SITU_ERR_CONSTRAINT for a wire type this",
			" * schema does not describe. */",
			f"static inline situ_err_t {read}(situ_view_t view, uint32_t at,",
			f"\t\t{item} *out)",
			"{",
			"\tuint64_t tag  = 0;",
			"\tuint32_t used = 0u;",
			"\tuint32_t size = 0u;",
			"",
			"\tif (at >= view.limit) {",
			"\t\treturn SITU_ERR_BOUNDS;",
			"\t}",
			"",
			f"\tused = situ_varint_get(view.base + at, view.limit - at, {max_tag}u, &tag);",
			"\tif (used == 0u) {",
			"\t\treturn SITU_ERR_BOUNDS;",
			"\t}",
			"",
			*stored,
			"\tat = at + used;",
			"",
		]

		lines.extend(self._tlv_value_extent(grammar, max_tag, placement.endian))
		lines.extend([
			"",
			"\tif (size > view.limit - at) {",
			"\t\treturn SITU_ERR_BOUNDS;",
			"\t}",
			"",
			"\tout->value_at  = at;",
			"\tout->value_len = size;",
			"\tout->next      = at + size;",
			"\treturn SITU_OK;",
			"}",
			"",
		])
		return lines

	def _tlv_value_extent(self, grammar: TlvGrammar, max_tag: int,
			endian: ast.Endian | None) -> list[str]:
		"""Where the value ends, dispatched exactly as the schema dispatches it.

		The simple form has no dispatch: one `length_type` sizes every value,
		so there is nothing to switch on.
		"""
		if grammar.selector is None:
			return self._tlv_prefixed_size(grammar.length_type or "u8", "\t",
			                               endian)

		lines = [f"\tswitch (out->{c_name(grammar.selector)}) {{"]
		for rule in grammar.rules:
			if rule.label is None:
				continue
			lines.append(f"\tcase {rule.label}u:")
			lines.extend(self._tlv_one_rule(rule, max_tag, endian))

		default = next((rule for rule in grammar.rules if rule.label is None), None)
		lines.append("\tdefault:")
		if default is None or default.kind == "error":
			lines.extend([
				"\t\t/* `default: error`: a wire type this schema does not",
				"\t\t * describe, so where the value ends is not knowable. */",
				"\t\treturn SITU_ERR_CONSTRAINT;",
			])
		else:
			lines.extend(self._tlv_one_rule(default, max_tag, endian))
		lines.append("\t}")
		return lines

	def _tlv_one_rule(self, rule: ValueRule, max_tag: int,
			endian: ast.Endian | None) -> list[str]:
		"""One arm of the dispatch, sizing a value the way section 9.5 says."""
		if rule.kind == "fixed":
			return [f"\t\tsize = {rule.size}u;", "\t\tbreak;"]

		if rule.kind == "error":
			return ["\t\treturn SITU_ERR_CONSTRAINT;"]

		if rule.kind == "self_delimiting":
			# The value carries its own extent, so reading it *is* measuring
			# it: the bytes it occupies are the bytes the read consumed.
			return [
				"\t\t{",
				"\t\t\tuint64_t carried = 0;",
				f"\t\t\tused = situ_varint_get(view.base + at, view.limit - at,"
				f" {max_tag}u, &carried);",
				"\t\t\tif (used == 0u) {",
				"\t\t\t\treturn SITU_ERR_BOUNDS;",
				"\t\t\t}",
				"\t\t\tsize = used;",
				"\t\t}",
				"\t\tbreak;",
			]

		return [*self._tlv_prefixed_size(rule.length_type or "u8", "\t\t",
		                                 endian),
		        "\t\tbreak;"]

	def _tlv_prefixed_size(self, length_type: str, indent: str,
			endian: ast.Endian | None) -> list[str]:
		"""`prefixed(T)`: a length in T, then that many bytes.

		The length is read where the value would start, and the value starts
		after it -- so `at` moves twice for one item, which is the shape that
		makes a length prefix different from a fixed width.
		"""
		declared = next((decl for decl in self.schema.varints()
		                 if decl.name == length_type), None)

		if declared is not None:
			width = (declared.max_bits + 6) // 7
			return [
				f"{indent}{{",
				f"{indent}\tuint64_t length = 0;",
				f"{indent}\tused = situ_varint_get(view.base + at,"
				f" view.limit - at, {width}u, &length);",
				f"{indent}\tif (used == 0u) {{",
				f"{indent}\t\treturn SITU_ERR_BOUNDS;",
				f"{indent}\t}}",
				f"{indent}\tat = at + used;",
				f"{indent}\tif (length > (uint64_t)(view.limit - at)) {{",
				f"{indent}\t\treturn SITU_ERR_BOUNDS;",
				f"{indent}\t}}",
				f"{indent}\tsize = (uint32_t)length;",
				f"{indent}}}",
			]

		scalar = lookup(length_type)
		width  = (scalar.bits + 7) // 8 if scalar is not None else 1
		return [
			f"{indent}{{",
			f"{indent}\tif (view.limit - at < {width}u) {{",
			f"{indent}\t\treturn SITU_ERR_BOUNDS;",
			f"{indent}\t}}",
			f"{indent}\tsize = {self._fixed_length_read(width, endian)};",
			f"{indent}\tat = at + {width}u;",
			f"{indent}}}",
		]

	def _fixed_length_read(self, width: int,
			endian: ast.Endian | None) -> str:
		"""A length prefix of a fixed width, in the region's byte order."""
		suffix = {2: "16", 4: "32", 8: "64"}.get(width)
		if suffix is None:
			return "(uint32_t)view.base[at]"
		order = "be" if endian is ast.Endian.BIG else "le"
		return f"(uint32_t)situ_get_{order}{suffix}(view.base + at)"

	def _tlv_cursor(self, struct: ResolvedStruct, placement: Placement,
			item: str, local: str) -> list[str]:
		"""`first` and `next`: the region walked from its own start."""
		read  = ident(self.prefix, struct.name, local, "read")
		first = ident(self.prefix, struct.name, local, "first")
		nxt   = ident(self.prefix, struct.name, local, "next")
		count = ident(self.prefix, struct.name, local, "count")
		base  = self._base_expression(struct, placement, gated=False)

		return [
			f"/* The first item, or SITU_ERR_BOUNDS if the region is empty. */",
			f"static inline situ_err_t {first}(situ_view_t view, {item} *out)",
			"{",
			f"\treturn {read}(view, {base}, out);",
			"}",
			"",
			f"/* The item after this one. The cursor carries its own view, so",
			" * walking needs nothing the caller has to keep in step. */",
			f"static inline situ_err_t {nxt}({item} *item)",
			"{",
			f"\treturn {read}(item->view, item->next, item);",
			"}",
			"",
			f"/* How many items are present. A walk, like everything else here:",
			" * nothing in the region records a count. */",
			f"static inline uint32_t {count}(situ_view_t view)",
			"{",
			f"\t{item} item;",
			f"\tuint32_t n = 0u;",
			f"\tsitu_err_t err = {first}(view, &item);",
			"",
			"\twhile (err == SITU_OK) {",
			"\t\tn   = n + 1u;",
			f"\t\terr = {nxt}(&item);",
			"\t}",
			"\treturn n;",
			"}",
			"",
		]

	def _tlv_by_name(self, struct: ResolvedStruct, placement: Placement,
			grammar: TlvGrammar, item: str, local: str) -> list[str]:
		"""`find`, and one accessor per tag the schema names.

		The identity part is decision 0023's: which decoded part a `known` key
		matches is declared where more than one could be meant, because an
		accessor comparing the wrong part still finds an item.
		"""
		if not grammar.known:
			return []

		first = ident(self.prefix, struct.name, local, "first")
		nxt   = ident(self.prefix, struct.name, local, "next")
		find  = ident(self.prefix, struct.name, local, "find")
		keyed = (f"item->{c_name(grammar.identity)}" if grammar.identity
		         else "(uint32_t)item->tag")
		named = ("the part `%s` decodes to" % grammar.identity if grammar.identity
		         else "the raw tag")

		lines = [
			f"/* The first item whose tag is `tag`, matched against {named}",
			" * (decision 0023). O(n): the region is walked from the start,",
			" * which is what `access = Sequential` costs. */",
			f"static inline situ_err_t {find}(situ_view_t view, uint32_t tag,",
			f"\t\t{item} *item)",
			"{",
			f"\tsitu_err_t err = {first}(view, item);",
			"",
			"\twhile (err == SITU_OK) {",
			f"\t\tif ({keyed} == tag) {{",
			"\t\t\treturn SITU_OK;",
			"\t\t}",
			f"\t\terr = {nxt}(item);",
			"\t}",
			"\treturn err;",
			"}",
			"",
		]

		for known in grammar.known:
			accessor = ident(self.prefix, struct.name, local, c_name(known.name))
			described = f"tag {known.tag}"
			if known.wire is not None:
				described += f", wire type {known.wire}"
			if known.type_name is not None:
				described += f", carrying {known.type_name}"
				described += "[]" if known.repeated else ""
			lines.extend([
				f"/* `{known.name}`: {described}. */",
				f"static inline situ_err_t {accessor}(situ_view_t view, {item} *item)",
				"{",
				f"\treturn {find}(view, {known.tag}u, item);",
				"}",
				"",
			])

		return lines

	def _indexed_region(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""An offset table, then the elements it reaches (section 9.3).

		The whole point of the construct is that element N is one read of entry
		N plus a base, whatever the elements weigh -- which is why `access`
		stays Random through a region whose elements are not the same size.
		Nothing walked the table for a long time, so the header said so and
		stopped there.

		Insertion is still not an operation, and for the reason it never was:
		every offset after the insertion point would have to move.
		"""
		table = placement.index_table
		if table is None:
			return []

		local   = c_name(self._local(struct, placement))
		start   = self._base_expression(struct, placement, gated=False)
		count   = self._count_expression(struct, placement)
		width   = table.entry_bits // BITS_PER_BYTE
		element = self.resolved.structs.get(table.element or "")

		lines = self._index_table(struct, placement, table, local, start,
		                          count, width)
		lines.extend(self._index_element(struct, placement, table, local,
		                                 element))
		return lines

	def _index_table(self, struct: ResolvedStruct, placement: Placement,
			table: IndexTable, local: str, start: str, count: str,
			width: int) -> list[str]:
		"""`count` and `offset`: the table itself, before anything it reaches."""
		counter = ident(self.prefix, struct.name, local, "count")
		offset  = ident(self.prefix, struct.name, local, "offset")
		read    = self._index_entry_read(placement, width)

		return [
			f"/* `{placement.name}` is an offset table of {width}-byte entries,"
			f" then the",
			" * elements it reaches. Element N is one read of entry N plus a"
			" base,",
			" * whatever the elements weigh -- which is why `access` stays"
			" Random",
			" * through a region whose elements need not be the same size.",
			" *",
			" * Insertion is not an operation here: every offset after the",
			" * insertion point would have to move. Rebuild the region"
			" instead. */",
			f"static inline uint32_t {counter}(situ_view_t view)",
			"{",
			f"\treturn {count};",
			"}",
			"",
			f"/* The offset held in entry `index`, as written -- measured from",
			f" * {self._index_base_noun(table)}. */",
			f"static inline situ_err_t {offset}(situ_view_t view, uint32_t index,",
			"\t\tuint32_t *out)",
			"{",
			f"\tuint32_t at = {start} + index * {width}u;",
			"",
			f"\tif (index >= {counter}(view)) {{",
			"\t\treturn SITU_ERR_BOUNDS;",
			"\t}",
			f"\tif (!situ_in_bounds(view, at, {width}u)) {{",
			"\t\treturn SITU_ERR_BOUNDS;",
			"\t}",
			"",
			f"\t*out = {read};",
			"\treturn SITU_OK;",
			"}",
			"",
		]

	def _index_entry_read(self, placement: Placement, width: int) -> str:
		"""One table entry, in the region's byte order."""
		if width == 1:
			return "(uint32_t)view.base[at]"
		order = "be" if placement.endian is ast.Endian.BIG else "le"
		return f"(uint32_t)situ_get_{order}{width * 8}(view.base + at)"

	def _index_base_noun(self, table: IndexTable) -> str:
		if table.base == "message":
			return "the start of the *message*"
		if table.base == "member":
			return f"the start of `{table.base_member}`"
		return "the start of this region"

	def _index_element(self, struct: ResolvedStruct, placement: Placement,
			table: IndexTable, local: str,
			element: ResolvedStruct | None) -> list[str]:
		"""`at`: a view over the element an entry reaches.

		Emitted only where the element's extent is computable. An offset with
		no length is a position and not a frame, and handing back a view over
		bytes whose end is a guess is the kind of thing this refuses.
		"""
		if element is None:
			held = table.element or placement.type_name
			return [
				f"/* No `{local}_at`: the element type is `{held}`, which is not"
				" a struct this",
				" * build can frame -- so an entry gives where an element starts"
				" and not",
				" * how far it runs. `view.base + <offset>` is the element;"
				" what is in",
				" * it is the caller's to know. */",
			]

		extent = self._element_extent_call(element)
		if extent is None:
			return [
				f"/* No `{local}_at`: one `{element.name}` has no extent this"
				" build can",
				" * compute, so an entry gives a position and not a view."
				" The offsets",
				" * are still readable above. */",
			]

		offset = ident(self.prefix, struct.name, local, "offset")
		at     = ident(self.prefix, struct.name, local, "at")

		# A message-relative offset is not bounded by this frame, so the
		# accessor takes the message the same way a located member's does
		# (9.8): only `situ_msg_t` knows where offset zero is.
		if table.base == "message" and not element.layout.is_fixed_size:
			return [
				f"/* No `{local}_at`: `{element.name}` has no fixed size and"
				" these offsets",
				" * are measured from the message, so narrowing to one element"
				" would",
				" * mean measuring it through a view this frame cannot bound."
				" The",
				" * offsets are readable above; frame the element with"
				f" `{ident(self.prefix, element.name, 'view')}`. */",
			]

		if table.base == "message":
			return [
				f"/* A view over element `index`. Its offset is measured from"
				f" the start",
				" * of the *message*, so both are taken: the view reads the"
				" table, the",
				" * message says where zero is.",
				" *",
				" * Nothing about this frame says the element is inside the"
				" buffer, so",
				" * that is checked here on every call rather than once at the"
				" region",
				" * boundary -- which is what measuring from the message"
				" costs. */",
				f"static inline situ_err_t {at}(const situ_msg_t *msg,"
				f" situ_view_t view,",
				"\t\tuint32_t index, situ_view_t *out)",
				"{",
				"\tuint32_t found = 0u;",
				f"\tsitu_err_t err = {offset}(view, index, &found);",
				"",
				"\tif (err != SITU_OK) {",
				"\t\treturn err;",
				"\t}",
				f"\treturn situ_view_at(msg, found,"
				f" {macro(self.prefix, element.name, 'SIZE_FIXED')}, out);",
				"}",
				"",
			]

		origin = (self._index_member_base(struct, table)
		          if table.base == "member"
		          else self._base_expression(struct, placement, gated=False))

		return [
			f"/* A view over element `index`, whose offset is measured from",
			f" * {self._index_base_noun(table)}. */",
			f"static inline situ_err_t {at}(situ_view_t view, uint32_t index,",
			"\t\tsitu_view_t *out)",
			"{",
			"\tuint32_t found = 0u;",
			f"\tsitu_err_t err = {offset}(view, index, &found);",
			"\tuint32_t start;",
			"",
			"\tif (err != SITU_OK) {",
			"\t\treturn err;",
			"\t}",
			f"\tstart = {origin} + found;",
			*self._index_element_extent(element),
			"}",
			"",
		]

	def _index_element_extent(self, element: ResolvedStruct) -> list[str]:
		"""Narrow the view to one element, measuring it first where it varies.

		A fixed element is its own macro. A variable one has to be measured,
		and measuring needs a view over it -- so the sequence is: take the
		largest view the frame allows, ask the element how long it is, and
		narrow to that. Handing back the provisional view instead would give
		the caller everything to the end of the region and call it an element.
		"""
		if element.layout.is_fixed_size:
			return [f"\treturn situ_view_sub(view, start,"
			        f" {macro(self.prefix, element.name, 'SIZE_FIXED')}, out);"]

		return [
			"\t{",
			"\t\tsitu_view_t probe;",
			"",
			"\t\t/* The extent is in the element's own bytes, so it takes a",
			"\t\t * view to read and a view is what it decides. Measure over",
			"\t\t * the rest of the region, then narrow. */",
			"\t\tif (start > view.limit) {",
			"\t\t\treturn SITU_ERR_BOUNDS;",
			"\t\t}",
			"\t\tif (situ_view_sub(view, start, view.limit - start, &probe)",
			"\t\t\t\t!= SITU_OK) {",
			"\t\t\treturn SITU_ERR_BOUNDS;",
			"\t\t}",
			f"\t\treturn situ_view_sub(view, start,",
			f"\t\t\t{ident(self.prefix, element.name, 'extent')}(probe), out);",
			"\t}",
		]

	def _index_member_base(self, struct: ResolvedStruct,
			table: IndexTable) -> str:
		"""Where the member `base` names starts, within this frame."""
		found = self.resolved.find(f"{struct.name}.{table.base_member}")
		if found is None:
			return "0u"
		return self._base_expression(struct, found.placement, gated=False)

	def _region_note(self, struct: ResolvedStruct, entry: Resolved) -> list[str]:
		"""An opaque region: bytes, which is the whole of what it supports.

		A pointer and a length, which is exactly what its capability vector
		allows -- treat-as-bytes and nothing more.
		"""
		placement = entry.placement
		local     = c_name(self._local(struct, placement))
		base      = self._base_expression(struct, placement)

		return [
			"/* Treat-as-bytes, which is the whole of what an `opaque` region",
			" * supports: no interior access, and whole-region replacement only",
			" * at the same size. */",
			f"static inline uint32_t "
			f"{ident(self.prefix, struct.name, local, 'len')}(situ_view_t view)",
			"{",
			f"	return {self._length_expression(struct, placement)};",
			"}",
			f"static inline uint8_t *"
			f"{ident(self.prefix, struct.name, local, 'ptr')}(situ_view_t view)",
			"{",
			f"	return view.base + {base};",
			"}",
		]

	def _variant_note(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""A variant has no accessor of its own: exactly one arm is present.

		Its *members* do, and each asks the discriminant first. Reading one
		arm's bytes while another is present stays inside the view -- the
		extent bounds it -- and means nothing, which is the kind of wrong
		answer situ exists to refuse. `SITU_ERR_VERSION` is the same code
		`default: error` returns, because it is the same mistake seen from
		the other end.
		"""
		arms = ", ".join(f"`{name}`" for name, _ in placement.arm_sizes)
		return [
			f"/* `{placement.name}` is a variant: exactly one of"
			f"{f' {arms}' if arms else ' its arms'} is",
			" * present, and which one is in the discriminant. The variant has no",
			" * accessor of its own -- there is no one thing to hand back -- and",
			" * each arm's members are below, guarded. */",
		]

	def _arm_member(self, struct: ResolvedStruct, placement: Placement,
			test: str, arm: str) -> list[str]:
		"""One member of one variant arm, behind the test that it is present.

		The check is per access rather than once, and that is what the
		construct costs: which arm is there is a fact about the message, and
		nothing about the frame or the view records the answer. A caller who
		has already dispatched pays for the branch twice, which is the price
		of an accessor that cannot be used wrongly.
		"""
		local  = c_name(self._local(struct, placement))
		scalar = placement.scalar
		base   = self._base_expression(struct, placement)

		head = [
			"",
			f"/* {placement.path}, present when the discriminant selects"
			f" `{arm}`.",
			" *",
			" * Reading another arm's bytes as this one's stays inside the view",
			" * and means nothing, so the accessor asks first and refuses. The",
			" * code is `default: error`'s, being the same mistake from the",
			" * other end. */",
		]

		if scalar is not None and placement.array_count is None \
				and placement.sized_by is None:
			ctype = self._field_ctype(placement)
			# The placement's own base and offset, the way the ordinary
			# scalar getter reads one. Passing the member's byte offset as
			# the *pointer* produced `(1u)[1u]`, and passing `offset=0`
			# silently read the arm's first byte instead of its own.
			loaded = self._load_expression(
				scalar, placement, self._value_base(struct, placement),
				offset=self._value_offset(placement))
			return [
				*head,
				f"static inline situ_err_t {ident(self.prefix, struct.name, local, 'get')}"
				f"(situ_view_t view, {ctype} *out)",
				"{",
				f"\tif ({test}) {{",
				"\t\treturn SITU_ERR_VERSION;",
				"\t}",
				f"\t*out = ({ctype})({loaded});",
				"\treturn SITU_OK;",
				"}",
			]

		if scalar is not None and scalar.bits == BITS_PER_BYTE:
			length = (self._length_expression(struct, placement)
			          if self._has_length(struct, placement) else None)
			if length is None:
				return [*head, "/* ...and its length is not one this can"
				        " compute, so no accessor. */"]
			return [
				*head,
				f"static inline situ_err_t {ident(self.prefix, struct.name, local, 'ptr')}"
				"(situ_view_t view, const uint8_t **out, uint32_t *len)",
				"{",
				f"\tif ({test}) {{",
				"\t\treturn SITU_ERR_VERSION;",
				"\t}",
				f"\t*out = view.base + {base};",
				f"\t*len = {length};",
				"\treturn SITU_OK;",
				"}",
			]

		# A struct-typed arm: `case msg_type.hello: Hello hello;`, which is
		# section 9.6's own example and so the common shape rather than the
		# exotic one. A sub-view over it, guarded the same way -- the arm's
		# own members are its type's to emit, which is what makes this the
		# whole of the work.
		nested = self.resolved.structs.get(placement.type_name or "")
		if nested is not None and placement.array_count is None \
				and placement.sized_by is None:
			if nested.layout.is_fixed_size:
				size = macro(self.prefix, nested.name, "SIZE_FIXED")
			elif self._struct_extent(nested):
				size = "size"
			else:
				return [*head, f"/* ...and one `{placement.type_name}` cannot be"
				        " measured, so there is no",
				        " * sub-view to hand back. */"]

			lines = [
				*head,
				f"static inline situ_err_t "
				f"{ident(self.prefix, struct.name, local, 'view')}"
				"(situ_view_t view, situ_view_t *out)",
				"{",
				f"\tif ({test}) {{",
				"\t\treturn SITU_ERR_VERSION;",
				"\t}",
			]
			if size == "size":
				lines.extend([
					"\tsitu_view_t whole;",
					"",
					f"\tif (situ_view_sub(view, {base}, view.limit - ({base}),"
					" &whole) != SITU_OK) {",
					"\t\treturn SITU_ERR_BOUNDS;",
					"\t}",
					f"\tconst uint32_t size = "
					f"{ident(self.prefix, nested.name, 'extent')}(whole);",
				])
			lines.extend([
				f"\treturn situ_view_sub(view, {base}, {size}, out);",
				"}",
			])
			return lines

		return [*head, f"/* ...and `{placement.name}` is not a shape this"
		        " backend reaches into yet. */"]

	def _arm_guard(self, struct: ResolvedStruct,
			placement: Placement) -> tuple[str, str] | None:
		"""The test that this arm is the one present, and the arm's name.

		None where the member is not in a variant arm, or where the variant
		is one this cannot dispatch on -- an unresolvable discriminant, or a
		`default` arm, which is present exactly when no `case` matched and so
		needs the negation of all of them.
		"""
		found = arm_of(struct, placement)
		if found is None:
			return None
		variant, arm = found

		held = self._over_fields(struct, variant.discriminant or "", "view")
		if arm.value is None:
			# A `default` arm: present when nothing else matched.
			matched = matched_values(variant)
			if not matched:
				return None
			test = " || ".join(f"{held} == {one.value}u" for one in matched)
			return f"({test})", arm.source or "default"
		return f"{held} != {arm.value}u", arm.source or str(arm.value)

	def _varint_field(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""Decode a varint field, and say how wide it turned out to be.

		Two accessors, because a varint answers two questions and a caller
		usually wants one of them. `_get` decodes the value; `_len` says how
		many bytes it occupied, which is what every member after it in this
		frame is measured through. That is what `offset = Dynamic` costs here,
		and it is a read rather than a scan.

		There was no accessor at all until this, and the note said so -- but
		the note was the smaller half of the problem. `_length_expression` had
		no case for a varint either, so it fell through to the array branch and
		returned a length of zero: every member after a varint got an offset
		computed as though the varint were not there, and read the varint's own
		bytes. Silently, and with an accessor that looked like any other.
		"""
		declared = next((decl for decl in self.schema.varints()
		                 if decl.name == placement.varint), None)
		if declared is None:
			return []

		local  = c_name(self._local(struct, placement))
		get    = ident(self.prefix, struct.name, local, "get")
		length = ident(self.prefix, struct.name, local, "len")
		base   = self._base_expression(struct, placement, gated=False)
		width  = declared.max_bytes
		signed = declared.transform is ast.VarintTransform.ZIGZAG
		ctype  = "int64_t" if signed else "uint64_t"
		big    = declared.encoding is ast.VarintEncoding.BE128

		decoded = ("(int64_t)situ_zigzag_decode(raw)" if signed else "raw")

		# The two encodings differ in which end the groups come from, and the
		# big-endian one in what its last permitted byte carries -- eight bits
		# and no continuation flag where there is no spare bit for one.
		read = (f"situ_varint_be_get(view.base + at, view.limit - at,"
		        f" {width}u, {declared.terminal_bits}u, &raw)" if big else
		        f"situ_varint_get(view.base + at, view.limit - at,"
		        f" {width}u, &raw)")
		encoded = (f"situ_varint_be_len(raw, {width}u,"
		           f" {declared.terminal_bits}u)" if big else
		           "situ_varint_len(raw)")

		minimal = ([
			"",
			"\t/* `minimal` is declared, so a padded encoding is a different",
			"\t * encoding of the same value and this schema does not admit",
			"\t * one. Without the check `canonical` would be a claim nothing",
			"\t * enforced. */",
			f"\tif (used != {encoded}) {{",
			"\t\treturn SITU_ERR_CONSTRAINT;",
			"\t}",
		] if declared.minimal else [])

		return [
			f"/* `{placement.name}` is a `{placement.varint}`: 1 to {width}"
			f" bytes, and how",
			" * many is in the bytes themselves.",
			" *",
			" * SITU_OK            decoded; *out is the value",
			" * SITU_ERR_BOUNDS    the frame ends mid-value, or it is longer"
			" than",
			f" *                    the {width} bytes this type allows",
			*([" * SITU_ERR_CONSTRAINT a non-minimal encoding, which `minimal`"
			   " refuses"] if declared.minimal else []),
			" */",
			f"static inline situ_err_t {get}(situ_view_t view, {ctype} *out)",
			"{",
			f"	uint32_t at = {base};",
			"	uint64_t raw = 0;",
			"	uint32_t used;",
			"",
			"	if (at >= view.limit) {",
			"		return SITU_ERR_BOUNDS;",
			"	}",
			"",
			f"	used = {read};",
			"	if (used == 0u) {",
			"		return SITU_ERR_BOUNDS;",
			"	}",
			*minimal,
			"",
			f"	*out = {decoded};",
			"	return SITU_OK;",
			"}",
			"",
			f"/* The same value, read where an error cannot be returned: the",
			" * length arithmetic downstream of this field is not fallible, and",
			" * making it so would put an error path in every accessor after"
			" it.",
			" *",
			" * `validate` is what makes this safe -- an unvalidated frame"
			" reads",
			" * zero, which is the bargain every other accessor makes with the",
			" * bounds check it did not do. */",
			f"static inline {ctype} "
			f"{ident(self.prefix, struct.name, local, 'value')}(situ_view_t view)",
			"{",
			f"\t{ctype} value = 0;",
			"",
			f"\t(void){get}(view, &value);",
			"\treturn value;",
			"}",
			"",
			f"/* How many bytes `{placement.name}` occupies. Zero where it"
			f" cannot be",
			" * read at all, which keeps every offset derived from it inside"
			" the",
			" * frame -- a width guessed at the maximum would push them past"
			" the end.",
			" * A caller who needs to tell the two apart asks `_get`. */",
			f"static inline uint32_t {length}(situ_view_t view)",
			"{",
			f"	uint32_t at = {base};",
			"	uint64_t raw = 0;",
			"",
			"	if (at >= view.limit) {",
			"		return 0u;",
			"	}",
			f"	return {read};",
			"}",
		]

	def _reserved_note(self, placement: Placement) -> list[str]:
		return [
			"",
			f"/* {placement.path} : {placement.type_name} -- reserved, no accessor.",
			" * Reserved regions are validated on parse, not exposed; see the",
			" * validate function below. */",
		]

	def _marker(self, struct: ResolvedStruct, placement: Placement) -> list[str]:
		"""The byte-order marker's constants and accessors (section 8.3).

		The marker is compared as a byte sequence, not decoded as a number: it
		has to be readable before its own byte order is known. Reading it big
		endian and comparing against the literal as written is the only
		interpretation that does not presuppose the answer.

		The host constant is what a writer uses instead of hardcoding an order,
		which is what makes the writer deterministic even though the format is
		not canonical.
		"""
		from situc.expr import evaluate

		marker = self.markers[placement.name]
		scalar = placement.scalar
		assert scalar is not None

		env    = self.resolved.layout.env
		little = evaluate(marker.little, env)
		big    = evaluate(marker.big, env)
		width  = scalar.bits
		base   = macro(self.prefix, struct.name, placement.name)
		local  = c_name(self._local(struct, placement))

		return [
			f"#define {base}_LITTLE 0x{little:0{width // 4}X}u",
			f"#define {base}_BIG    0x{big:0{width // 4}X}u",
			"",
			"/* The host's own order, resolved at compile time. A writer stores",
			" * this rather than picking an order, so the writer is deterministic",
			" * even though the format admits both. */",
			"#if SITU_HOST_BIG",
			f"#define {base}_HOST {base}_BIG",
			"#else",
			f"#define {base}_HOST {base}_LITTLE",
			"#endif",
			"",
			f"static inline int "
			f"{ident(self.prefix, struct.name, local, 'is_little')}(situ_view_t view)",
			"{",
			f"\treturn situ_get_be{width}(view.base + {placement.offset_bytes}u)"
			f" == {base}_LITTLE;",
			"}",
			f"static inline uint{width}_t "
			f"{ident(self.prefix, struct.name, local, 'host')}(void)",
			"{",
			f"\treturn {base}_HOST;",
			"}",
			f"static inline void "
			f"{ident(self.prefix, struct.name, local, 'set_host')}(situ_view_t view)",
			"{",
			f"\tsitu_put_be{width}(view.base + {placement.offset_bytes}u, {base}_HOST);",
			"}",
		]

	def _value_base(self, struct: ResolvedStruct, placement: Placement,
			gated: bool = False) -> str:
		"""The pointer a scalar accessor loads from.

		A statically placed field reads from the view base at a constant
		displacement. A dynamically placed one has its offset resolved first,
		so the displacement folds into the pointer and the load itself stays a
		single access.

		A gated accessor reads through the wrapper's view, which is the same
		frame view: the gate is a compile-time obligation, not a different base,
		so the load is still `base + K`.
		"""
		base = "gate.view.base" if gated else "view.base"
		if placement.offset_bits is not None:
			return base
		return f"({base} + {self._base_expression(struct, placement, gated)})"

	def _value_offset(self, placement: Placement) -> int | None:
		return None if placement.offset_bits is not None else 0

	def _scalar_bytes(self, scalar: ScalarType) -> int:
		"""How many bytes a scalar's load touches. A bit-packed field is
		inside a container the layout already placed, and its container is
		what has to be in the view."""
		return max(1, (scalar.bits + BITS_PER_BYTE - 1) // BITS_PER_BYTE)

	def _fits(self, struct: ResolvedStruct, placement: Placement,
			bytes_: int, held: str = "view", gated: bool = False) -> str | None:
		"""Whether a fixed-size member at a *dynamic* offset is in the view.

		None where the question does not arise: a statically placed member is
		inside the frame by the bounds check that acquired the view (20.2),
		which is the argument that makes every constant-offset access
		unchecked. A member placed after a variable-length region is not
		covered by it -- its offset is a sum of lengths the message chose, and
		`examples/packet` put its tag 65 kilobytes past a 62-byte view.

		The offset is already clamped to the view (`situ_advance_u32`), so
		what remains to ask is whether the member *fits* there. It is the same
		bargain 26.27 struck for lengths: the accessor answers safely, and
		`validate` reports the message as malformed.
		"""
		if placement.offset_bits is not None:
			return None
		base = self._base_expression(struct, placement, gated)
		return f"situ_in_bounds({held}, {base}, {bytes_}u)"

	def _is_array(self, placement: Placement) -> bool:
		return self.resolved.find(placement.path + "[]") is not None

	def _base_expression(self, struct: ResolvedStruct, placement: Placement,
			gated: bool = False) -> str:
		"""Where this member starts, in bytes, as a C expression."""
		if placement.offset_bits is not None:
			return f"{placement.offset_bytes}u"
		local    = c_name(self._local(struct, placement))
		argument = "gate.view" if gated else "view"
		return f"{ident(self.prefix, struct.name, local, 'offset')}({argument})"

	def _top_level(self, struct: ResolvedStruct) -> list[Placement]:
		"""The struct's own members, in order, partitioning its bytes exactly.

		An `authenticated` region is left out: it consumes no bytes of its own,
		it names bytes its members already account for, and counting both would
		double every offset after it. A `sealed` region stays, because its
		members are the codec's output and are not in this list.
		"""
		return own_members(struct)

	# -- delimited members (section 8.6.1) ------------------------------

	def _is_record_run(self, placement: Placement) -> bool:
		"""`T x[] until "D"` where T is a struct: a run of records, not bytes.

		The two spell alike and mean different things, and getting them
		confused is silent. For a byte array the delimiter ends the *content*,
		so the scan looks for it anywhere. For a run of records it ends the
		*run*, and is only a terminator where an element would otherwise
		start -- a CRLF inside the first header line is part of that line, not
		the end of the block. Scanning for it anywhere found the first one and
		stopped there.
		"""
		return (placement.delimiter is not None
		        and placement.type_name in self.structs)

	def _is_run_element(self, name: str) -> bool:
		"""Whether anything needs to know how long one `name` is.

		A run walks them, a nested member has to size its sub-view, and an
		`indexed` region has to narrow an offset to one element. All three read
		the same function, and gating it on the run alone left the nested case
		reaching for `SIZE_FIXED` instead -- and later left an indexed region
		saying it could not compute an extent that was simply not emitted.
		"""
		return any((self._is_record_run(entry.placement)
		            or entry.placement.repeat_while is not None
		            or entry.placement.kind == "indexed"
		            or self._is_nested_member(entry.placement))
		           and entry.placement.type_name == name
		           for other in self.resolved.structs.values()
		           for entry in other.entries)

	def _is_nested_member(self, placement: Placement) -> bool:
		return (placement.kind == "field"
		        and placement.delimiter is None
		        and placement.array_count is None
		        and placement.sized_by is None
		        and placement.type_name in self.structs)

	def _struct_extent(self, struct: ResolvedStruct) -> list[str]:
		"""How many bytes one instance of a variable struct occupies.

		Needed to walk a run of them: the next element starts where this one
		ends, and for a struct whose own members are delimited that is not a
		constant. A fixed-size struct has `SIZE_FIXED` and needs none of this.
		"""
		# Only where something walks a run of these. Emitted for every variable
		# struct it was dead code in most headers, and in one case a function
		# that summed a member with no resolvable length and returned a
		# confident zero.
		if not self._is_run_element(struct.name):
			return []
		# The arithmetic is shared (traverse.extent_parts); only rendering the
		# per-member lengths is C's business.
		parts = extent_parts(self.resolved.structs, struct)
		if parts is None:
			return []
		constant, variable = parts

		terms: list[str] = []
		for placement in variable:
			if not self._has_length(struct, placement):
				return []
			terms.append(self._length_expression(struct, placement))

		lines = [
			"",
			f"/* How many bytes one `{struct.name}` occupies at this view's base.",
			" *",
			" * A run of these is walked rather than indexed, and the walk needs",
			" * to know where each one ends. For a struct whose own members are",
			" * delimited that is a scan, not a constant. */",
			f"static inline uint32_t {ident(self.prefix, struct.name, 'extent')}"
			"(situ_view_t view)",
			"{",
			f"\tuint32_t extent = {constant}u;",
		]
		lines.extend(f"\textent = extent + ({term});" for term in terms)
		if not terms:
			lines.append("\t(void)view;")
		lines.extend(["\treturn extent;", "}"])
		return lines

	def _required(self, struct: ResolvedStruct) -> list[str]:
		"""How many bytes a whole message needs, given the ones so far.

		The framing question, which every stream receiver answers by hand and
		gets wrong on the truncated-length case: a `u32` length read from four
		bytes that have not all arrived is three bytes and a guess.

		It is the extent again, computed defensively. The extent function
		assumes its bytes are present, because a walk has already bounds-
		checked the frame; this one is called on a prefix and has to check
		before every read.

		`at` starts at the sum of *every* fixed member, including the ones
		after the variable member being measured, and grows by each variable
		length as it is resolved. So the check before a read is against a
		number no smaller than that member's base -- conservative, and that is
		what makes it sufficient. A length field always precedes the member it
		sizes (section 10), so everything the next expression reads lies below
		the base, and therefore below `at`.

		The cost of the conservatism is that a message can be declared
		incomplete one round earlier than strictly necessary, which costs a
		caller one more read of a socket they were reading anyway.
		"""
		if struct.layout.register is not None:
			return []

		# A fixed-size struct is the easiest framing there is and was being
		# declined, because `extent_parts` returns None for one -- it exists
		# to measure the variable case and says so by refusing the other.
		# Correct for its own purpose and the wrong question here.
		if struct.layout.is_fixed_size and struct.layout.is_byte_sized:
			return self._required_fixed(struct)

		parts = extent_parts(self.resolved.structs, struct)
		if parts is None:
			return self._unframeable(struct)

		constant, variable = parts
		if constant == 0 and not variable:
			# Nothing to frame: every buffer, including an empty one, holds a
			# complete message. True of a bare `tlv` region and useless to
			# say, so it is not said.
			return self._unframeable(struct, "a complete one can be zero bytes,"
			        " so every buffer already holds one")

		steps: list[str] = []
		at = str(constant) + "u"

		for placement in variable:
			if not self._has_length(struct, placement):
				return self._unframeable(struct)
			local = c_name(self._local(struct, placement))
			steps.extend([
				"",
				f"\t/* {placement.path}: reading its length means reading bytes"
				" that",
				"\t * have to be here first. */",
				"\tif (have < at) {",
				"\t\t*need = at;",
				"\t\treturn SITU_ERR_TRUNCATED;",
				"\t}",
			])
			if is_run(placement, self.structs):
				walk = self._framing_walk(struct, placement)
				if walk is None:
					return self._unframeable(struct, "a run whose element"
					        " cannot be framed cannot be framed either")
				steps.extend(walk)
				continue

			if placement.delimiter is not None:
				terminated = ident(self.prefix, struct.name, local, "terminated")
				steps.extend([
					f"\tif (!{terminated}_from(view, at)) {{",
					"\t\t/* The delimiter is not in what we have. How much"
					" more is a",
					"\t\t * question only the sender can answer, so the honest"
					" lower",
					"\t\t * bound is one byte: read again and ask again. */",
					"\t\t*need = have + 1u;",
					"\t\treturn SITU_ERR_TRUNCATED;",
					"\t}",
				])
			steps.append("\tat = at + ("
				+ self._length_expression(struct, placement, running="at")
				+ ");")

		# Only where something reads through it. A length that is arithmetic
		# over nothing -- a varint's, say -- leaves the local set and unused,
		# which is `-Werror` under `-Wunused-but-set-variable`.
		uses_view = any("view" in line for line in steps)
		view_lines = [
			"\t/* A view over what has arrived, so every length expression"
			" below",
			"\t * reads through the same bounds the accessors do. */",
			"\tview.base       = (uint8_t *)(uintptr_t)(const void *)data;",
			"\tview.limit      = have;",
			"\tview.generation = 0u;",
		] if uses_view else ["\t(void)data;"]

		size_min = macro(self.prefix, struct.name, "SIZE_MIN")
		lines = [
			"",
			f"/* How many bytes a whole `{struct.name}` needs, given `have` of"
			" them.",
			" *",
			" * SITU_OK            a complete one is present; *need is its"
			" length",
			" * SITU_ERR_TRUNCATED not yet; *need is a lower bound on the total",
			" *",
			" * For framing a stream: read, ask, read the difference, ask"
			" again. The",
			" * length fields are read only once the bytes holding them have"
			" arrived,",
			" * which is the case every hand-written version of this gets"
			" wrong. */",
			f"static inline situ_err_t "
			f"{ident(self.prefix, struct.name, 'required')}"
			"(const uint8_t *data, uint32_t have, uint32_t *need)",
			"{",
			f"\tuint32_t at = {at};",
			*(["\tsitu_view_t view;"] if uses_view else []),
			"",
			# Skipped where the minimum is zero: `have < 0u` is always false
			# and `-Wtype-limits` says so.
			*([] if struct.layout.size_bytes == 0 else [
				f"\tif (have < {size_min}) {{",
				f"\t\t*need = {size_min};",
				"\t\treturn SITU_ERR_TRUNCATED;",
				"\t}",
				"",
			]),
			*view_lines,
			*steps,
			"",
			"\t*need = at;",
			"\treturn have >= at ? SITU_OK : SITU_ERR_TRUNCATED;",
			"}",
		]
		return lines

	def _framing_walk(self, struct: ResolvedStruct,
			placement: Placement) -> list[str] | None:
		"""Framing a run: one element at a time, through the element's own
		`required`.

		The walk the accessors use cannot answer this. It stops when the
		terminator stands where an element would, when an element runs past
		the view, and when the bytes run out -- and it cannot tell the first
		from the last, which are opposite answers to "is a whole one here?".
		So the run was declined for a record run and, worse, *accepted* for a
		`while` run: two bytes of a nine-byte reply came back complete, with
		`need` reported as zero (26.31).

		Asking the element's own `required` is what separates them. It is the
		same question one level down, it already distinguishes truncation from
		completion, and it is emitted for every element type this can reach --
		`traverse.frameable` is the check, and it recurses for the same reason
		this does.
		"""
		element = self.resolved.structs.get(placement.type_name or "")
		if element is None or not frameable(self.resolved.structs, element):
			return None

		local    = c_name(self._local(struct, placement))
		required = ident(self.prefix, element.name, "required")
		cap      = placement.repeat_cap if placement.repeat_while else None

		body = [
			"\t\tuint32_t   part;",
			"\t\tsitu_err_t e;",
		]

		if placement.repeat_while is not None:
			body.extend([
				"\t\tsitu_view_t element;",
				"",
				f"\t\te = {required}(data + at, have - at, &part);",
				"\t\tif (e != SITU_OK) {",
				"\t\t\t*need = at + part;",
				"\t\t\treturn SITU_ERR_TRUNCATED;",
				"\t\t}",
				"\t\tif (situ_view_sub(view, at, part, &element) != SITU_OK) {",
				"\t\t\tbreak;",
				"\t\t}",
				"\t\tat = at + part;",
				"",
				"\t\t/* The condition is asked about the element just read,"
				" which is",
				"\t\t * the whole difference from a delimiter -- and it can"
				" only be",
				"\t\t * asked once that element is known to be entirely"
				" here. */",
				f"\t\tif (!({self._element_condition(element, placement)})) {{",
				"\t\t\tbreak;",
				"\t\t}",
				*([] if cap is None else [
					"\t\tn = n + 1u;",
					f"\t\tif (n == {cap}u) {{",
					"\t\t\tbreak;",
					"\t\t}",
				]),
			])
		else:
			assert placement.delimiter is not None
			delim = placement.delimiter
			sym   = ident(self.prefix, struct.name, local, "delim")
			body.extend([
				"",
				f"\t\tif (have < at + {len(delim)}u) {{",
				f"\t\t\t*need = at + {len(delim)}u;",
				"\t\t\treturn SITU_ERR_TRUNCATED;",
				"\t\t}",
				"",
				"\t\t/* The terminator only terminates where an element would"
				" start.",
				"\t\t * It belongs to this member, as a delimiter does. */",
				f"\t\tif (situ_scan(data + at, {len(delim)}u, {sym},"
				f" {len(delim)}u) == 0u) {{",
				f"\t\t\tat = at + {len(delim)}u;",
				"\t\t\tbreak;",
				"\t\t}",
				"",
				f"\t\te = {required}(data + at, have - at, &part);",
				"\t\tif (e != SITU_OK) {",
				"\t\t\t*need = at + part;",
				"\t\t\treturn SITU_ERR_TRUNCATED;",
				"\t\t}",
				"\t\tat = at + part;",
			])

		# The counter exists only where a cap reads it, and lives in a block
		# of its own: it belongs to this member's walk and a second run in the
		# same struct would otherwise redeclare it.
		loop = ["\tfor (;;) {", *body, "\t}"]
		if cap is not None:
			loop = ["\t{", "\t\tuint32_t n = 0u;",
			        *[f"\t{line}" if line else line for line in loop], "\t}"]

		return [
			"",
			f"\t/* {placement.path}: a run of `{element.name}`, framed one"
			" element at",
			"\t * a time. The walk the accessors use stops at the end of the"
			" bytes",
			"\t * as readily as at the end of the run; this asks each element"
			" whether",
			"\t * it is whole, which is the same question one level down. */",
			*loop,
		]

	def _required_fixed(self, struct: ResolvedStruct) -> list[str]:
		"""Framing a struct with one size: it is that size."""
		size = macro(self.prefix, struct.name, "SIZE_FIXED")
		return [
			"",
			f"/* How many bytes a whole `{struct.name}` needs, given `have` of"
			" them.",
			" *",
			" * This one has a single size, so the answer never depends on the"
			" bytes.",
			" * It is generated anyway: a caller framing a stream should not"
			" have to",
			" * write one loop for the fixed messages and another for the rest,"
			" and",
			" * a struct that gains a length field later keeps the same call. */",
			f"static inline situ_err_t "
			f"{ident(self.prefix, struct.name, 'required')}"
			"(const uint8_t *data, uint32_t have, uint32_t *need)",
			"{",
			"\t(void)data;",
			f"\t*need = {size};",
			f"\treturn have >= {size} ? SITU_OK : SITU_ERR_TRUNCATED;",
			"}",
		]

	def _unframeable(self, struct: ResolvedStruct,
			why: str | None = None) -> list[str]:
		"""Why no `_required` was emitted, where it would be looked for."""
		if struct.layout.register is not None:
			return []		# a register is not framed; it is addressed

		reason = why or (
			"it ends where the view ends, so how long one is is the"
			" transport's answer rather than the message's"
			if any(p.sized_by == "remaining" for p in self._top_level(struct))
			else "one of its members has no length this can compute")
		return [
			"",
			f"/* No `{struct.name}_required`: {reason}.",
			" * Framing such a message is the layer below's job -- situ can say"
			" what",
			" * the bytes mean and not when they have all arrived. */",
		]

	def _walk_prologue(self, base: str, cap: str, extent: str) -> list[str]:
		"""The head of a `while` run's loop: one element measured and bounded.

		Shared because the index walks exactly the same way and must, or the
		two disagree about where an element starts -- and a second copy of a
		loop with two break conditions is how they would.
		"""
		return [
			f"\tuint32_t at = {base};",
			"\tuint32_t n  = 0u;",
			"",
			f"\twhile (at < view.limit{cap}) {{",
			"\t\tsitu_view_t element;",
			"\t\tuint32_t    size;",
			"",
			f"\t\tif (situ_view_sub(view, at, view.limit - at, &element)"
			" != SITU_OK) {",
			"\t\t\tbreak;",
			"\t\t}",
			f"\t\tsize = {extent};",
			"\t\tif (size == 0u || at + size > view.limit) {",
			"\t\t\t/* A zero-extent element would walk here forever, and one",
			"\t\t\t * running past the limit was never in this frame. */",
			"\t\t\tbreak;",
			"\t\t}",
		]

	def _run_index(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""The second accessor family, for a run: its element offsets, once.

		A run is `access = Sequential` -- element N is reached by reading the
		N-1 before it -- so `_at` walks from the base every call and visiting
		every element costs O(N^2). This resolves the offsets in one pass and
		indexes them, which is the axis the map says is weak and the only one
		worth spending memory on: the *bytes* are already reachable by
		pointer, so copying them buys nothing. What is expensive here is
		finding them.

		The storage is the caller's. `max N` in the schema is what makes the
		array a fixed size, so an uncapped run gets no index rather than an
		allocation -- generated code never allocates (invariant 4), and the
		cap is the schema saying how much memory this may cost.
		"""
		if not self.materialize:
			return []

		cap = placement.repeat_cap or placement.delimiter_cap
		if cap is None:
			return [
				"",
				f"/* No index for `{placement.name}`: the run has no `max`, so",
				" * how many offsets to hold is not a number this knows and the",
				" * array would have to be allocated. Add `max N` and the index",
				" * costs N words of the caller's memory. */",
			]

		# The index walks the run itself, in one pass, so it needs what the
		# walk needs. Building it by calling the walking accessor per element
		# is quadratic again, which is the whole thing it exists to avoid.
		element = self.resolved.structs.get(placement.type_name or "")
		extent  = (None if element is None
		           else self._element_extent_call(element))
		if element is None or extent is None:
			return []

		base = self._base_expression(struct, placement, gated=False)
		if placement.repeat_while is not None:
			cond = self._element_condition(element, placement)
			walk = self._walk_prologue(base, f" && n < {cap}u", extent)
		else:
			# A record run stops where the terminator stands rather than on a
			# condition, so it shares that walk instead. The cap is the
			# scan's, and it bounds the index for the same reason.
			delim = placement.delimiter
			assert delim is not None
			cond = None
			walk = self._record_prologue(
				base, delim,
				ident(self.prefix, struct.name,
				      c_name(self._local(struct, placement)), "delim"),
				extent)

		local  = c_name(self._local(struct, placement))
		kind   = ident(self.prefix, struct.name, local, "index_t")
		build  = ident(self.prefix, struct.name, local, "index")
		fetch  = ident(self.prefix, struct.name, local, "indexed")
		macro_ = macro(self.prefix, struct.name, local, "CAP")

		return [
			"",
			f"/* An index over `{placement.path}`: where each element starts.",
			" *",
			f" * The map calls this run `access = Sequential`, which is the"
			" cost of",
			" * a walk: reaching element N means reading the N-1 before it, so"
			" the",
			" * `_at` accessor above starts from the base every call and"
			" visiting",
			" * every element is quadratic. Building this is one pass; every"
			" lookup",
			" * after it is arithmetic.",
			" *",
			" * The memory is the caller's, and `max` in the schema is what"
			" bounds",
			" * it. Nothing here allocates. */",
			f"#define {macro_} {cap}u",
			"",
			f"typedef struct {kind} {{",
			"	uint32_t count;",
			"	/* One more than `count`: the last entry is where the run",
			"	 * ends, so an element's size is the difference between",
			"	 * neighbours and the last one needs no special case. */",
			f"	uint32_t start[{macro_} + 1u];",
			f"}} {kind};",
			"",
			f"static inline situ_err_t {build}(situ_view_t view, {kind} *out)",
			"{",
			*walk,
			"		/* Recorded before advancing, so `start[n]` is where element",
			"		 * n begins. The walk is the run's own -- the same lines the",
			"		 * accessors above use -- because an index that disagreed",
			"		 * with the walk about where an element starts would be",
			"		 * worse than no index. */",
			"		out->start[n] = at;",
			"		at = at + size;",
			"		n  = n + 1u;",
			*([] if cond is None else [
				f"		if (!({cond})) {{",
				"			break;",
				"		}",
			]),
			"		if (n == " + str(cap) + "u) {",
			"			break;",
			"		}",
			"	}",
			"",
			"	/* One pass. Building this by calling the walking accessor per",
			"	 * element would be quadratic again, which is what the first",
			"	 * version did -- and it measured 13% faster than the walk",
			"	 * instead of the order of magnitude the shape promises. */",
			"	out->count    = n;",
			"	out->start[n] = at;",
			"	return SITU_OK;",
			"}",
			"",
			f"/* Element `index`, in constant time. The view must be the one the",
			" * index was built from: it holds offsets, not pointers, so a"
			" different",
			" * view of the same bytes is fine and a different message is"
			" not. */",
			f"static inline situ_err_t {fetch}(const {kind} *idx,"
			" situ_view_t view,",
			"		uint32_t index, situ_view_t *out)",
			"{",
			"	uint32_t start;",
			"",
			"	if (index >= idx->count) {",
			"		return SITU_ERR_BOUNDS;",
			"	}",
			"	start = idx->start[index];",
			"	/* Arithmetic, not a walk. Calling the walking accessor here",
			"	 * would build an index and then ignore it, which is what the",
			"	 * first version of this did. */",
			"	return situ_view_sub(view, start,",
			"		idx->start[index + 1u] - start, out);",
			"}",
		]

	def _repeat_while(self, struct: ResolvedStruct,
			entry: Resolved) -> list[str]:
		"""A run ending after the element that fails a condition (8.6.6).

		Two protocols asked for this and neither could be written with a
		delimiter: SMTP's multiline reply ends after the line whose separator
		is a space, and an IPv6 extension chain ends after the header whose
		`next_header` names an upper-layer protocol. The difference from
		`until` is the quantifier -- that asks about the position before each
		element, and this asks about the element just read.

		Never empty: the first element is parsed before the condition is
		evaluated, and whether the run is there at all is a `variant`'s
		question.
		"""
		placement = entry.placement
		element   = self.resolved.structs.get(placement.type_name or "")
		if element is None:
			return []

		extent = self._element_extent_call(element)
		if extent is None:
			return [
				f"/* No accessors for `{placement.name}`: one"
				f" `{placement.type_name}` has no",
				" * extent this build can compute, so the run cannot be"
				" walked. */",
			]

		local = c_name(self._local(struct, placement))
		base  = self._base_expression(struct, placement, gated=False)
		cond  = self._element_condition(element, placement)
		cap   = ("" if placement.repeat_cap is None
		         else f" && n < {placement.repeat_cap}u")

		count = ident(self.prefix, struct.name, local, "count")
		at    = ident(self.prefix, struct.name, local, "at")
		span  = ident(self.prefix, struct.name, local, "span")

		walk  = self._walk_prologue(base, cap, extent)
		from_ = self._walk_prologue("start", cap, extent)

		return [
			f"/* `{placement.name}` is a run of `{placement.type_name}` ending"
			f" after the",
			f" * element for which `{placement.repeat_while}` is false. The"
			" element that",
			" * ends it is part of the run: the condition is asked about it"
			" after it",
			" * has been read, which is the whole difference from a"
			" delimiter. */",
			f"static inline uint32_t {count}(situ_view_t view)",
			"{",
			*walk,
			"\t\tat = at + size;",
			"\t\tn  = n + 1u;",
			f"\t\tif (!({cond})) {{",
			"\t\t\tbreak;",
			"\t\t}",
			"\t}",
			"\treturn n;",
			"}",
			"",
			f"static inline situ_err_t {at}(situ_view_t view, uint32_t index,"
			" situ_view_t *out)",
			"{",
			*walk,
			"\t\tif (n == index) {",
			"\t\t\treturn situ_view_sub(view, at, size, out);",
			"\t\t}",
			"\t\tat = at + size;",
			"\t\tn  = n + 1u;",
			f"\t\tif (!({cond})) {{",
			"\t\t\tbreak;",
			"\t\t}",
			"\t}",
			"\treturn SITU_ERR_BOUNDS;",
			"}",
			"",
			"/* The walk, from a base the caller already knows: the same",
			" * helper every delimited member has, for the same reason. */",
			f"static inline uint32_t {span}_from(situ_view_t view,"
			" uint32_t start)",
			"{",
			*from_,
			"\t\tat = at + size;",
			"\t\tn  = n + 1u;",
			f"\t\tif (!({cond})) {{",
			"\t\t\tbreak;",
			"\t\t}",
			"\t}",
			"\t(void)n;",
			"\treturn at - start;",
			"}",
			"",
			f"static inline uint32_t {span}(situ_view_t view)",
			"{",
			f"\treturn {span}_from(view, {base});",
			"}",
		]

	def _element_extent_call(self, element: ResolvedStruct) -> str | None:
		"""How one element's length is read, as an expression over `element`."""
		if element.layout.is_fixed_size:
			return f"{macro(self.prefix, element.name, 'SIZE_FIXED')}"
		if not self._struct_extent(element):
			return None
		return f"{ident(self.prefix, element.name, 'extent')}(element)"

	def _element_condition(self, element: ResolvedStruct,
			placement: Placement) -> str:
		"""The predicate, as C over the element's own view."""
		return self._over_fields(element, placement.repeat_while or "", "element")

	def _over_fields(self, struct: ResolvedStruct, source: str,
			held: str) -> str:
		"""A schema expression over a struct's own fields, as C.

		Every name in it is a field of `struct`, so each becomes that field's
		getter over the view named by `held`. Longest name first, or `len`
		would rewrite the `len` inside `hdr_ext_len`.
		"""
		names = [entry.placement.name for entry in struct.entries
		         if entry.placement.scalar is not None
		         and "." not in entry.placement.path[len(struct.name) + 1:]]
		return over_fields(names, source, lambda name:
			f"{ident(self.prefix, struct.name, c_name(name), 'get')}({held})")

	def _record_prologue(self, base: str, delim: bytes, sym: str,
			extent: str) -> list[str]:
		"""The head of a record run's loop, shared with its index.

		The `while` run's equivalent is `_walk_prologue`; the difference is
		this one asks whether the terminator stands where an element would
		before measuring anything. Both are shared for the same reason: an
		index built by a second copy of the walk would eventually disagree
		with the walk about where an element starts.
		"""
		return [
			f"\tuint32_t at = {base};",
			"\tuint32_t n  = 0u;",
			"",
			f"\twhile (at + {len(delim)}u <= view.limit) {{",
			"\t\tsitu_view_t element;",
			"\t\tuint32_t    size;",
			"",
			"\t\t/* The terminator only terminates where an element would",
			"\t\t * start. Inside one it is that element's own byte. */",
			f"\t\tif (situ_scan(view.base + at, {len(delim)}u, {sym}, "
			f"{len(delim)}u) == 0u) {{",
			"\t\t\tbreak;",
			"\t\t}",
			f"\t\tif (situ_view_sub(view, at, view.limit - at, &element)"
			" != SITU_OK) {",
			"\t\t\tbreak;",
			"\t\t}",
			f"\t\tsize = {extent};",
			"\t\tif (size == 0u || at + size > view.limit) {",
			"\t\t\t/* A zero-extent element would walk here forever, and one",
			"\t\t\t * running past the limit was never in this frame. */",
			"\t\t\tbreak;",
			"\t\t}",
		]

	def _record_run(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""A run of records, ending where the terminator would be an element.

		Three functions, and a walk in each rather than one cached count: a
		view is a value and situ never allocates, so there is nowhere *here*
		to put a table of offsets. There is somewhere in the caller, which is
		what `--materialize` uses -- `max N` on the run bounds the array and
		the caller owns it. `indexed` is the construct for a caller who needs
		O(1), and the map says `access = Sequential` here so nobody reaches for
		this expecting it.

		Every walk is bounded twice over -- by the view's limit, and by
		refusing to advance on a zero-extent element. The second is not
		theoretical: a record whose members are all delimited and all empty
		occupies no bytes, and a walk that took it would not terminate on
		input somebody chose.
		"""
		assert placement.delimiter is not None
		element = self.resolved.structs[placement.type_name]
		if not self._struct_extent(element):
			return self._unwalkable_run(struct, placement)

		local  = c_name(self._local(struct, placement))
		delim  = placement.delimiter
		bytes_ = ", ".join(f"0x{byte:02X}u" for byte in delim)
		base   = self._base_expression(struct, placement, gated=False)
		extent = ident(self.prefix, element.name, "extent")

		sym   = ident(self.prefix, struct.name, local, "delim")
		count = ident(self.prefix, struct.name, local, "count")
		at    = ident(self.prefix, struct.name, local, "at")
		span  = ident(self.prefix, struct.name, local, "span")

		walk = self._record_prologue(base, delim, sym, f"{extent}(element)")
		from_ = self._record_prologue("start", delim, sym, f"{extent}(element)")

		return [
			f"/* `{placement.name}` is a run of `{element.name}`, ending where",
			f" * {render_delimiter(delim)} stands in for one. Walked rather than",
			" * indexed: a view is a value and nothing here allocates, so there",
			" * is nowhere in this header to keep a table of offsets. Build with",
			" * `--materialize` and a `max` on the run for one the caller owns. */",
			f"static const uint8_t {sym}[{len(delim)}] = {{{bytes_}}};",
			"",
			f"static inline uint32_t {count}(situ_view_t view)",
			"{",
			*walk,
			"\t\tat = at + size;",
			"\t\tn  = n + 1u;",
			"\t}",
			"\treturn n;",
			"}",
			"",
			f"static inline situ_err_t {at}(situ_view_t view, uint32_t index,"
			" situ_view_t *out)",
			"{",
			*walk,
			"\t\tif (n == index) {",
			"\t\t\treturn situ_view_sub(view, at, size, out);",
			"\t\t}",
			"\t\tat = at + size;",
			"\t\tn  = n + 1u;",
			"\t}",
			"\treturn SITU_ERR_BOUNDS;",
			"}",
			"",
			"/* The walk, from a base the caller already knows.",
			" *",
			" * Same bargain the delimited members make: everything that",
			" * accumulates offsets has `at` in hand, and the plain `_span`",
			" * re-resolves the base by rescanning every member before this",
			" * one. This was the last member kind without the helper. */",
			f"static inline uint32_t {span}_from(situ_view_t view,"
			" uint32_t start)",
			"{",
			*from_,
			"\t\tat = at + size;",
			"\t\tn  = n + 1u;",
			"\t}",
			"\t(void)n;",
			"",
			"\t/* The terminator belongs to this member, as a delimiter does.",
			"\t * Where the run ran out of buffer instead there is none to",
			"\t * count. */",
			f"\tif (at + {len(delim)}u <= view.limit) {{",
			f"\t\tat = at + {len(delim)}u;",
			"\t}",
			"\treturn at - start;",
			"}",
			"",
			f"static inline uint32_t {span}(situ_view_t view)",
			"{",
			f"\treturn {span}_from(view, {base});",
			"}",
		]

	def _unwalkable_run(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""A run whose element has no extent this backend can compute.

		Said rather than skipped. The member is in the map with a size and an
		offset, and a header that simply lacked the accessor would leave a
		reader looking for a typo.
		"""
		return [
			f"/* No accessors for `{placement.name}`: one `{placement.type_name}`",
			" * has no extent this build can compute, so the run cannot be",
			" * walked and nothing after it can be placed. A `[remaining]`",
			" * member inside the element is the usual cause -- it consumes",
			" * whatever view it is given, so a second element has nowhere to",
			" * begin. */",
		]

	def _delimited(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""Content, length, and where the member ends.

		Three functions rather than one, because a caller and the next member
		want different numbers. `_len` is the content, which is what a reader
		of the field wants. `_span` is content plus delimiter, which is what
		the following member's offset is computed from -- the delimiter belongs
		to this member's extent even though it is not part of its value, for
		the reason `nul_terminated` counts its capacity.

		`_terminated` is separate from both. A member whose delimiter is
		missing is truncated rather than empty, and a getter is not the place
		to decide what to do about that; `validate` is.
		"""
		assert placement.delimiter is not None

		local  = c_name(self._local(struct, placement))
		delim  = placement.delimiter
		bytes_ = ", ".join(f"0x{byte:02X}u" for byte in delim)
		base   = self._base_expression(struct, placement, gated=False)
		limit  = self._scan_limit(placement, base)

		sym        = ident(self.prefix, struct.name, local, "delim")
		ptr        = ident(self.prefix, struct.name, local, "ptr")
		length     = ident(self.prefix, struct.name, local, "len")
		span       = ident(self.prefix, struct.name, local, "span")
		terminated = ident(self.prefix, struct.name, local, "terminated")

		raw = ident(self.prefix, struct.name, local, "raw_len")
		# Trimming separates the two questions a delimited member answers.
		# The *framing* is the scan: where the delimiter is, and therefore
		# where the next member starts. The *value* is what is left after the
		# whitespace at either end. Without `[trim]` they are the same number
		# and only one function is emitted for both.
		scan_len = raw if placement.trimmed else length

		lines = [
			f"/* `{placement.name}` runs to the first {render_delimiter(delim)}."
			" Reaching anything after",
			" * it means this scan, which is why the map calls their offsets"
			" Scanned",
			" * rather than Dynamic: it is a search, and the delimiter may not"
			" be there. */",
			f"static const uint8_t {sym}[{len(delim)}] = {{{bytes_}}};",
			"",
			"/* The scan, from a base the caller already knows.",
			" *",
			" * Everything that accumulates offsets -- the offset functions,",
			" * `_required`, the offset cache -- walks the members in order and",
			" * has `at` in hand. Calling the plain `_span` there re-resolves",
			" * the base by rescanning every member before it, so a loop over M",
			" * members costs M^2 scans while looking like one pass. Measured:",
			" * an eight-member record took three times *longer* through a",
			" * cache built that way than through the per-call offsets it was",
			" * meant to replace. */",
			f"static inline uint32_t {scan_len}_from(situ_view_t view,"
			" uint32_t at)",
			"{",
			f"\treturn {self._scan_call(placement, 'view.base + at', self._scan_limit(placement, 'at'), sym)};",
			"}",
			"",
			f"static inline uint32_t {scan_len}(situ_view_t view)",
			"{",
			f"\treturn {scan_len}_from(view, {base});",
			"}",
			"",
			f"static inline int {terminated}_from(situ_view_t view, uint32_t at)",
			"{",
			f"\treturn {scan_len}_from(view, at) < ({self._scan_limit(placement, 'at')});",
			"}",
			"",
			f"static inline int {terminated}(situ_view_t view)",
			"{",
			f"\treturn {terminated}_from(view, {base});",
			"}",
			"",
			f"static inline uint32_t {span}_from(situ_view_t view, uint32_t at)",
			"{",
			"\t/* The delimiter is this member's too, so the next member starts",
			"\t * after it. Where it is missing there is nothing to add: the",
			"\t * member ran to the end of the buffer, and claiming the extra",
			"\t * bytes would put the next one past the limit its own bounds",
			"\t * check trusts. */",
			f"\treturn {scan_len}_from(view, at) + "
			f"({terminated}_from(view, at) ? {len(delim)}u : 0u);",
			"}",
			"",
			f"static inline uint32_t {span}(situ_view_t view)",
			"{",
			f"\treturn {span}_from(view, {base});",
			"}",
			"",
		]

		if not placement.trimmed:
			lines.extend([
				f"static inline const uint8_t *{ptr}(situ_view_t view)",
				"{",
				f"\treturn view.base + {base};",
				"}",
			])
			return lines

		lines.extend([
			"/* `[trim]`: the whitespace at either end is framing rather than",
			" * value, so the span above is unchanged -- those bytes are still",
			" * this member's -- and these two describe what is left. */",
			f"static inline const uint8_t *{ptr}(situ_view_t view)",
			"{",
			f"\treturn view.base + {base} + situ_trim_start(view.base + {base},"
			f" {scan_len}(view));",
			"}",
			"",
			f"static inline uint32_t {length}(situ_view_t view)",
			"{",
			f"\treturn situ_trim_len(view.base + {base}, {scan_len}(view));",
			"}",
		])
		return lines

	def _fixed_text_number(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""Digits in a field of declared width, padded (section 8.6.2).

		SMTP's reply code and HTTP's status are both this: exactly three
		digits, no delimiter, and the leading zero is required rather than
		tolerated. That last part is why this is `Canonical` where a delimited
		text number is not -- `007` is the only spelling of seven here, not a
		second one.

		The range is the field's, not the type's. `decimal u16 code[3]` holds
		0..999, and a check written against `u16` would accept a value the
		three bytes cannot represent.
		"""
		assert placement.radix is not None
		scalar = placement.scalar
		assert scalar is not None
		width  = placement.array_count or 0
		limit  = placement.radix_max or 0

		local = c_name(self._local(struct, placement))
		ctype = self._field_ctype(placement)
		base  = self._base_expression(struct, placement)
		base  = f"view.base + {base}" if base.isdigit() or base.endswith("u") \
			else base

		return [
			"",
			f"/* `{placement.name}`: {width} digits, padded, holding 0..{limit}.",
			" *",
			f" * The range is the field's rather than {scalar.name}'s: three"
			" bytes cannot",
			f" * hold what {scalar.name} can, and a check against the type"
			" would accept a",
			" * value the field cannot represent. */",
			f"static inline const uint8_t *"
			f"{ident(self.prefix, struct.name, local, 'ptr')}(situ_view_t view)",
			"{",
			f"\treturn {base};",
			"}",
			"",
			f"static inline situ_err_t "
			f"{ident(self.prefix, struct.name, local, 'get')}"
			f"(situ_view_t view, {ctype} *out)",
			"{",
			"\tuint64_t value;",
			"",
			f"\tif (situ_parse_uint({ident(self.prefix, struct.name, local, 'ptr')}"
			f"(view), {width}u, {placement.radix}u, {limit}u, &value) != 0) {{",
			"\t\treturn SITU_ERR_CONSTRAINT;",
			"\t}",
			f"\t*out = ({ctype})value;",
			"\treturn SITU_OK;",
			"}",
		]

	def _text_number(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""A number written as digits, read through a getter that can fail.

		Out-parameter and an error code, not a return value. Every other
		scalar getter in this header returns the value, because every other
		conversion is total -- a byte swap has an answer for any bit pattern.
		A decimal parse does not, and a getter that returned 0 for `12x4`
		would be handing back a number nobody wrote.

		That difference is the whole of what `repr = TextConverted` means, and
		it shows up in the signature rather than in a comment.
		"""
		assert placement.radix is not None
		scalar = placement.scalar
		assert scalar is not None

		local  = c_name(self._local(struct, placement))
		ctype  = self._field_ctype(placement)
		length = ident(self.prefix, struct.name, local, "len")
		ptr    = ident(self.prefix, struct.name, local, "ptr")
		base   = {10: "decimal", 16: "hexadecimal"}[placement.radix]
		limit  = (1 << scalar.bits) - 1

		return [
			"",
			f"/* `{placement.name}` is a {base} number: the digits between the",
			f" * start of the member and its delimiter, in the range of"
			f" {scalar.name}.",
			" *",
			" * This one takes an out-parameter because the conversion can"
			" fail, which",
			" * no other scalar getter here can. Empty digits, a byte that is"
			" not one,",
			f" * and anything above {limit} are all SITU_ERR_CONSTRAINT. */",
			f"static inline situ_err_t "
			f"{ident(self.prefix, struct.name, local, 'get')}"
			f"(situ_view_t view, {ctype} *out)",
			"{",
			"\tuint64_t value;",
			"",
			f"\tif (situ_parse_uint({ptr}(view), {length}(view), "
			f"{placement.radix}u, {limit}u, &value) != 0) {{",
			"\t\treturn SITU_ERR_CONSTRAINT;",
			"\t}",
			f"\t*out = ({ctype})value;",
			"\treturn SITU_OK;",
			"}",
		]

	def _coded_delimited_note(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""A region found by scanning and then decoded (section 13.6).

		Scan first, decode second, and the order is the protocol's rather than
		a convenience: SMTP's dot-stuffing protects its own terminator, so
		`CRLF . CRLF` is unambiguous in the encoded bytes and would not be in
		the decoded ones. A decoder that ran first would have to know where to
		stop, which is what the scan is for.
		"""
		local   = c_name(self._local(struct, placement))
		decoded = self._decode_accessor(struct, placement)

		# What follows decides what this says. The note read "there is no
		# accessor for the decoded bytes" whatever came after it, which was
		# true while the decode was emitted for `table` kernels alone and
		# became a contradiction sitting directly above one.
		about = ([" * The decoded bytes are below: the transform is derived"
		          " from the kernel,",
		          f" * so the length is {placement.codec}'s to report and not"
		          " this header's to",
		          " * guess."] if decoded else
		         [" * There is no accessor for the decoded bytes: the transform"
		          " is the",
		          f" * caller's to run, and its length is {placement.codec}'s"
		          " to report rather",
		          " * than this header's to guess."])

		return [
			"",
			f"/* `{placement.name}` is `{placement.codec}` output, and the"
			f" pointer above is",
			" * the encoded form.",
			" *",
			*about,
			" *",
			" * The scan runs on the encoded bytes, which is the order the"
			" format",
			" * specifies -- a stuffing code protects its own terminator, so"
			" the",
			" * sequence is unambiguous here and would not be after"
			" decoding. */",
			*decoded,
		]

	def _coded_region(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""The encoded bytes of a coded region, and the decode beside them."""
		local = c_name(self._local(struct, placement))
		base  = self._base_expression(struct, placement)
		if placement.size_max_bits is None \
				or placement.size_bits % BITS_PER_BYTE:
			return [
				f"/* No accessor for `{placement.name}`: its encoded extent is",
				f" * {placement.codec}'s to report and not a number this"
				" knows. */",
			]

		size = placement.size_bits // BITS_PER_BYTE
		return [
			"",
			f"/* `{placement.name}` is `{placement.codec}` output, and these"
			" are the",
			" * bytes on the wire rather than the value. What they mean is"
			" behind",
			" * the transform (13.5), which is what `stage = TransformTime`"
			" says. */",
			f"static inline uint32_t "
			f"{ident(self.prefix, struct.name, local, 'len')}(situ_view_t view)",
			"{",
			"\t(void)view;",
			f"\treturn {size}u;",
			"}",
			f"static inline const uint8_t *"
			f"{ident(self.prefix, struct.name, local, 'ptr')}(situ_view_t view)",
			"{",
			f"\treturn view.base + {base};",
			"}",
			*self._decode_accessor(struct, placement),
		]

	def _decode_accessor(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""Run the codec once, into a buffer the caller owns (13.5).

		The interior of a coded region is `stage = TransformTime`: the
		plaintext is not in the message, so unlike every other accessor here
		this one has somewhere to put the answer only if the caller says
		where. Nothing allocates (invariant 4), so the buffer and its capacity
		are parameters, and the bound the caller needs is a macro beside them.

		For a `table` or a `stuffing` kernel, which are the two whose derived
		decoder this build emits with a settled shape: `(in, count, out) ->
		count`. It was `table` alone, on the argument that the other families
		were described and not generated -- which stopped being true without
		this note noticing.

		What differs between them is what `count` measures, and that is not a
		guess: a `table` kernel is bit-oriented by construction, and a
		`stuffing` kernel declares `unit`, because HDLC counts bits where COBS
		scans bytes. Getting it wrong would pass a byte count to a bit loop and
		decode an eighth of the region.
		"""
		codec = self.codecs.get(placement.codec or "")
		if codec is None or codec.kernel is None:
			return []
		if not decodes_here(codec):
			return []		# the decoder's shape is the kernel's, and
					# only some of them are settled

		ratio = codec.ratio
		if ratio is None or ratio[0] == 0:
			return []

		# A bit-oriented kernel counts bits; a byte or stream one counts
		# bytes. The region's span is bytes either way, so exactly one of
		# these two needs the conversion.
		bitwise = decode_counts_bits(codec)
		scale   = " * 8u" if bitwise else ""
		unscale = " / 8u" if bitwise else ""

		local   = c_name(self._local(struct, placement))
		# `_len` and not `_span`: the span includes the delimiter, and the
		# delimiter is not the codec's to transform. Decoding it as content
		# put SMTP's `CRLF . CRLF` through the unstuffer and handed back two
		# extra bytes -- which nothing caught while the accessor was emitted
		# for `table` kernels alone and no delimited region used one.
		span    = (f"{ident(self.prefix, struct.name, local, 'len')}(view)"
		           if placement.delimiter is not None
		           else self._length_expression(struct, placement))
		decoded = macro(self.prefix, struct.name, local, "DECODED_MAX")
		bound   = decode_bound(codec, placement)

		out = [
			"",
			f"/* The decoded bytes of `{placement.name}`, into a buffer the"
			" caller owns.",
			" *",
			" * Its interior is `stage = TransformTime` (13.5): the plaintext"
			" is not",
			" * in the message, so this is the one accessor here that needs"
			" somewhere",
			" * to put its answer. Nothing allocates, so the buffer is the"
			" caller's",
			f" * and `{decoded}` is how large it has to be.",
			" *",
			f" * `{placement.codec}` is {ratio[0]}:{ratio[1]}, so the decoded"
			" form is that much",
			" * smaller than the bytes on the wire. */",
		]
		if bound is not None:
			out.append(f"#define {decoded} {bound}u")
		out.extend([
			f"extern uint32_t {ident(self.prefix, placement.codec or '', 'decode')}"
			f"(const uint8_t *in, uint32_t {'bits' if bitwise else 'len'},"
			f" uint8_t *out);",
			"",
			f"static inline situ_err_t "
			f"{ident(self.prefix, struct.name, local, 'decode')}"
			"(situ_view_t view, uint8_t *out, uint32_t cap, uint32_t *len)",
			"{",
			f"\tconst uint32_t encoded = {span};",
			f"\tconst uint32_t need    = encoded * {ratio[1]}u / {ratio[0]}u;",
			"",
			"\tif (cap < need) {",
			"\t\t/* Not room for what the codec will produce. Reported rather",
			"\t\t * than truncated: half a decode is not a shorter message. */",
			"\t\treturn SITU_ERR_BOUNDS;",
			"\t}",
			f"\t*len = {ident(self.prefix, placement.codec or '', 'decode')}("
			f"{ident(self.prefix, struct.name, local, 'ptr')}(view),",
			f"\t\tencoded{scale}, out){unscale};",
			"\treturn SITU_OK;",
			"}",
		])
		return out

	def _token_compare(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""How to ask whether a delimited value is a particular token.

		Emitted for every delimited byte run, not only the case-insensitive
		ones, because the question "is this field `Content-Length`" is the
		thing a caller of a text format actually asks -- and the answer depends
		on what the schema says about case. Leaving it to the caller means
		leaving them to decide, and the schema has already decided.

		`[case_insensitive]` folds ASCII case; without it this is a plain
		compare. Either way the length is checked first, so a prefix is not a
		match -- which `strncmp` against a literal would have made it.
		"""
		local  = c_name(self._local(struct, placement))
		ptr    = ident(self.prefix, struct.name, local, "ptr")
		length = ident(self.prefix, struct.name, local, "len")
		fold   = "situ_ascii_ci_eq" if placement.case_insensitive else "situ_bytes_eq"

		how = ("folding ASCII case, because the schema says this token is "
		       "case-insensitive" if placement.case_insensitive
		       else "byte for byte")

		return [
			"",
			f"/* Whether `{placement.name}` is a given token, compared {how}.",
			" *",
			" * The length is checked first, so a prefix is not a match --"
			" which is",
			" * what `strncmp` against a literal quietly makes it. */",
			f"static inline int {ident(self.prefix, struct.name, local, 'eq')}"
			"(situ_view_t view, const uint8_t *other, uint32_t other_len)",
			"{",
			f"\treturn {fold}({ptr}(view), {length}(view), other, other_len);",
			"}",
		]

	def _scan_limit(self, placement: Placement, base: str) -> str:
		"""How far the scan may run: to the cap, or to the end of the buffer."""
		if placement.delimiter_cap is None:
			return f"situ_remaining_u32(view.limit, {base})"
		# The smaller of the two, not the cap alone: a cap larger than what is
		# left would read past the extent the one bounds check established.
		return (f"situ_min_u32({placement.delimiter_cap}u, "
		        f"situ_remaining_u32(view.limit, {base}))")

	def _scan_call(self, placement: Placement, data: str, limit: str,
			sym: str) -> str:
		delim = placement.delimiter
		assert delim is not None

		if placement.delimiter_quote is None and placement.delimiter_escape is None:
			return f"situ_scan({data}, {limit}, {sym}, {len(delim)}u)"

		quote  = (f"{placement.delimiter_quote}u"
		          if placement.delimiter_quote is not None else "SITU_NO_BYTE")
		escape = (f"{placement.delimiter_escape}u"
		          if placement.delimiter_escape is not None else "SITU_NO_BYTE")
		return (f"situ_scan_relaxed({data}, {limit}, {sym}, "
		        f"{len(delim)}u, {quote}, {escape})")

	def _offsets(self, struct: ResolvedStruct) -> list[str]:
		"""Every dynamic offset in this struct, resolved in one pass.

		The other half of what `access = Sequential` costs. A run makes
		reaching element N a walk of the N-1 before it; a *scan* makes
		reaching member N a rescan of the N-1 before it, and `_offset` does
		that on every call -- so reading three members of an HTTP request line
		scans the target twice.

		What that is worth depends on how many members there are, and this
		comment used to say `4ms against 77ms` for that request line. It is
		not: measured in all four backends, three members is inside the noise
		and eight is 3x (26.30). The 4ms was taken against a baseline that no
		longer exists -- `_span` rescanned from the start of the struct then,
		and fixing that took the per-call path with it.

		Same bargain as the run index and the same reason it is off by
		default: this is memory the caller did not ask for. Nothing here
		allocates; the struct has one word per dynamically-placed member and
		the schema decides how many that is.
		"""
		if not self.materialize:
			return []

		dynamic = [held for held in self._top_level(struct)
		           if held.offset_bits is None and held.located is None]
		if not dynamic:
			return []		# every offset is already a constant

		# The accumulation is `_offset_function`'s, run once for all of them
		# rather than once per member. Bailing where that one bails: a member
		# whose length is unknown stops the chain for everything after it.
		# A running constant, flushed where it belongs rather than summed up
		# front: a fixed member *after* a variable one is not part of the
		# offsets before it, and totalling them all first put every recorded
		# offset ahead of itself by the width of everything that followed.
		plan = offset_plan(struct, self._top_level(struct),
		                   lambda held: self._has_length(struct, held))
		if plan is None:
			return [
				"",
				f"/* No offset cache for `{struct.name}`: a member has no"
				" length this",
				" * can compute, so the offsets after it cannot be resolved in"
				" one",
				" * pass any more than one at a time. */",
			]

		steps: list[str] = []
		for step in plan:
			if step.kind == "record":
				assert step.placement is not None
				local = c_name(self._local(struct, step.placement))
				steps.append(f"\tout->{local} = at;")
			elif step.placement is None:
				steps.append(f"\tat = at + {step.size}u;")
			else:
				steps.append("\tat = at + ("
					+ self._length_expression(struct, step.placement,
					                          running="at") + ");")

		kind    = ident(self.prefix, struct.name, "offsets_t")
		build   = ident(self.prefix, struct.name, "offsets")
		fields  = [f"\tuint32_t {c_name(self._local(struct, held))};"
		           for held in dynamic]

		return [
			"",
			f"/* Where each dynamically-placed member of `{struct.name}`"
			" starts.",
			" *",
			" * `_offset` resolves one member by summing what precedes it, so"
			" it",
			" * rescans every delimited member ahead of the one asked for --"
			" on",
			" * every call. This is that sum, once, for all of them.",
			" *",
			" * The memory is the caller's: one word per member below. */",
			f"typedef struct {kind} {{",
			*fields,
			f"}} {kind};",
			"",
			f"static inline void {build}(situ_view_t view, {kind} *out)",
			"{",
			"\tuint32_t at = 0u;",
			"",
			*steps,
			"}",
		]

	def _offset_function(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""Resolve a dynamic offset by summing what precedes it.

		Everything before the first variable-length member contributes a
		constant; each variable-length member contributes its own length, read
		from the field that drives it. That field always has a static offset,
		because a size expression may only name a field declared before it and
		everything before the first dynamic member is statically placed.
		"""
		local    = c_name(self._local(struct, placement))
		constant = 0
		terms    = []

		for other in self._top_level(struct):
			if other.path == placement.path:
				break
			if other.is_fixed_size:
				constant += other.size_bits // BITS_PER_BYTE
				continue
			if not self._has_length(struct, other):
				return self._unresolvable_offset(placement, other)
			# `running="offset"` -- the sum in hand, rather than letting
			# `_span` re-resolve the base by rescanning everything before
			# this member. Without it each term's base is computed from
			# scratch, so this function is not linear in the members before
			# it but in the work of resolving each of them; an eight-member
			# record measured 10.3 seconds where the same reads now take 0.3.
			terms.append(self._length_expression(struct, other,
			                                     running="offset"))

		lines = [
			f"static inline uint32_t "
			f"{ident(self.prefix, struct.name, local, 'offset')}(situ_view_t view)",
			"{",
			f"\tuint32_t offset = {constant}u;",
		]
		# Saturating, because a term is a length the message chose: `hdr.length
		# = 0xffff` put `examples/packet`'s tag 65581 bytes into a 62-byte view
		# and the accessor handed that pointer back, which an address sanitizer
		# stopped three seconds into the first real fuzz run. A wide length
		# field is worse than out of range -- `offset + by` in `uint32_t` wraps
		# to an offset that is *inside* the frame and points at the wrong
		# bytes, which nothing downstream can detect. So every term stops at
		# the view, and `validate` reports the message as malformed (26.27).
		for term in terms:
			lines.append(f"\toffset = situ_advance_u32(offset, ({term}),"
			             " view.limit);")

		if not terms:
			lines.append("\t(void)view;")

		lines.extend(["", "\treturn offset;", "}"])
		return lines

	def _located_accessor(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""A member the data positions, reached through the message.

		`situ_view_t` is `{ base, limit, generation }` and carries no message
		origin -- only `situ_msg_t` knows where offset zero is. So an accessor
		for a located member takes both, which is a different signature from
		every other accessor here and is the honest one: the offset is
		measured from the start of the message, and a view is a window part
		way into it.

		The alternative was a fourth word in every view, growing the core type
		by half for a construct few schemas use. Section 9.8.
		"""
		local  = c_name(self._local(struct, placement))
		offset = self._over_fields(struct, placement.located or "", "view")
		length = (str(placement.size_bits // BITS_PER_BYTE)
		          if placement.is_fixed_size
		          and placement.size_bits % BITS_PER_BYTE == 0
		          else self._length_expression(struct, placement)
		          if self._has_length(struct, placement) else None)
		if length is None:
			return [
				f"/* No accessor for `{placement.name}`: it is placed by"
				f" `{placement.located}`,",
				" * and how long it is is not something this can work out. */",
			]

		return [
			"",
			f"/* `{placement.name}` sits at `{placement.located}` bytes from the",
			" * start of the *message*, not from this view. Both are taken:"
			" the view",
			" * reads the offset field, the message says where zero is.",
			" *",
			" * The offset is the message's, so nothing about this frame says"
			" it is",
			" * inside the buffer. That is checked here, on every call, rather"
			" than",
			" * once at the frame boundary -- which is what `offset ="
			" DataPlaced` in",
			" * the map is telling you it costs. */",
			f"static inline situ_err_t "
			f"{ident(self.prefix, struct.name, local, 'view')}"
			"(const situ_msg_t *msg, situ_view_t view, situ_view_t *out)",
			"{",
			f"\tconst uint32_t at = (uint32_t)({offset});",
			f"\tconst uint32_t n  = (uint32_t)({length});",
			"",
			"\tif (n > msg->size || at > msg->size - n) {",
			"\t\treturn SITU_ERR_BOUNDS;",
			"\t}",
			"\tout->base       = msg->base + at;",
			"\tout->limit      = n;",
			"\tout->generation = msg->generation;",
			"\treturn SITU_OK;",
			"}",
		]

	def _offset_blocker(self, struct: ResolvedStruct,
			placement: Placement) -> Placement | None:
		"""The earlier member whose extent is unknown, if there is one."""
		for other in self._top_level(struct):
			if other.path == placement.path:
				return None
			if other.is_fixed_size:
				continue
			if not self._has_length(struct, other):
				return other
		return None

	def _unresolvable_offset(self, placement: Placement,
			blocker: Placement) -> list[str]:
		"""No offset function, and the reason, where it will be looked for.

		A region whose codec expands by a bounded ratio or without bound has no
		length until it is decoded, so nothing after it has an offset either.
		Emitting arithmetic that ignored that would put every later accessor on
		the wrong bytes, which is the one failure mode worse than a missing
		accessor.
		"""
		return [
			f"/* No accessor for `{placement.name}`: it starts after "
			f"`{blocker.name}`, whose",
			f" * codec `{blocker.type_name}` has no closed-form expansion, so how many",
			" * bytes it occupies is not known until it has been decoded.",
			" *",
			" * A `length_preserving` codec, or one with a fixed or exact-ratio",
			" * expansion, keeps the members after it addressable. */",
		]

	def _fits_check(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""A length the message declares must fit the frame it is in.

		Nothing checked this, so `u8 opts[hdr.length]` with a `u16` length in a
		32-byte frame was a message that parsed clean and handed out a pointer
		to 65535 bytes. The accessor clamps, which is what keeps a caller who
		skips validation memory-safe; this is what tells a caller who does not
		skip it that the message is malformed rather than short.

		The two are different answers and both are needed. Clamping alone
		silently turns a lie into a truncation.
		"""
		if "." in placement.path[len(struct.name) + 1:]:
			return []

		# The other half of the same sentence, one step further on. A member
		# *placed* after a variable-length region has an offset that is a sum
		# of lengths the message chose, so "does the frame contain it" is a
		# question the acquiring bounds check did not answer for it either.
		# `examples/packet`'s tag is sixteen fixed bytes and was 65 kilobytes
		# past a 62-byte view.
		if not declares_its_own_length(placement):
			extent = self._fixed_extent(placement)
			if extent is None or placement.offset_bits is not None:
				return []
			return [
				f"\t/* {placement.path}: its offset is a sum of lengths the"
				" message",
				"\t * chose, so the frame is not known to contain it. The"
				" accessor",
				"\t * answers safely; this is where the message is called"
				" malformed. */",
				f"\tif (!situ_in_bounds(view,"
				f" {self._base_expression(struct, placement)}, {extent}u)) {{",
				"\t\treturn SITU_ERR_BOUNDS;",
				"\t}",
			]

		declared = self._length_expression(struct, placement)
		base     = self._base_expression(struct, placement)
		return [
			f"\t/* {placement.path}: the length the message declares has to fit",
			"\t * the frame it is in. The accessor clamps; this is where a",
			"\t * message that does not fit is called malformed. */",
			f"\tif (situ_remaining_u32(view.limit, {base}) < ({declared})) {{",
			"\t\treturn SITU_ERR_BOUNDS;",
			"\t}",
		]

	def _fixed_extent(self, placement: Placement) -> int | None:
		"""How many bytes this member occupies, where that is a constant.

		A scalar's container, a counted array's run, a fixed-size nested
		struct. None for anything the data sizes, which the branch above
		already covers.
		"""
		if not placement.is_fixed_size:
			return None
		if placement.size_bits % BITS_PER_BYTE:
			return None		# a bit-packed field's container is placed, not it
		return placement.size_bits // BITS_PER_BYTE or None

	def _discriminant_check(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""`default: error` -- the discriminant must select an arm.

		Section 14.5 says an unrecognised discriminant is rejected on parse,
		and until now nothing rejected it. `SITU_ERR_VERSION` has meant
		"unknown version or variant discriminant" since the runtime header was
		written and was returned by nothing.
		"""
		if classify_check(struct, placement, self.structs) \
				is not Check.DISCRIMINANT:
			return []

		values = matched_values(placement)
		if not values or placement.discriminant is None:
			return []

		held = self._over_fields(struct, placement.discriminant, "view")
		test = " && ".join(f"{held} != {arm.value}u" for arm in values)
		named = ", ".join(arm.source or str(arm.value) for arm in values)
		return [
			f"\t/* {placement.path}: an arm for {named}, and"
			f" `default: error` for the rest. */",
			f"\tif ({test}) {{",
			"\t\treturn SITU_ERR_VERSION;",
			"\t}",
		]

	def _variant_length(self, struct: ResolvedStruct, placement: Placement,
			held: str = "view") -> str | None:
		"""How many bytes the selected arm occupies, as one expression.

		A ternary chain rather than a `switch`, because callers want this
		inside a sum -- the extent of the struct around it, or the offset of
		whatever follows. C has no statement-expression that is not an
		extension, and a helper function per variant would need the same
		chain inside it anyway.

		An unmatched discriminant yields zero. That is not a claim that such a
		message is empty; it is the value the walk already refuses to advance
		by, so `default: error` arrives as the run stopping rather than as a
		length nobody can justify.
		"""
		if not placement.arm_cases or placement.discriminant is None:
			return None

		held_disc = self._over_fields(struct, placement.discriminant, held)

		chain = "0u"		# no arm matched
		for arm, member in reversed(arm_members(struct, placement)):
			if member is None:
				continue		# `default: error`; falls to the zero above
			if member.is_fixed_size:
				length = f"{member.size_bits // BITS_PER_BYTE}u"
			elif not self._has_length(struct, member):
				return None
			else:
				length = f"({self._length_expression(struct, member, held)})"

			if arm.value is None:
				chain = length		# `default:` with an arm; matches anything
			else:
				chain = (f"({held_disc} == {arm.value}u"
				         f" ? {length} : {chain})")
		return chain

	def _length_expression(self, struct: ResolvedStruct, placement: Placement,
			held: str = "view", running: str | None = None) -> str:
		"""How many bytes a variable-length member occupies, at runtime.

		`held` names the view in scope, which is `gate.view` inside a sealed
		region: the length is read from a plaintext field at the same offsets,
		through whichever view the caller holds.
		"""
		gated = held != "view"

		# A delimited member's extent is not a closed form: it is wherever the
		# delimiter turns out to be. The member emits its own `_span`, which
		# scans, and everything downstream sums that call rather than trying to
		# inline the search (section 8.6.1).
		if placement.delimiter is not None or placement.repeat_while is not None:
			# One name for "how far this member reaches", whichever kind it
			# is: a byte array's `_span` is its content plus the delimiter, a
			# record run's is its elements plus the terminator, and a `while`
			# run's is its elements.
			local = c_name(self._local(struct, placement))
			span  = ident(self.prefix, struct.name, local, "span")
			# `running` is the offset a caller accumulating them already has.
			# Without it `_span` re-resolves the base by rescanning every
			# member before this one, so a loop over M members costs M^2
			# scans while reading as one pass -- which is what `_required`
			# was doing, and what made the first offset cache slower than
			# the per-call offsets it replaced.
			# Every member kind that reaches here emits the `_from` form: a
			# byte array's scan, a record run's walk and a `while` run's.
			# The runs were the exception until the walks grew the helper,
			# and the exception cost a full rescan of everything before the
			# run on every accumulating pass over it.
			if running is not None:
				return f"{span}_from({held}, {running})"
			return f"{span}({held})"

		# A nested struct with no single size. Its own `_extent` needs a view
		# positioned at the member, which is not something an expression can
		# make -- so the member emits a helper that does, and this calls it.
		# Without this the sum treated the member as zero bytes wide and put
		# whatever follows it at the same offset.
		nested = self.resolved.structs.get(placement.type_name or "")
		if (nested is not None and not nested.layout.is_fixed_size
				and placement.kind == "field"
				and placement.array_count is None
				and placement.sized_by is None
				and placement.delimiter is None):
			assert self._struct_extent(nested), "_has_length checks this first"
			local = c_name(self._local(struct, placement))
			return f"{ident(self.prefix, struct.name, local, 'extent')}({held})"

		if placement.kind == "variant":
			chain = self._variant_length(struct, placement, held)
			assert chain is not None, "callers check _has_length first"
			return chain

		if placement.kind in ("coded", "sealed"):
			length = self._region_length(struct, placement, held)
			assert length is not None, "callers check _has_length first"
			return length

		if placement.kind == "opaque":
			# An opaque region's size expression is already a byte count; there
			# are no elements to multiply by.
			return f"(uint32_t){self._count_expression(struct, placement, held)}"

		# A varint is as long as its own bytes say. Without this it fell to the
		# count branch below, which found no `sized_by` and returned zero -- so
		# everything after a varint was placed as though it occupied nothing.
		if placement.varint is not None:
			local = c_name(self._local(struct, placement))
			return f"{ident(self.prefix, struct.name, local, 'len')}({held})"

		# A size that is arithmetic over a field rather than a bare reference
		# to one. `sized_by` holds a path and holds nothing for this, so the
		# count branch below returned zero -- for `data[(len + 1) * 8 - 2]`,
		# which is a length counted in units and about as common as a length
		# gets.
		if placement.size_expr is not None:
			rendered = self._over_fields(struct, placement.size_expr, held)
			return f"(uint32_t)({rendered})"

		element = (placement.element_bits or BITS_PER_BYTE) // BITS_PER_BYTE

		if placement.sized_by == "remaining":
			# Saturating. A plain subtraction wraps when the members before it
			# claim more than the view holds, which is a length the message
			# chooses rather than one the schema does.
			return (f"situ_remaining_u32({held}.limit, "
			        f"{self._base_expression(struct, placement, gated)})")

		count = self._count_expression(struct, placement, held)
		return f"(uint32_t){count}" if element == 1 else f"(uint32_t){count} * {element}u"

	def _region_length(self, struct: ResolvedStruct, region: Placement,
			held: str = "view") -> str | None:
		"""How many bytes a coded or sealed region occupies, at runtime.

		Its interior extent put through the codec's expansion, which is the
		same rule the solver applies and the only one available: the region's
		bytes are the transform's output, so nothing in them can be read to find
		out how many there are.

		None where the expansion has no closed form -- a bounded ratio or an
		unbounded one -- because then the length genuinely is not computable
		without decoding, and a wrong number here would silently misplace every
		member after it.
		"""
		rule = region_extent(struct, region,
		                     self.codecs.get(region.type_name))
		if rule is None:
			return None

		terms = [f"{rule.constant}u"]
		terms += [f"({self._length_expression(struct, member, held)})"
		          for member in rule.variable]
		inner = " + ".join(terms)

		if rule.kind == "preserving":
			return inner
		if rule.kind == "add":
			return f"({inner}) + {rule.add}u"
		if rule.kind == "ratio":
			return f"(({inner}) * {rule.out}u) / {rule.into}u"
		# Whole groups only, so a partial one still costs a full group.
		return (f"((({inner}) + {rule.group_in - 1}u)"
		        f" / {rule.group_in}u) * {rule.group_out}u")

	def _has_length(self, struct: ResolvedStruct, placement: Placement) -> bool:
		"""Whether a member's runtime extent has a closed form."""
		# A run whose element has no computable extent cannot be walked, so
		# how far it reaches is unknown and nothing after it can be placed.
		# Without this the offset chain called a `_span` that was never
		# emitted, which is a compile error rather than a wrong answer -- but
		# only because C forbids the call, not because anything checked.
		# Whether the element or the nested struct can be measured from its
		# own bytes is a fact about the layout, so it is asked in one place --
		# `gen-checks` needs the same answer to decide whether a check may
		# call an accessor this decided not to emit.
		element = self.resolved.structs.get(placement.type_name or "")
		if element is not None and not element.layout.is_fixed_size:
			nested_member = (placement.kind == "field"
			                 and placement.delimiter is None
			                 and placement.array_count is None
			                 and placement.sized_by is None)
			if (placement.repeat_while is not None
					or self._is_record_run(placement) or nested_member):
				return has_computable_extent(self.resolved.structs, element)

		if placement.kind == "variant":
			return self._variant_length(struct, placement) is not None
		if placement.kind not in ("coded", "sealed"):
			return True
		return self._region_length(struct, placement) is not None

	def _count_expression(self, struct: ResolvedStruct, placement: Placement,
			held: str = "view") -> str:
		"""Read the field that drives a member's length.

		Usually a constant-offset load, but not always: the driving field has to
		precede the member it sizes, which does not mean it precedes every
		dynamic member. A second variable-length region can be counted by a
		field that is itself dynamically placed, so this goes through the same
		base resolution as any other read.
		"""
		if placement.sized_by is None:
			return f"{placement.array_count or 0}u"

		target = self.resolved.find(f"{struct.name}.{placement.sized_by}")
		if target is None:
			return f"{placement.array_count or 0}u"

		# A varint driver has no scalar and no constant offset, so neither the
		# load below nor the check above applies to it. It reached the `scalar
		# is None` guard and returned zero, which made `u8 payload[n]` a
		# zero-length field with a correctly computed offset -- the second of
		# the two silent zeros a varint used to produce.
		if target.placement.varint is not None:
			local = c_name(self._local(struct, target.placement))
			return (f"{ident(self.prefix, struct.name, local, 'value')}"
			        f"({held})")

		if target.placement.scalar is None:
			return f"{placement.array_count or 0}u"

		# A text driver's value is digits, not bits. Loading it with
		# `situ_get_be32` read the ASCII as a big-endian integer, which is the
		# kind of wrong that produces a plausible number: "10" came out as
		# 0x3130.
		if target.placement.radix is not None:
			return self._text_count_expression(struct, target.placement)

		loaded = self._load_expression(
			target.placement.scalar, target.placement,
			self._value_base(struct, target.placement, gated=held != "view"),
			offset=self._value_offset(target.placement))
		return f"({loaded})"

	def _text_count_expression(self, struct: ResolvedStruct,
			driver: Placement) -> str:
		"""A length written as digits, as an expression that cannot fail.

		The parse can fail, and this cannot: an offset function returning an
		error would make every accessor downstream of it fallible, and the
		whole shape of this API is that the checks happen once and the reads
		trust them.

		So the check happens once, in `validate`, which refuses a frame whose
		digits are not digits -- and this reads the same bytes knowing that.
		An unvalidated frame gets zero here, which is the same bargain as
		every other accessor on this header: they trust the bounds check they
		did not perform.
		"""
		local = c_name(self._local(struct, driver))
		return f"{ident(self.prefix, struct.name, local, 'value')}(view)"

	def _text_value_helper(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""The non-failing read, for the offset arithmetic that cannot fail."""
		assert placement.radix is not None
		scalar = placement.scalar
		assert scalar is not None

		local  = c_name(self._local(struct, placement))
		ctype  = self._field_ctype(placement)
		limit  = (1 << scalar.bits) - 1

		return [
			"",
			"/* The same digits, read where an error cannot be returned: the",
			" * offset arithmetic downstream of this field is not fallible, and",
			" * making it so would put an error path in every accessor after it.",
			" *",
			" * `validate` is what makes this safe -- it refuses a frame whose",
			" * digits are not digits, so a validated frame always parses here.",
			" * An unvalidated one reads zero, which is the same bargain every",
			" * other accessor makes with the bounds check it did not do. */",
			f"static inline {ctype} "
			f"{ident(self.prefix, struct.name, local, 'value')}(situ_view_t view)",
			"{",
			"	uint64_t value = 0u;",
			"",
			f"	(void)situ_parse_uint({ident(self.prefix, struct.name, local, 'ptr')}(view),"
			f" {ident(self.prefix, struct.name, local, 'len')}(view),"
			f" {placement.radix}u, {limit}u, &value);",
			f"	return ({ctype})value;",
			"}",
		]

	def _element_view(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""Indexed access to one element of an array of structs.

		Acquiring the element view is the bounds check; the fields inside it are
		then constant offsets from its base (section 12.2). That is what gives a
		dynamically positioned static struct its static capabilities back, and
		why the accessors inside `Record` are the same ones a standalone
		`Record` would have.
		"""
		local  = c_name(self._local(struct, placement))
		nested = placement.type_name
		base   = self._base_expression(struct, placement)
		lines  = []

		if placement.array_count is not None:
			lines.append(f"#define {macro(self.prefix, struct.name, local, 'COUNT')} "
			             f"{placement.array_count}u")
		else:
			lines.extend([
				f"static inline uint32_t "
				f"{ident(self.prefix, struct.name, local, 'count')}(situ_view_t view)",
				"{",
				f"\treturn (uint32_t){self._count_expression(struct, placement)};",
				"}",
			])

		# The element count, not just the message extent. `situ_view_sub` bounds
		# against the view, which is a weaker claim: an array that does not run
		# to the end of its struct has bytes after it that are inside the view
		# and are not elements. Indexing one step past the end used to land on
		# whatever followed and report SITU_OK.
		count = (macro(self.prefix, struct.name, local, "COUNT")
		         if placement.array_count is not None
		         else f"{ident(self.prefix, struct.name, local, 'count')}(view)")

		lines.extend([
			f"static inline situ_err_t "
			f"{ident(self.prefix, struct.name, local, 'at')}"
			"(situ_view_t view, uint32_t index, situ_view_t *out)",
			"{",
			f"\tconst uint32_t stride = {macro(self.prefix, nested, 'SIZE_FIXED')};",
			f"\tconst uint32_t base   = {base};",
			"",
			f"\tif (index >= {count}) {{",
			"\t\treturn SITU_ERR_BOUNDS;",
			"\t}",
			"",
			"\treturn situ_view_sub(view, base + index * stride, stride, out);",
			"}",
		])
		return lines

	def _sub_view(self, struct: ResolvedStruct, placement: Placement) -> list[str]:
		"""A sub-view of a nested struct.

		`SIZE_FIXED` only exists where a struct has one size, so nesting a
		variable one emitted a reference to a macro that was never defined --
		since nested structs and variable structs both existed, and found by
		the first schema that nested one.
		"""
		nested = placement.type_name
		base   = self._base_expression(struct, placement)
		inner  = self.resolved.structs.get(nested)
		name   = ident(self.prefix, struct.name,
		               c_name(self._local(struct, placement)), "view")

		if (inner is not None and not inner.layout.is_fixed_size
				and self._struct_extent(inner)):
			extent = ident(self.prefix, nested, "extent")
			site   = ident(self.prefix, struct.name,
			               c_name(self._local(struct, placement)), "extent")
			return [
				f"/* How many bytes `{placement.name}` occupies here. The",
				" * member after it starts at the end of this, and that was a",
				f" * constant zero until `{nested}` stopped having one size --",
				" * so the next member was placed on top of this one. */",
				f"static inline uint32_t {site}(situ_view_t view)",
				"{",
				"\tsitu_view_t whole;",
				"",
				f"\tif (situ_view_sub(view, {base}, "
				f"situ_remaining_u32(view.limit, {base}), &whole)"
				" != SITU_OK) {",
				"\t\treturn 0u;",
				"\t}",
				f"\treturn {extent}(whole);",
				"}",
				"",
				f"/* `{placement.name}` has no one size, so its extent is read",
				" * from the bytes. The sub-view is taken twice: once over what",
				" * is left, to give the extent function something to measure,",
				" * and once at the size it reports. */",
				f"static inline situ_err_t {name}(situ_view_t view, "
				"situ_view_t *out)",
				"{",
				"\tsitu_view_t whole;",
				"\tsitu_err_t  e;",
				"",
				f"\te = situ_view_sub(view, {base}, "
				f"situ_remaining_u32(view.limit, {base}), &whole);",
				"\tif (e != SITU_OK) {",
				"\t\treturn e;",
				"\t}",
				f"\treturn situ_view_sub(view, {base}, {extent}(whole), out);",
				"}",
			]

		if inner is not None and not inner.layout.is_fixed_size:
			return [
				f"/* No sub-view for `{placement.name}`: one `{nested}` has no",
				" * extent this build can compute, so where it ends is not",
				" * known and nothing after it can be placed. */",
			]

		return [
			f"static inline situ_err_t {name}(situ_view_t view, "
			"situ_view_t *out)",
			"{",
			f"\treturn situ_view_sub(view, {base}, "
			f"{macro(self.prefix, nested, 'SIZE_FIXED')}, out);",
			"}",
		]

	def _array(self, struct: ResolvedStruct, entry: Resolved) -> list[str]:
		placement = entry.placement
		local     = c_name(self._local(struct, placement))
		scalar    = placement.scalar
		count     = placement.array_count
		assert scalar is not None

		gate  = self._gate_type(struct, placement)
		taken = f"{gate} gate" if gate else "situ_view_t view"
		held  = "gate.view" if gate else "view"

		lines = []
		if count is not None:
			lines.append(
				f"#define {macro(self.prefix, struct.name, local, 'COUNT')} {count}u")
		else:
			# A run-time length: the count comes from the field that drives it,
			# and `remaining` measures to the end of the view.
			#
			# Clamped to what the view holds, because the field is the
			# message's. `u8 opts[hdr.length]` with a `u16` length claims up to
			# 65535 bytes, and this returned that number beside a pointer at
			# the frame base -- so a caller reading `ptr(view)[len(view) - 1]`
			# read 65 kilobytes past a 32-byte frame. Section 20.2 amortises
			# the bounds check at the frame boundary, and that argument holds
			# only for offsets the frame is known to contain; a length the
			# message chooses is not one.
			#
			# `validate` is where a malformed message is *reported*. This is
			# what keeps the accessor safe for a caller who did not ask.
			lines.extend([
				f"static inline uint32_t "
				f"{ident(self.prefix, struct.name, local, 'len')}({taken})",
				"{",
				f"\treturn situ_min_u32("
				f"{self._length_expression(struct, placement, held)},",
				f"\t\tsitu_remaining_u32({held}.limit, "
				f"{self._base_expression(struct, placement, gated=gate is not None)}));",
				"}",
			])

		if scalar.bits == BITS_PER_BYTE:
			# A byte array is the bytes: MemoryIdentical, so a pointer is safe
			# and is what callers actually want.
			lines.extend(self._address_note(entry))
			# Unless the member is not there. A counted array at a dynamic
			# offset has a fixed extent and an offset the message chose, so
			# "the frame contains it" is not something the acquiring bounds
			# check established -- `packet.tag` is sixteen bytes after a
			# region a header field sizes, and a `0xffff` there put the
			# pointer past the end of the view.
			fits = (self._fits(struct, placement, count, held,
			                   gate is not None)
			        if count is not None else None)
			body = (f"\treturn {held}.base + "
			        f"{self._base_expression(struct, placement, gated=gate is not None)};"
			        if fits is None else
			        f"\treturn {fits}\n\t\t? {held}.base + "
			        f"{self._base_expression(struct, placement, gated=gate is not None)}"
			        "\n\t\t: NULL;")
			lines.extend([
				*([] if fits is None else [
					"/* NULL where the member does not fit the view: its offset"
					" is a sum",
					" * of lengths the message chose, so the frame is not known"
					" to hold",
					" * it. `validate` reports such a message as malformed"
					" (26.27). */",
				]),
				f"static inline uint8_t *"
				f"{ident(self.prefix, struct.name, local, 'ptr')}({taken})",
				"{",
				body,
				"}",
			])
			lines.extend(self._nul_length(struct, placement, local, taken, held,
			                              gate is not None))
			return lines

		lines.extend([
			f"/* No pointer accessor: the element type is {placement.type_name},",
			" * which is ValueConverted, so a pointer into it would alias bytes",
			" * that are not the value. Index the elements individually. */",
		])
		lines.extend(self._indexed_element(struct, entry, local, scalar))
		return lines

	def _indexed_element(self, struct: ResolvedStruct, entry: Resolved,
			local: str, scalar: ScalarType) -> list[str]:
		placement = entry.placement
		ctype     = self._field_ctype(placement)
		stride    = scalar.bits // BITS_PER_BYTE
		getter    = ident(self.prefix, struct.name, local, "get")

		base = self._base_expression(struct, placement)
		load = self._load_expression(
			scalar, placement, f"view.base + {base} + index * {stride}u", offset=0)
		return [
			f"static inline {ctype} {getter}(situ_view_t view, uint32_t index)",
			"{",
			f"\treturn ({ctype})({load});",
			"}",
		]

	# -- scalars --------------------------------------------------------

	def _versioned_get(self, struct: ResolvedStruct, placement: Placement,
			ctype: str, taken: str, load: str) -> list[str]:
		"""A member present only from a given version (section 19.4).

		Out-parameter and an error, because there is no value to return when
		the field is not there. A getter returning whatever follows would hand
		back the bytes of some later member, or of another message entirely --
		which is the bug this construct exists to make impossible, so it is
		not available to write.

		The offset is still a constant. `[since]` is append-only, so nothing
		before this member can move; what varies is only whether the bytes are
		present, and that is one comparison against a field at a known offset.
		"""
		assert placement.since is not None and placement.version_field is not None

		local   = c_name(self._local(struct, placement))
		version = ident(self.prefix, struct.name,
		                c_name(placement.version_field), "get")

		return [
			f"/* Present from version {placement.since}. Reading it from an "
			f"earlier message",
			" * would return the bytes of whatever follows, so this reports"
			" rather",
			" * than guesses. The offset is a constant either way: `[since]` is",
			" * append-only, so nothing ahead of this member can move. */",
			f"static inline situ_err_t "
			f"{ident(self.prefix, struct.name, local, 'get')}"
			f"({taken}, {ctype} *out)",
			"{",
			f"	if ({version}(view) < {placement.since}u) {{",
			"		return SITU_ERR_VERSION;",
			"	}",
			f"	*out = ({ctype})({load});",
			"	return SITU_OK;",
			"}",
		]

	def _scalar_get(self, struct: ResolvedStruct, entry: Resolved) -> list[str]:
		placement = entry.placement
		scalar    = placement.scalar
		if scalar is None:
			return []

		local  = c_name(self._local(struct, placement))
		ctype  = self._field_ctype(placement)
		getter = ident(self.prefix, struct.name, local, "get")
		gate   = self._gate_type(struct, placement)
		taken  = f"{gate} gate" if gate else "situ_view_t view"
		base   = self._value_base(struct, placement, gated=gate is not None)
		load   = self._load_expression(scalar, placement, base,
		                               offset=self._value_offset(placement))

		# A scalar whose offset the message decides answers zero where it does
		# not fit, rather than reading whatever is at that address. The value
		# is wrong either way; one of the two is also a read past the frame.
		fits = self._fits(struct, placement, self._scalar_bytes(scalar),
		                  "gate.view" if gate else "view", gate is not None)

		if placement.since is not None:
			lines = self._versioned_get(struct, placement, ctype, taken, load)
		elif fits is not None:
			lines = [
				f"/* Zero where the member does not fit the view: its offset is"
				" a sum of",
				" * lengths the message chose, so the frame is not known to"
				" hold it, and",
				" * `validate` reports such a message as malformed"
				" (26.27). */",
				f"static inline {ctype} {getter}({taken})",
				"{",
				f"\treturn {fits}",
				f"\t\t? ({ctype})({load})",
				f"\t\t: ({ctype})0;",
				"}",
			]
		else:
			lines = [
				f"static inline {ctype} {getter}({taken})",
				"{",
				f"\treturn ({ctype})({load});",
				"}",
			]

		# A pointer accessor is offered only where the value is literally the
		# bytes. Section 20.2: never hand out a pointer into a converted field.
		if (entry.vector.get(Axis.REPR).base == "MemoryIdentical"
				and not scalar.is_bit_packed
				and placement.kind != "marker"
				and placement.type_name not in self.enums):
			lines.extend(self._address_note(entry))
			lines.extend([
				f"static inline {self._ctype(scalar)} *"
				f"{ident(self.prefix, struct.name, local, 'ptr')}({taken})",
				"{",
				f"\treturn ({self._ctype(scalar)} *)"
				f"({'gate.view.base' if gate else 'view.base'} + "
				f"{self._base_expression(struct, placement, gated=gate is not None)});",
				"}",
			])

		return lines

	def _scalar_set(self, struct: ResolvedStruct, entry: Resolved) -> list[str]:
		placement = entry.placement
		scalar    = placement.scalar
		if scalar is None:
			return []

		refusal = self._setter_refusal(entry)
		if refusal is not None:
			return refusal

		# A field that drives a length gets one setter, not two: writing it
		# moves later members, so it has to bump the generation. The shifting
		# setter at the end of this struct's section is that one.
		local_path = self._local(struct, placement)
		if local_path in self._drivers(struct):
			return [
				f"/* No plain setter: `{local_path}` decides where later members",
				" * start, so writing it must invalidate outstanding views. Use",
				f" * {ident(self.prefix, struct.name, c_name(local_path), 'set')}(),",
				" * which takes the message and bumps its generation. */",
			]

		# A covered field gets one setter, not two: writing it leaves something
		# stale, so it has to mark the message dirty. The coverage-aware setter
		# at the end of this struct's section is that one.
		if placement.covered_by:
			return [
				f"/* No plain setter: `{placement.name}` is covered by "
				f"{', '.join(placement.covered_by)}, so",
				f" * writing it leaves {self._coverage_noun(struct, placement)}"
				" stale. Use",
				f" * {ident(self.prefix, struct.name, c_name(self._local(struct, placement)), 'set')}(),",
				" * which takes the message and marks the bit. */",
			]

		local = c_name(self._local(struct, placement))
		ctype = self._field_ctype(placement)
		setter = ident(self.prefix, struct.name, local, "set")

		base  = self._value_base(struct, placement)
		store = self._store_statement(scalar, placement, base, "value",
		                              offset=self._value_offset(placement))

		# A versioned field is not there to be written in an older message,
		# and writing it anyway puts these bytes past the end of that message
		# -- into whatever the caller's buffer holds next. The getter refused
		# this from the start and the setter did not, which is the asymmetry
		# worth naming: reading the wrong bytes is a wrong answer, and writing
		# them is somebody else's data.
		if placement.since is not None and placement.version_field is not None:
			version = ident(self.prefix, struct.name,
			                c_name(placement.version_field), "get")
			return [
				f"/* Present from version {placement.since}. Writing it to an "
				f"earlier message",
				" * would put these bytes past that message's end, so this"
				" refuses. */",
				f"static inline situ_err_t {setter}(situ_view_t view, "
				f"{ctype} value)",
				"{",
				f"\tif ({version}(view) < {placement.since}u) {{",
				"\t\treturn SITU_ERR_VERSION;",
				"\t}",
				f"\t{store}",
				"\treturn SITU_OK;",
				"}",
			]

		# A write at an offset the message chose is the same hole as a read,
		# and worse: reading past the frame is a wrong answer and writing past
		# it is somebody else's data. The setter has no error channel, so it
		# does nothing -- and `validate` is where the caller learns why.
		fits = self._fits(struct, placement, self._scalar_bytes(scalar))
		if fits is not None:
			return [
				"/* Does nothing where the member does not fit the view: its"
				" offset is a",
				" * sum of lengths the message chose, and writing past the"
				" frame is",
				" * somebody else's data. `validate` reports such a message"
				" (26.27). */",
				f"static inline void {setter}(situ_view_t view, {ctype} value)",
				"{",
				f"\tif ({fits}) {{",
				f"\t\t{store}",
				"\t}",
				"}",
			]

		return [
			f"static inline void {setter}(situ_view_t view, {ctype} value)",
			"{",
			f"\t{store}",
			"}",
		]

	def _address_note(self, entry: Resolved) -> list[str]:
		"""Say how long a pointer stays good.

		The pointer is valid now; what varies is what invalidates it. Section
		11.1's address axis exists to answer exactly that, so the answer belongs
		beside the accessor rather than only in the map.
		"""
		address = entry.vector.get(Axis.ADDRESS).base

		if address == "Stable":
			return []

		if address == "FrameStable":
			return ["/* The returned pointer is valid while the enclosing frame's",
			        " * base does not move. */"]

		return ["/* The returned pointer is invalidated by any write that changes",
		        " * the length of what precedes this field; see the INVALIDATION",
		        " * note on this struct's view accessor. */"]

	def _setter_refusal(self, entry: Resolved) -> list[str] | None:
		"""Explain a missing setter, in the header, where it will be looked for.

		A missing setter with no explanation is hostile (project.md section 4).
		The text is the same blame chain `situc explain` prints.
		"""
		mutate = entry.vector.get(Axis.MUTATE)
		if mutate.base in ("InPlaceFixed", "InPlaceSlack"):
			return None

		lines = [f"/* No setter: mutate is {mutate.render()}."]
		for weakening in entry.blame(Axis.MUTATE):
			lines.append(f" *   caused by: {weakening.rule.construct}")
			lines.append(f" *              {weakening.effect.because}")
			if weakening.rule.remedy:
				lines.append(f" *   remedy:    {weakening.rule.remedy}")
		lines.append(" */")
		return lines

	def _load_expression(self, scalar: ScalarType, placement: Placement,
			base: str, offset: int | None = None) -> str:
		"""The value the field means, which is not always the bits it holds.

		BCD is the case where those differ by more than byte order: the nibbles
		are digits, so reading the number is a decode. Everything else falls
		through to the raw load unchanged.
		"""
		raw = self._raw_load(scalar, placement, base, offset)
		if scalar.is_bcd:
			return f"situ_bcd_decode((uint64_t){raw}, {scalar.digits}u)"
		return raw

	def _raw_load(self, scalar: ScalarType, placement: Placement,
			base: str, offset: int | None = None) -> str:
		offset = placement.offset_bytes if offset is None else offset

		if placement.marker is not None and not scalar.is_bit_packed \
				and scalar.bits > BITS_PER_BYTE:
			return self._conditional_load(scalar, placement, base)

		if scalar.is_bit_packed:
			order = "lsb" if placement.bit_order is ast.BitOrder.LSB_FIRST else "msb"
			raw   = (f"situ_bits_get_{order}({base}, {placement.offset_bits}u, "
			         f"{scalar.bits}u)")
			if scalar.signed:
				return f"({self._ctype(scalar)})situ_sign_extend({raw}, {scalar.bits}u)"
			return f"({self._ctype(scalar)}){raw}"

		if scalar.bits == BITS_PER_BYTE:
			cast = self._ctype(scalar)
			return f"({cast})({base})[{offset}u]"

		suffix = _order_suffix(placement.endian)

		width = scalar.bits
		if width in WORD_WIDTHS:
			raw = f"situ_get_{suffix}{width}({base} + {offset}u)"
			return f"({self._ctype(scalar)}){raw}" if scalar.signed else raw

		# Non-word whole-byte widths (u24, u48) go through the bit path, which
		# handles any width without a special case.
		assembly = _bit_assembly(placement.endian)
		raw = f"situ_bits_get_{assembly}({base}, {placement.offset_bits}u, {width}u)"
		if scalar.signed:
			return f"({self._ctype(scalar)})situ_sign_extend({raw}, {width}u)"
		return f"({self._ctype(scalar)}){raw}"

	def _conditional_load(self, scalar: ScalarType, placement: Placement,
			base: str) -> str:
		"""A parse-time branch on the marker.

		The branch is on a public, layout-irrelevant value, so it is not a side
		channel (section 11.1).
		"""
		predicate = self._marker_predicate(placement)
		width     = scalar.bits
		offset    = placement.offset_bytes
		if width in WORD_WIDTHS:
			return (f"{predicate} ? situ_get_le{width}({base} + {offset}u)"
			        f" : situ_get_be{width}({base} + {offset}u)")

		bits = placement.offset_bits
		return (f"{predicate} ? situ_bits_get_lsb({base}, {bits}u, {width}u)"
		        f" : situ_bits_get_msb({base}, {bits}u, {width}u)")

	def _marker_predicate(self, placement: Placement) -> str:
		owner = placement.path.partition(".")[0]
		return ident(self.prefix, owner, placement.marker or "", "is_little") + "(view)"

	def _store_statement(self, scalar: ScalarType, placement: Placement,
			base: str, value: str, offset: int | None = None) -> str:
		"""Store the value the caller means, in the encoding the wire wants."""
		if scalar.is_bcd:
			value = f"situ_bcd_encode((uint64_t){value}, {scalar.digits}u)"
		return self._raw_store(scalar, placement, base, value, offset)

	def _raw_store(self, scalar: ScalarType, placement: Placement,
			base: str, value: str, offset: int | None = None) -> str:
		offset = placement.offset_bytes if offset is None else offset

		if placement.marker is not None and not scalar.is_bit_packed \
				and scalar.bits > BITS_PER_BYTE:
			width     = scalar.bits
			predicate = self._marker_predicate(placement)
			if width in WORD_WIDTHS:
				return (f"if ({predicate}) {{\n"
				        f"\t\tsitu_put_le{width}({base} + {offset}u, "
				        f"(uint{width}_t){value});\n"
				        f"\t}} else {{\n"
				        f"\t\tsitu_put_be{width}({base} + {offset}u, "
				        f"(uint{width}_t){value});\n"
				        f"\t}}")
			bits = placement.offset_bits
			return (f"if ({predicate}) {{\n"
			        f"\t\tsitu_bits_set_lsb({base}, {bits}u, {width}u, "
			        f"(uint64_t){value});\n"
			        f"\t}} else {{\n"
			        f"\t\tsitu_bits_set_msb({base}, {bits}u, {width}u, "
			        f"(uint64_t){value});\n"
			        f"\t}}")

		if scalar.is_bit_packed:
			order = "lsb" if placement.bit_order is ast.BitOrder.LSB_FIRST else "msb"
			return (f"situ_bits_set_{order}({base}, {placement.offset_bits}u, "
			        f"{scalar.bits}u, (uint64_t){value});")

		if scalar.bits == BITS_PER_BYTE:
			return f"({base})[{offset}u] = (uint8_t){value};"

		suffix = _order_suffix(placement.endian)

		width = scalar.bits
		if width in WORD_WIDTHS:
			return (f"situ_put_{suffix}{width}({base} + {offset}u, "
			        f"(uint{width}_t){value});")

		assembly = _bit_assembly(placement.endian)
		return (f"situ_bits_set_{assembly}({base}, {placement.offset_bits}u, "
		        f"{width}u, (uint64_t){value});")

	# -- validation -----------------------------------------------------

	def _delimiter_check(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""A delimited member whose delimiter is missing is a truncated frame.

		The other half of section 8.6.1 -- that the content may not contain the
		delimiter -- is not checked here, because there is nothing to check:
		the scan stops at the first occurrence, so a parsed member's content
		cannot contain one. It holds by construction, which is stronger than a
		check. Writing is the direction where it can be broken, and a delimited
		member is written through its pointer, so the header says so where a
		pointer note already says the rest.
		"""
		local = c_name(self._local(struct, placement))
		delim = placement.delimiter
		assert delim is not None

		if self._is_record_run(placement):
			# A run's terminator is checked by the walk that finds it, and a
			# run that ran out of buffer has no elements to be wrong about --
			# `_count` simply stops. Validating each element is a cost the
			# caller chooses, as it is for any other array of structs.
			return [
				f"\t/* `{placement.name}` is a run of records. Whether its",
				f"\t * terminator was reached is `{ident(self.prefix, struct.name, local, 'span')}`'s",
				"\t * answer, and validating each element is the caller's",
				"\t * choice, as with any other array of structs. */",
			]

		lines = [
			f"\t/* `{placement.name}` runs to {render_delimiter(delim)}, and a",
			"\t * frame that does not contain it was cut short: the member ran",
			"\t * to the end of the buffer rather than to its own end. */",
			f"\tif (!{ident(self.prefix, struct.name, local, 'terminated')}(view)) {{",
			"\t\treturn SITU_ERR_CONSTRAINT;",
			"\t}",
		]

		if placement.radix is not None:
			# A text number's digits are a constraint like any other, so parse
			# refuses them here rather than leaving every caller of the getter
			# to be the first to find out.
			ctype = self._field_ctype(placement)
			if placement.radix_minimal:
				lines.extend([
					"",
					"\t/* `[minimal]`: one spelling per value, so a leading zero",
					"\t * is a second way to write a number that already has one. */",
					f"\tif (!situ_digits_minimal("
					f"{ident(self.prefix, struct.name, local, 'ptr')}(view),",
					f"\t\t\t{ident(self.prefix, struct.name, local, 'len')}(view),"
					f" {placement.radix}u)) {{",
					"\t\t return SITU_ERR_CONSTRAINT;",
					"\t}",
				])

			lines.extend([
				"",
				f"\t/* And its digits have to be digits, in range. */",
				"\t{",
				f"\t\t{ctype} parsed;",
				f"\t\tsitu_err_t e = "
				f"{ident(self.prefix, struct.name, local, 'get')}(view, &parsed);",
				"",
				"\t\tif (e != SITU_OK) {",
				"\t\t\treturn e;",
				"\t\t}",
				"\t}",
			])

		return lines

	def _validate_decl(self, struct: ResolvedStruct) -> list[str]:
		return [
			"",
			"/* Check every constraint this schema states: [must_eq], [max],",
			" * [min], and the reserved-bit policy. Called on parse under",
			" * SITU_CHECKED, and available explicitly in any build. */",
			f"situ_err_t {ident(self.prefix, struct.name, 'validate')}(situ_view_t view);",
		]

	def source(self) -> str:
		lines = [
			*self._banner(),
			f'#include "{self.basename}.h"',
			"",
		]

		for struct in self.resolved.structs.values():
			if struct.layout.is_byte_sized:
				lines.extend(self._validate_body(struct))

		return "\n".join(lines) + "\n"

	def _validate_body(self, struct: ResolvedStruct) -> list[str]:
		lines = [
			f"situ_err_t {ident(self.prefix, struct.name, 'validate')}(situ_view_t view)",
			"{",
		]

		checks = []
		for entry in struct.entries:
			checks.extend(self._checks_for(struct, entry))

		# A check may be a comment and nothing else -- an array of structs is
		# noted rather than walked -- and a body of comments still leaves the
		# parameter unused, which is an error under the project's own flags.
		reads_view = any(
			line.strip() and not line.strip().startswith(("/*", "*", "//"))
			for line in checks)

		if not reads_view:
			lines.append("\t(void)view;")
		lines.extend(checks)

		lines.extend(["\treturn SITU_OK;", "}", ""])
		return lines

	def _checks_for(self, struct: ResolvedStruct, entry: Resolved) -> list[str]:
		"""Everything `validate` says about one member.

		The bounds question comes first and does not replace the rest: a
		member can be both out of the frame and constrained, and returning
		early on the first left `[must_eq]` unchecked for every dynamically
		placed field the moment the offset check landed.
		"""
		return [*self._fits_check(struct, entry.placement),
		        *self._member_checks(struct, entry)]

	def _member_checks(self, struct: ResolvedStruct,
			entry: Resolved) -> list[str]:
		placement = entry.placement
		scalar    = placement.scalar

		# An element's own members are checked under the element's struct, not
		# here -- the same rule the delimiter case applies below, and the same
		# reason: the discriminant is a field of `label`, not of the struct
		# holding a run of them, so this emitted a comparison against a name
		# that is not in scope.
		if placement.kind == "variant":
			if "." in placement.path[len(struct.name) + 1:]:
				return []
			return self._discriminant_check(struct, placement)

		# A delimited member's delimiter has to be there. That is the one thing
		# parse can check about it: the content cannot contain the delimiter,
		# but not because anything looks -- the scan stops at the first one, so
		# it holds by construction. A missing delimiter is different, and is
		# the truncated-frame case.
		#
		# An element's own members are checked under the element's struct, not
		# here. They appear in this struct's entries under a dotted path
		# because the map names them, and carrying the delimiter through so
		# the map reads consistently brought them into this loop as well.
		if placement.delimiter is not None:
			if "." in placement.path[len(struct.name) + 1:]:
				return []
			return self._delimiter_check(struct, placement)

		# A struct-typed member carries its own constraints, and they are not
		# this function's to restate: delegate to the type's own validator.
		# Without this the enclosing struct validated nothing at all, so a
		# `Packet` whose `hdr.version` was wrong parsed clean -- which is the
		# bug `gen-checks` found on its first run.
		if scalar is None and placement.type_name in self.structs:
			if self._offset_blocker(struct, placement) is not None:
				return []		# no sub-view was emitted to validate through
			nested = self.resolved.structs.get(placement.type_name)
			if (nested is not None and not nested.layout.is_fixed_size
					and not self._struct_extent(nested)):
				return []
			return self._nested_validation(struct, placement)

		# A reserved array is still a constraint. Skipping it left `reserved
		# u8[3]` unchecked in the one example where it matters most: those
		# three bytes sit inside an authenticated region, so a receiver that
		# ignores them lets a sender vary bytes the format calls fixed without
		# disturbing the tag. Section 8.8 calls that malleability control and
		# it is the reason reserved fields are a constraint rather than a
		# comment.
		if scalar is not None and placement.array_count is not None \
				and placement.kind == "reserved":
			return self._reserved_array_check(struct, placement, scalar)

		# `[encoding]` is a claim about what the bytes are. Section 8.6 offers
		# it, and it was accepted and dropped on the floor until now: a schema
		# could declare a field ASCII and the generated code would neither
		# check it nor record it.
		if scalar is not None and placement.array_count is not None \
				and _has_attr(placement.attrs, "encoding"):
			return self._encoding_check(struct, placement, scalar)

		if scalar is not None and placement.array_count is not None \
				and _has_attr(placement.attrs, "nul_terminated"):
			return self._nul_check(struct, placement, scalar)

		if scalar is None or placement.array_count is not None:
			return []
		if placement.kind == "marker":
			return []
		if "." in placement.path[len(struct.name) + 1 :]:
			return []

		local  = c_name(self._local(struct, placement))
		lines  = []

		if placement.kind == "reserved":
			policy = _reserved_policy(placement.attrs)
			if policy in ("must_be_zero", "must_be_one"):
				expect = "0" if policy == "must_be_zero" else _all_ones(scalar.bits)
				read   = self._load_expression(scalar, placement, "view.base")
				lines.extend([
					f"\t/* reserved {placement.type_name} [{policy}] */",
					f"\tif ({read} != {expect}) {{",
					"\t\treturn SITU_ERR_CONSTRAINT;",
					"\t}",
				])
			return lines

		getter = ident(self.prefix, struct.name, local, "get")
		env    = self.resolved.layout.env

		# An enum with `default = error` admits its members and nothing else.
		# The declaration said so all along; this is what makes it true.
		enum = self.enums.get(placement.type_name or "")
		if enum is not None \
				and enum.effective_default is ast.EnumDefault.ERROR:
			lines.extend([
				f"\t/* {placement.path}: `{enum.name}` rejects unknown values"
				f" (section 8.7) */",
				f"\tif (!{ident(self.prefix, enum.name)}_is_known({getter}(view))) {{",
				"\t\treturn SITU_ERR_CONSTRAINT;",
				"\t}",
			])

		# A BCD field can hold a bit pattern that is not a number: a nibble
		# above nine. The getter cannot report that -- it returns a number
		# either way -- so parsing is where it has to be caught.
		if scalar.is_bcd:
			raw = self._raw_load(scalar, placement, "view.base")
			lines.extend([
				f"\t/* {placement.path}: every nibble must be a decimal digit */",
				f"\tif (!situ_bcd_valid((uint64_t){raw}, {scalar.digits}u)) {{",
				"\t\treturn SITU_ERR_CONSTRAINT;",
				"\t}",
			])

		for attr in placement.attrs:
			if attr.name not in ("must_eq", "max", "min") or attr.value is None:
				continue

			from situc.expr import evaluate
			expected = evaluate(attr.value, env)
			operator = {"must_eq": "!=", "max": ">", "min": "<"}[attr.name]
			lines.extend([
				f"\t/* {placement.path} [{attr.name} = {expected}] */",
				f"\tif ({getter}(view) {operator} {expected}) {{",
				"\t\treturn SITU_ERR_CONSTRAINT;",
				"\t}",
			])

		return lines

	def _nul_length(self, struct: ResolvedStruct, placement: Placement,
			local: str, taken: str, held: str, gated: bool) -> list[str]:
		"""How much of a nul-terminated field is content.

		The capacity is a constant the header already carries; this is the
		other number, and the one a caller has to compute by hand otherwise.
		Bounded by the capacity, so an unterminated field reports the whole
		thing rather than running off the end -- `validate` is what refuses
		that, and a getter is not the place to discover it.
		"""
		if placement.array_count is None \
				or not _has_attr(placement.attrs, "nul_terminated"):
			return []

		base = self._base_expression(struct, placement, gated=gated)
		return [
			"",
			f"/* Content length: up to the first zero byte, or"
			f" {placement.array_count} if there is none. */",
			f"static inline uint32_t "
			f"{ident(self.prefix, struct.name, local, 'len')}({taken})",
			"{",
			f"\treturn situ_nul_len({held}.base + {base},"
			f" {placement.array_count}u);",
			"}",
		]

	def _nul_check(self, struct: ResolvedStruct, placement: Placement,
			scalar: ScalarType) -> list[str]:
		"""A field declared nul-terminated must actually carry a terminator.

		The declared size is the capacity, so a field with no zero byte in it
		is not a short string -- it is a field whose content runs off the end,
		and every reader of it would have to guess where to stop.
		"""
		if placement.offset_bits is None or scalar.bits != BITS_PER_BYTE:
			return []

		count = placement.array_count or 0
		return [
			f"\t/* {placement.path} [nul_terminated]: the terminator must be"
			f" within the field */",
			f"\tif (!situ_nul_terminated((view.base) + {placement.offset_bytes}u,"
			f" {count}u)) {{",
			"\t\treturn SITU_ERR_CONSTRAINT;",
			"\t}",
		]

	def _encoding_check(self, struct: ResolvedStruct, placement: Placement,
			scalar: ScalarType) -> list[str]:
		"""Text declared as an encoding must actually be in it.

		Strict, because RFC 3629 is: an overlong form or a surrogate half is a
		second spelling of a character that already has one, and accepting both
		means two byte sequences encode one value. That is the malleability
		problem of section 8.8 wearing different clothes.
		"""
		encoding = next((attr for attr in placement.attrs
		                 if attr.name == "encoding"), None)
		if encoding is None or placement.offset_bits is None:
			return []
		if scalar.bits != BITS_PER_BYTE:
			return []

		named = getattr(encoding.value, "name", None)
		if named not in ("ascii", "utf8"):
			return []

		count = placement.array_count or 0
		return [
			f"\t/* {placement.path} [encoding = {named}] */",
			f"\tif (!situ_{named}_valid((view.base) + {placement.offset_bytes}u,"
			f" {count}u)) {{",
			"\t\treturn SITU_ERR_CONSTRAINT;",
			"\t}",
		]

	def _reserved_array_check(self, struct: ResolvedStruct, placement: Placement,
			scalar: ScalarType) -> list[str]:
		"""Every element of a reserved array must hold the required pattern."""
		policy = _reserved_policy(placement.attrs)
		if policy not in ("must_be_zero", "must_be_one"):
			return []
		if placement.offset_bits is None or scalar.bits != BITS_PER_BYTE:
			return []

		count  = placement.array_count or 0
		expect = "0u" if policy == "must_be_zero" else "0xFFu"
		base   = placement.offset_bytes

		return [
			f"\t/* reserved {placement.type_name}[{count}] [{policy}] */",
			"\t{",
			"\t\tuint32_t i;",
			"",
			f"\t\tfor (i = 0; i < {count}u; i++) {{",
			f"\t\t\tif ((view.base)[{base}u + i] != {expect}) {{",
			"\t\t\t\treturn SITU_ERR_CONSTRAINT;",
			"\t\t\t}",
			"\t\t}",
			"\t}",
		]

	def _nested_validation(self, struct: ResolvedStruct,
			placement: Placement) -> list[str]:
		"""Validate a struct-typed member through its own validator.

		Only a single member, not an array of them: validating every element of
		a counted array is a loop whose cost is not obvious from the call, and
		`validate` is called on every parse. An array of structs with
		constraints inside is worth saying out loud rather than quietly paying
		for.
		"""
		if "." in placement.path[len(struct.name) + 1 :]:
			return []		# reached through its parent, not directly
		if placement.kind != "field":
			# Only a plain member has a sub-view accessor to delegate through.
			# An element describes a whole array at once, and a region -- an
			# `indexed` table, an `opaque` span -- is reached by machinery that
			# is not a sub-view.
			return []

		local  = c_name(self._local(struct, placement))
		nested = placement.type_name

		# The same predicate the accessors use: an array is a placement with an
		# element entry beside it, which is not the same as one with a count.
		if self._is_array(placement):
			return [
				f"\t/* {placement.path} is an array of `{nested}`. Its elements are",
				"\t * not validated here: that is a loop on every parse, and a",
				"\t * caller who wants it should walk the elements and call",
				f"\t * {ident(self.prefix, nested, 'validate')}() on each. */",
			]

		return [
			f"\t/* {placement.path} : {nested} -- its own constraints */",
			"\t{",
			"\t\tsitu_view_t nested;",
			f"\t\tsitu_err_t err = {ident(self.prefix, struct.name, local, 'view')}"
			"(view, &nested);",
			"",
			"\t\tif (err != SITU_OK) {",
			"\t\t\treturn err;",
			"\t\t}",
			f"\t\terr = {ident(self.prefix, nested, 'validate')}(nested);",
			"\t\tif (err != SITU_OK) {",
			"\t\t\treturn err;",
			"\t\t}",
			"\t}",
		]

	# -- helpers --------------------------------------------------------

	def _local(self, struct: ResolvedStruct, placement: Placement) -> str:
		return placement.path[len(struct.name) + 1 :]

	def _field_ctype(self, placement: Placement) -> str:
		"""The C type a field's accessors use.

		An enum field exposes its typedef rather than the backing width: the
		backing type is mandatory so that the layout is fixed (section 8.7), not
		so that callers have to remember which width it was.
		"""
		if placement.type_name in self.enums:
			return f"{ident(self.prefix, placement.type_name)}_t"

		scalar = placement.scalar
		assert scalar is not None
		return self._ctype(scalar)

	def _ctype(self, scalar: ScalarType) -> str:
		if scalar.kind is ScalarKind.FLOAT:
			return {16: "uint16_t", 32: "float", 64: "double"}[scalar.bits]

		width = _storage_width(scalar.bits)
		return f"int{width}_t" if scalar.signed else f"uint{width}_t"


def _storage_width(bits: int) -> int:
	for width in WORD_WIDTHS:
		if bits <= width:
			return width
	return 64


def _all_ones(bits: int) -> str:
	return f"0x{(1 << bits) - 1:X}u"


def _has_attr(attrs: tuple[ast.Attr, ...], name: str) -> bool:
	return any(attr.name == name for attr in attrs)


def _reserved_policy(attrs: tuple[ast.Attr, ...]) -> str:
	"""Reserved behaviour, defaulting to must_be_zero (section 8.8).

	The default is deliberate: every ignored bit is a malleability surface, so
	the safe option is the silent one.
	"""
	for attr in attrs:
		if attr.name in ("must_be_zero", "must_be_one", "preserve", "unknown"):
			return attr.name
	return "must_be_zero"


def _order_suffix(endian: ast.Endian | None) -> str:
	"""Which byte-order helper a scalar accessor calls.

	`native` resolves to `ne`, whose host-order branch the *C* compiler folds.
	It deliberately does not resolve here: situc runs on the machine building
	the code, which is not the machine running it, and baking one order into
	the output would be wrong for every cross build without saying so.
	"""
	if endian is ast.Endian.NATIVE:
		return "ne"
	return "le" if endian is ast.Endian.LITTLE else "be"


def _bit_assembly(endian: ast.Endian | None) -> str:
	"""The same choice for widths that are not a word: u24, u48."""
	if endian is ast.Endian.NATIVE:
		return "ne"
	return "lsb" if endian is ast.Endian.LITTLE else "msb"

