"""C accessors for the MMIO target (project.md section 15).

The surface looks like the buffer backend's and the code underneath is entirely
different, which is exactly what 15.1 warns about: a register is a bus
transaction, not bytes in memory. Three things follow, and they shape every
function here.

**A read is an event.** A field getter takes a word the caller has already
read, never the register. A register with `on_read = pop` yields something
different each time it is read, so an API that read once per field would drain
a FIFO to decode a status word. Read once, decode as many fields as you like.

**A write is the whole word.** Where a field is narrower than `access_width`
and the register cannot be read safely, there is no setter at all -- section
15.3's headline. What there is instead is a builder: pure functions that place
a field into a word, and one write that issues the transaction.

**The volatile qualifier is load-bearing.** It is what stops the compiler
caching a status register or reordering a trigger, so it is on the pointer type
rather than on a comment.
"""

from __future__ import annotations

from situc import ast
from situc.capability import Axis
from situc.codegen.c.names import c_name, ident, macro
from situc.propagate import Resolved
from situc.resolve import ResolvedSchema, ResolvedStruct

WORD_WIDTHS = (8, 16, 32, 64)


def word_type(width: int) -> str:
	return f"uint{width}_t"


class RegisterEmitter:
	def __init__(self, resolved: ResolvedSchema, prefix: str) -> None:
		self.resolved = resolved
		self.prefix   = prefix

	def register(self, struct: ResolvedStruct) -> list[str]:
		info = struct.layout.register
		assert info is not None

		lines = ["", f"/* ---- register {struct.name} ---- */", ""]

		if info.access_width not in WORD_WIDTHS:
			lines.extend([
				f"/* No accessors: `access_width = {info.access_width}` is not a",
				" * width C can name. The bus offers transactions of some size;",
				" * declare that size.",
				" */",
			])
			return lines

		lines.extend(self._constants(struct, info))
		lines.extend(self._word_access(struct, info))

		for entry in struct.entries:
			lines.extend(self._field(struct, info, entry))

		return lines

	# -- the register as a whole ----------------------------------------

	def _constants(self, struct: ResolvedStruct, info: ast.RegisterInfo) -> list[str]:
		name  = struct.name
		lines = []

		if info.address is not None:
			lines.append(f"#define {macro(self.prefix, name, 'ADDR')} "
			             f"0x{info.address:02X}u")

		lines.extend([
			f"#define {macro(self.prefix, name, 'WIDTH')}        {info.width}u",
			f"#define {macro(self.prefix, name, 'ACCESS_WIDTH')} {info.access_width}u",
			"",
		])
		return lines

	def _word_access(self, struct: ResolvedStruct,
			info: ast.RegisterInfo) -> list[str]:
		"""The transaction the bus actually offers.

		Everything else in this section composes or decomposes what these two
		move. `block` is the base of the region the register sits in, so a
		caller holds one pointer for a peripheral rather than one per register.
		"""
		name  = struct.name
		word  = word_type(info.access_width)
		at    = ident(self.prefix, name, "at")
		off   = (macro(self.prefix, name, "ADDR") if info.address is not None
		         else "0u")

		lines = [
			"/* The register itself. `volatile` is not decoration here: it is",
			" * what stops the compiler caching a status word or reordering a",
			" * write that triggers something (section 15.3).",
			" */",
			f"static inline volatile {word} *{at}(volatile uint8_t *block)",
			"{",
			f"\treturn (volatile {word} *)(void *)(block + {off});",
			"}",
			f"static inline {word} {ident(self.prefix, name, 'read')}"
			"(volatile uint8_t *block)",
			"{",
			f"\treturn *{at}(block);",
			"}",
		]

		if self._is_writable(struct):
			lines.extend([
				f"static inline void {ident(self.prefix, name, 'write')}"
				f"(volatile uint8_t *block, {word} word)",
				"{",
				f"\t*{at}(block) = word;",
				"}",
			])
		else:
			lines.extend([
				f"/* No {ident(self.prefix, name, 'write')}(): every field in this",
				" * register is read-only, so there is no word a caller could",
				" * compose that the hardware would accept. */",
			])

		return lines

	def _is_writable(self, struct: ResolvedStruct) -> bool:
		return any(entry.placement.access_mode is not None
		           and entry.placement.access_mode.writable
		           for entry in struct.entries)

	# -- one field ------------------------------------------------------

	def _field(self, struct: ResolvedStruct, info: ast.RegisterInfo,
			entry: Resolved) -> list[str]:
		placement = entry.placement

		if placement.kind == "reserved":
			return ["", f"/* {placement.path} -- reserved, no accessor. */"]

		mode = placement.access_mode
		assert mode is not None

		lines = ["", *self._comment(entry, mode)]

		if mode.readable:
			lines.extend(self._getter(struct, info, entry))
		else:
			lines.extend([
				f"/* No getter: `{placement.name}` is `{mode.value}`. The bus does",
				" * not return a value for it, so there is nothing to decode",
				" * out of a word that was read. */",
			])

		lines.extend(self._writer(struct, info, entry, mode))
		return lines

	def _comment(self, entry: Resolved, mode: ast.AccessMode) -> list[str]:
		placement = entry.placement
		vector    = entry.vector
		effects   = []
		if placement.on_read is not ast.SideEffect.NONE:
			effects.append(f"on_read = {placement.on_read.value}")
		if placement.on_write is not ast.SideEffect.NONE:
			effects.append(f"on_write = {placement.on_write.value}")

		detail = f" [{', '.join(effects)}]" if effects else ""
		return [
			f"/* {placement.path} : {mode.value}{detail}",
			f" * bits {self._bit_range(placement)}, "
			f"mutate={vector.get(Axis.MUTATE).render()}, "
			f"effect={vector.get(Axis.EFFECT).render()}",
			" */",
		]

	def _bit_range(self, placement: object) -> str:
		start = getattr(placement, "offset_bits", None) or 0
		width = getattr(placement, "size_bits", 0)
		return f"{start}" if width == 1 else f"{start}..{start + width - 1}"

	def _getter(self, struct: ResolvedStruct, info: ast.RegisterInfo,
			entry: Resolved) -> list[str]:
		"""Decode a field out of a word the caller already read.

		Never out of the register. A read is an event, and a getter that
		performed one would make decoding a status word cost as many
		transactions as it has fields -- which for `on_read = pop` is not a
		performance question but a correctness one.
		"""
		placement = entry.placement
		local     = c_name(placement.path[len(struct.name) + 1:])
		word      = word_type(info.access_width)
		shift     = placement.offset_bits or 0
		mask      = (1 << placement.size_bits) - 1

		body = f"word >> {shift}u" if shift else "word"
		if placement.size_bits != info.access_width:
			body = f"({body}) & 0x{mask:X}u"

		return [
			f"static inline {word} {ident(self.prefix, struct.name, local, 'get')}"
			f"({word} word)",
			"{",
			f"\treturn ({word})({body});",
			"}",
		]

	def _writer(self, struct: ResolvedStruct, info: ast.RegisterInfo,
			entry: Resolved, mode: ast.AccessMode) -> list[str]:
		placement = entry.placement

		if not mode.writable:
			return [
				f"/* No setter: `{placement.name}` is `{mode.value}`. The hardware",
				" * drives this field; a write either does nothing or does",
				" * something other than what it looks like. */",
			]

		if not mode.is_assignment:
			return self._bit_operation(struct, info, entry, mode)

		lines = self._builder(struct, info, entry)
		if placement.on_write is ast.SideEffect.TRIGGER:
			lines.extend(self._trigger(struct, info, entry))
		return lines

	def _builder(self, struct: ResolvedStruct, info: ast.RegisterInfo,
			entry: Resolved) -> list[str]:
		"""Place a field into a word, without touching the bus.

		This is what section 15.3 means by "only `write(builder)` where the
		caller constructs the whole word". The composition is pure, so it can
		be folded, reordered and constant-evaluated; only the write is an
		event.
		"""
		placement = entry.placement
		local     = c_name(placement.path[len(struct.name) + 1:])
		word      = word_type(info.access_width)
		shift     = placement.offset_bits or 0
		mask      = (1 << placement.size_bits) - 1
		refusal   = self._setter_refusal(entry)

		lines = [
			f"static inline {word} {ident(self.prefix, struct.name, local, 'with')}"
			f"({word} word, {word} value)",
			"{",
			f"\tword &= ({word})~(({word})0x{mask:X}u << {shift}u);",
			f"\tword |= ({word})((value & 0x{mask:X}u) << {shift}u);",
			"\treturn word;",
			"}",
		]
		return refusal + lines

	def _setter_refusal(self, entry: Resolved) -> list[str]:
		"""Say why there is no direct setter, where one would be looked for.

		The whole point of the chapter: setting one bit becomes a compile error
		rather than a runtime hazard, and the header explains it.
		"""
		if entry.vector.get(Axis.MUTATE).base != "RewriteRequired":
			return []

		local = entry.placement.name
		lines = [f"/* No {local}_set(): mutate is RewriteRequired."]
		for weakening in entry.blame(Axis.MUTATE):
			lines.append(f" *   caused by: {weakening.rule.construct}")
			lines.append(f" *              {weakening.effect.because}")
			if weakening.rule.remedy:
				lines.append(f" *   remedy:    {weakening.rule.remedy}")
		lines.append(" *")
		lines.append(" * Compose a word with the function below and write it once.")
		lines.append(" */")
		return lines

	def _bit_operation(self, struct: ResolvedStruct, info: ast.RegisterInfo,
			entry: Resolved, mode: ast.AccessMode) -> list[str]:
		"""`clear_error()` rather than `set_error(false)` (section 15.3).

		A `w1c` field is cleared by writing a one to it. Offering assignment
		would describe an operation the bus does not have, and `set(false)`
		would be exactly backwards from what the write does.
		"""
		placement = entry.placement
		local     = c_name(placement.path[len(struct.name) + 1:])
		word      = word_type(info.access_width)
		shift     = placement.offset_bits or 0
		mask      = (1 << placement.size_bits) - 1

		writes_one = mode in (ast.AccessMode.W1C, ast.AccessMode.W1S)
		verb       = "clear" if mode in (ast.AccessMode.W1C,
		                                 ast.AccessMode.W0C) else "set"
		bits       = (f"({word})(0x{mask:X}u << {shift}u)" if writes_one
		              else f"({word})~(({word})0x{mask:X}u << {shift}u)")

		return [
			f"/* `{mode.value}`: the write is not an assignment, so this is a",
			f" * {verb} rather than a setter. Writing "
			f"{'1' if writes_one else '0'} to the field is what the",
			" * hardware acts on; the other bits carry the value that leaves",
			" * every neighbouring field of this kind alone.",
			" */",
			f"static inline void {ident(self.prefix, struct.name, local, verb)}"
			"(volatile uint8_t *block)",
			"{",
			f"\t*{ident(self.prefix, struct.name, 'at')}(block) = {bits};",
			"}",
		]

	def _trigger(self, struct: ResolvedStruct, info: ast.RegisterInfo,
			entry: Resolved) -> list[str]:
		"""A write-to-start field, which is an action rather than a value."""
		placement = entry.placement
		local     = c_name(placement.path[len(struct.name) + 1:])
		word      = word_type(info.access_width)
		shift     = placement.offset_bits or 0
		mask      = (1 << placement.size_bits) - 1

		return [
			f"/* `on_write = trigger`: writing this field starts something, so",
			" * the operation is named for what it does. It writes a word with",
			" * this field set and every other field zero, which is the only",
			" * word that can be composed without reading first.",
			" */",
			f"static inline void {ident(self.prefix, struct.name, local, 'trigger')}"
			"(volatile uint8_t *block)",
			"{",
			f"\t*{ident(self.prefix, struct.name, 'at')}(block) = "
			f"({word})(0x{mask:X}u << {shift}u);",
			"}",
		]


def is_mmio(schema: ast.Schema) -> bool:
	for decl in schema.decls:
		if isinstance(decl, ast.TargetDirective):
			return decl.kind is ast.TargetKind.MMIO
	return False
