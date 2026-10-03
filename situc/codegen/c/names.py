"""C identifier construction, and the one hazard it carries.

A schema path is hierarchical and a C identifier is not, so every generated
name is a path flattened with underscores. That flattening is not injective:
`A.b_c` and `A_b.c` reach the same identifier, and so do an enum named `a_b`
and the sealed region `b` of a struct `a`. Left alone, the first sign of it is
the C compiler rejecting generated code, with a diagnostic that names a
function nobody wrote and no source location in the schema at all.

So the flattening is checked here, before anything is emitted. Nothing about
this is specific to how a schema spells its names: the check fires on two
constructs that collide, whatever convention either of them follows.
"""

from __future__ import annotations

from dataclasses import dataclass

from situc.diagnostics import Diagnostic, Label, Severity, SituError, Span
from situc.resolve import ResolvedSchema
import re

from situc.wellformed import CPP_KEYWORDS

#: Words a *bare* generated identifier may not be.
#:
#: The C++ set, which is where the hazard mostly lives, plus the C spellings
#: it does not contain. Taken from `wellformed` rather than restated, because
#: two lists of keywords are one list that has drifted -- and the same set has
#: to decide both what an enum member may be called and how a struct member is
#: spelled, or a schema passes the front end and fails the compiler.
KEYWORDS = CPP_KEYWORDS | frozenset({
	"restrict", "typeof", "_Atomic", "_Bool", "_Generic",
})


#: Every name `runtime/c/situ.h` defines, which a generated one may not be.
#:
#: The paragraph opening this module describes the hazard and the check below
#: covered one population of it: two CONSTRUCTS flattening to one identifier.
#: A construct colliding with the RUNTIME has the same symptom in the same
#: words -- the C compiler rejecting generated code, with a diagnostic that
#: names a function nobody wrote and no source location in the schema at all
#: -- and nothing looked. `struct bounds` generates `situ_bounds_check`, and
#: the runtime has defined `situ_bounds_check(view, off, ext)` all along
#: (26.562).
#:
#: Carried here rather than read from the header at generation time. It
#: installs to `<prefix>/include/situ.h`, which situc would have to guess,
#: and a generator that needs its own runtime on disk to emit code fails
#: wherever the runtime is packaged apart from it.
#: `test_the_runtime_symbols_are_the_runtime_s` reads the header and compares,
#: which is the half a literal needs: a list nobody checks is a list that has
#: drifted.
RUNTIME_SYMBOLS = frozenset({
	"SITU_ALWAYS_INLINE", "SITU_H", "SITU_HOST_BIG", "SITU_LEAF_MAX",
	"SITU_NO_BYTE", "SITU_STALE", "SITU_VERSION_MAJOR",
	"SITU_VERSION_MINOR", "situ_adler32", "situ_advance_u32",
	"situ_align_up_u32", "situ_ascii_ci_eq", "situ_ascii_fold",
	"situ_ascii_valid", "situ_base", "situ_bcd_decode",
	"situ_bcd_encode", "situ_bcd_valid", "situ_bits_get_lsb",
	"situ_bits_get_msb", "situ_bits_get_ne", "situ_bits_set_lsb",
	"situ_bits_set_msb", "situ_bits_set_ne", "situ_bounds_check",
	"situ_bytes_eq", "situ_checksum_internet", "situ_delimiter_absent",
	"situ_digits_canonical", "situ_digits_minimal", "situ_err_str",
	"situ_err_t", "situ_fletcher16", "situ_fletcher32",
	"situ_format_uint", "situ_get_be16", "situ_get_be32",
	"situ_get_be64", "situ_get_le16", "situ_get_le32", "situ_get_le64",
	"situ_get_ne16", "situ_get_ne32", "situ_get_ne64", "situ_in_bounds",
	"situ_in_set", "situ_leaf_i64", "situ_leaf_u64", "situ_min_u32",
	"situ_msg_clear_dirty", "situ_msg_init", "situ_msg_mark_dirty",
	"situ_msg_t", "situ_msg_touch", "situ_msg_transmittable",
	"situ_need_mul_u32", "situ_need_u32", "situ_nonneg_u32",
	"situ_nul_len", "situ_nul_terminated", "situ_parse_int",
	"situ_parse_scaled", "situ_parse_uint", "situ_put_be16",
	"situ_put_be32", "situ_put_be64", "situ_put_le16", "situ_put_le32",
	"situ_put_le64", "situ_put_ne16", "situ_put_ne32", "situ_put_ne64",
	"situ_remaining_u32", "situ_scan", "situ_scan_any",
	"situ_scan_relaxed", "situ_sign_extend", "situ_skip", "situ_span_t",
	"situ_trim_len", "situ_trim_start", "situ_utf16_valid",
	"situ_utf16be_valid", "situ_utf16le_valid", "situ_utf8_valid",
	"situ_varint_be_get", "situ_varint_be_len", "situ_varint_get",
	"situ_varint_len", "situ_varint_put", "situ_view_assert",
	"situ_view_at", "situ_view_check", "situ_view_sub", "situ_view_t",
	"situ_zeroize", "situ_zigzag_decode", "situ_zigzag_encode"
})


def defined_symbols(text: str) -> frozenset[str]:
	"""The `situ_`-prefixed names a piece of C DEFINES, not the ones it calls.

	One reader for two jobs, so the two cannot disagree about what counts: the
	check below reads generated text with it, and the drift test reads
	`runtime/c/situ.h`. Two regexes over two files is how a list and the thing
	it mirrors stop matching.

	Anchored at the start of a line, which is what separates a definition from
	a call: generated bodies are indented and every definition this emitter
	writes begins in column zero. Sound for text situ produced; it is not a C
	parser and is not pointed at anything else.
	"""
	return frozenset(
		set(re.findall(
			r"^(?:static\s+inline\s+)?[A-Za-z_][\w \t*]*?\b(situ_\w+)\s*\(",
			text, re.M))
		| set(re.findall(r"^#\s*define\s+(SITU_\w+)", text, re.M))
		| set(re.findall(r"^\s*\}\s*(situ_\w+)\s*;", text, re.M)))


def ident(*parts: str) -> str:
	"""Join name fragments into a C identifier.

	Every part goes through `c_name` on the way, so a caller cannot hand a
	namespace separator or a dotted path to the emitter and have it reach the
	output verbatim. That has to happen here rather than at the call sites:
	there are dozens of those, and one that forgot would emit a header no
	compiler accepts.
	"""
	return "_".join(c_name(part) for part in parts if part)


def macro(*parts: str) -> str:
	return ident(*parts).upper()


def c_name(path: str) -> str:
	"""A path rendered as a C identifier fragment.

	Namespace separators and nested paths both flatten to underscores, and the
	brackets situ puts around a synthesised name come off: `<reserved0>` is the
	compiler's own label for a field the schema did not name, and it is not an
	identifier anywhere.

	And anything else that is not an identifier character, because one of
	these fragments is not a schema name at all: the include guard is built
	from the *file* name, and `situc build my-schema.situ` emitted `#ifndef
	SITU_MY-SCHEMA_H` -- a subtraction in a directive, which every C compiler
	warns about and none of them means. A schema is free to live in a file
	named the way files are named.
	"""
	flattened = (path.replace("::", "_").replace(".", "_")
	                 .replace("[]", "").replace("<", "").replace(">", ""))
	return "".join(character if character.isalnum() or character == "_" else "_"
	               for character in flattened)


def bare_name(path: str) -> str:
	"""`c_name`, for a name that is emitted on its own.

	Most generated C identifiers carry the whole path in front of them, so a
	member called `int` reaches `situ_keywords_int_get` and nothing is wrong
	with it. Two places emit the name by itself and are not so lucky: the
	owned struct's field, which is `uint32_t int;` and not C, and every C++
	accessor, which is `std::uint32_t int() const` and not C++.

	One trailing underscore, which is what `class_name` already does to a
	class a member has named, what the Lua dissector already does to a field
	named `function`, and what PEP 8 recommends by name. Rust needs none of
	it: `r#type` is what raw identifiers are for.

	Mangling rather than refusing, and that is decision 0025's argument
	rather than a new one -- the schema keeps its name and the emitter moves.
	It matters more here than it did for `base` and `bytes`: `type` and
	`class` are what specifications actually call their fields, DNS having
	both, so a rule against them would be situ refusing to describe formats
	for a reason that has nothing to do with their bytes.

	The hazard the underscore introduces -- a schema holding both `int` and
	`int_` -- needs nothing new. Both flatten to one stem, and decision
	0013's gate has refused two constructs that reach one C identifier since
	the day it was written.
	"""
	name = c_name(path)
	return f"{name}_" if name in KEYWORDS else name


@dataclass(frozen=True)
class Entity:
	"""One schema construct and the identifier stem it generates.

	The stem, not the full name: a field yields `_get`, `_set`, `_ptr` and more
	from one stem, so two constructs sharing a stem collide in every accessor
	derived from it. Checking stems needs no list of the suffixes in use, which
	is what keeps this from going stale when a phase adds another one.
	"""

	stem: str
	described: str
	span: Span


def entities(resolved: ResolvedSchema, prefix: str,
		declarations: list[tuple[str, str, Span]]) -> list[Entity]:
	"""Every construct that contributes a name, with where it was declared.

	Type declarations arrive from the schema rather than from the resolved
	layout, because a layout carries no span of its own that a diagnostic could
	point at.
	"""
	found = [Entity(ident(prefix, c_name(name)), f"{kind} `{name}`", span)
	         for kind, name, span in declarations]

	for name, struct in resolved.structs.items():
		for entry in struct.entries:
			placement = entry.placement
			# An element entry describes every element of an array at once and
			# generates nothing of its own, and a reserved region is validated
			# rather than exposed.
			if placement.kind in ("element", "reserved"):
				continue

			local = placement.path[len(name) + 1 :]
			found.append(Entity(
				ident(prefix, c_name(name), c_name(local)),
				f"`{placement.path}`",
				placement.span))

	return found


def check_collisions(resolved: ResolvedSchema, prefix: str,
		declarations: list[tuple[str, str, Span]]) -> list[Diagnostic]:
	"""Refuse a schema whose names cannot be told apart in C.

	Raises on a genuine collision, because the alternative is generated code
	that does not compile. Returns warnings for names that survive as functions
	but meet in the macro namespace, which is uppercased: those are legal, and
	they are worth saying out loud rather than enforcing, since which
	convention a schema uses is the author's business (section 25).
	"""
	found    = entities(resolved, prefix, declarations)
	warnings = []

	by_stem: dict[str, Entity] = {}
	for entity in found:
		previous = by_stem.get(entity.stem)
		if previous is not None:
			raise _collision(previous, entity)
		by_stem[entity.stem] = entity

	folded: dict[str, Entity] = {}
	for entity in found:
		previous = folded.get(entity.stem.upper())
		if previous is not None:
			warnings.append(_macro_collision(previous, entity))
		folded[entity.stem.upper()] = entity

	return warnings


def _collision(first: Entity, second: Entity) -> SituError:
	return SituError(Diagnostic(
		severity = Severity.ERROR,
		message  = f"{second.described} and {first.described} generate the same "
		           "C identifier",
		primary  = Label(second.span, f"generates `{second.stem}`"),
		labels   = [Label(first.span, "and so does this")],
		notes    = [
			f"a path flattens to underscores, so both reach `{second.stem}` and "
			"every accessor built from it",
			"rename either one, or put them in separate namespaces",
		],
	))


def check_runtime_collisions(emitted: str, found: list[Entity]) -> None:
	"""Refuse a generated name the C runtime already defines.

	Reads the EMITTED TEXT rather than predicting the names, and that is not
	belt-and-braces: a generated name is not always `ident()`'s result.
	`_extent_from` and `_span_from` are built by appending to one, so a check
	at `ident()` cannot see them, while reading what was produced cannot miss
	a name however it was assembled.

	**The stem check above cannot answer this, and a prefix rule was measured
	and refused.** Stems are what that check compares, deliberately keeping no
	list of suffixes so it survives a phase adding one -- and `situ_bounds`
	collides with nothing while `situ_bounds_check` collides with the runtime.
	Widening it to "a stem that prefixes a runtime symbol" would have caught
	both real cases and refused seven working schemas with them: `msg`,
	`bits`, `digits`, `leaf`, `ascii`, `utf8` and `bcd` all sit under a
	runtime prefix and all seven generate code that compiles.

	Run against the whole corpus before it was added -- 5035 distinct
	generated symbols, no collision -- so this refuses nothing anybody has
	written.
	"""
	clashing = sorted(defined_symbols(emitted) & RUNTIME_SYMBOLS)
	if not clashing:
		return

	# The longest stem that prefixes the name, which is the construct that
	# produced it: `situ_bounds_check` comes from the struct whose stem is
	# `situ_bounds`, not from some shorter one that happens to match.
	by_length = sorted(found, key=lambda one: len(one.stem), reverse=True)
	symbol    = clashing[0]
	owner     = next((one for one in by_length
	                  if symbol.startswith(one.stem + "_")), None)

	raise SituError(Diagnostic(
		severity = Severity.ERROR,
		message  = f"{owner.described if owner else 'this schema'} generates "
		           f"`{symbol}`, which the C runtime defines",
		primary  = (Label(owner.span, f"generates `{symbol}`") if owner
		            else Label(found[0].span, f"generates `{symbol}`")),
		notes    = [
			f"`{symbol}` is a function of `situ.h`, which every generated "
			"header includes, so the two definitions meet in one translation "
			"unit",
			"rename the construct, or generate under a different `--prefix`",
		],
	))


def _macro_collision(first: Entity, second: Entity) -> Diagnostic:
	return Diagnostic(
		severity = Severity.WARNING,
		message  = f"{second.described} and {first.described} differ only in case",
		primary  = Label(second.span, f"generates `{second.stem}`"),
		labels   = [Label(first.span, f"against `{first.stem}`")],
		notes    = [
			"their accessors are distinct, but macro names are uppercased, so "
			f"any macro derived from either reaches `{second.stem.upper()}`",
			"a size constant, an array count or a tag dirty bit would collide; "
			"the accessors would not",
		],
	)
