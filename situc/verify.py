"""Check that real bytes conform to a schema, generating nothing.

Situ's usual bargain is that the schema becomes the code. A project that
cannot take that bargain -- because its callers hold owned structs rather
than views, or because its build may not gain a code generator -- can still
take a smaller one: **the schema as a specification**, checked in CI against
bytes the existing hand-written implementation produced.

That is what this is for. `situc wire --check` and `situc map --check`
already hold a schema to its own committed contracts, which catches an edit
that moves a field. Neither of them looks at a single real byte, so neither
notices the case that matters more: the schema and the implementation
disagreeing from the day the schema was written.

The corpus is the arbiter. Bytes the other implementation emitted are the
only thing in the arrangement that is not somebody's opinion about the
format -- which is the same argument `test/unit/oracles.py` makes, one layer
out and available to a project that generates nothing.

Nothing is written to disk and nothing is compiled. The Python accessors are
built in memory and executed there, so an adopter's build gains a check and
no artifacts.

WHAT A VECTOR IS HELD TO. Three things, and they were not always three. The
view has to be acquirable over the vector's bytes and no more of them; the
generated `validate` has to accept it; and every expectation the vector
states -- `sender_hardware = 00:1A:2B:3C:4D:5E`, the lines the corpus writes
under each case -- has to be what the schema reads out of those bytes.

The last was the gap that mattered. `parse_vectors` has always filled in
`Case.expectations` and this module had never once looked at them, so the
fourteen lines under ARP's two vectors stated what the packet says and were
checked by nothing: swapping `sender_*` with `target_*` in the schema left
`situc verify` reporting "2 vectors conform" while `situc wire --check`
called the same edit BREAKING. The expectations are read here the way
`situc gen-tests` reads them into cmocka -- the same `_nested_of`,
`_byte_run` and `_hex`, imported rather than restated, because two commands
that decide the shape of an expectation separately are two commands that
will eventually disagree about a corpus.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

from situc import ast
from situc.codegen import python as generate_py
from situc.codegen.c.names import macro
from situc.codegen.c.vectors import (
	Case, parse_vectors,
	_byte_run, _hex, _nested_of,   # noqa: PLC2701 -- see the module docstring
)
from situc.codegen.python.emit import py_name
from situc.diagnostics import Source
from situc.resolve import ResolvedSchema, ResolvedStruct
from situc.traverse import Check, classify_check, data_sized


@dataclass
class Outcome:
	"""One vector, and what the schema made of it."""

	case: Case
	#: None where the bytes conformed; otherwise what refused them.
	refusal: str | None = None
	#: One line per expectation that did not hold, where that is the refusal.
	mismatches: list[str] = field(default_factory=list)
	#: Arrays in this vector's struct whose elements `validate` does not walk
	#: -- see `_unwalked`. Recorded whether or not the vector conformed,
	#: because it qualifies what conforming means rather than the vector.
	unwalked: tuple[str, ...] = ()

	@property
	def ok(self) -> bool:
		return self.refusal is None


def runtime_source() -> str:
	"""The Python runtime the generated module imports.

	Beside the package when situ is installed, and up two directories in the
	source tree. Both are checked rather than one being assumed, because the
	whole point of this command is that it runs where a build system does not.
	"""
	here = Path(__file__).resolve().parent
	for candidate in (here / "_runtime" / "situ_runtime.py",
	                  here.parent / "runtime" / "python" / "situ_runtime.py"):
		if candidate.is_file():
			return candidate.read_text(encoding="ascii")

	raise SystemExit(
		"situc: cannot find situ_runtime.py, which `verify` needs to read "
		"bytes without generating code")


def _module(schema: ast.Schema, resolved: ResolvedSchema, name: str) -> object:
	"""The accessors, built and executed in memory.

	`exec` rather than a temporary file and an import: this command exists so
	that a project can check a corpus without its build acquiring a code
	generator, and writing generated Python into somebody's tree -- even a
	temporary directory -- is the thing it is avoiding.
	"""
	runtime = types.ModuleType("situ_runtime")
	exec(runtime_source(), runtime.__dict__)           # noqa: S102
	sys.modules["situ_runtime"] = runtime

	try:
		built = types.ModuleType(name)
		exec(generate_py.generate(schema, resolved, name).module,   # noqa: S102
		     built.__dict__)
		return built
	finally:
		del sys.modules["situ_runtime"]


def check(schema: ast.Schema, resolved: ResolvedSchema, name: str,
		vectors: Source) -> list[Outcome]:
	"""Every vector in `vectors`, against the schema.

	A vector conforms when the schema can acquire a view over its bytes, no
	byte is left over, `validate` accepts them, and every expectation the
	case states is what the accessors read. Those fail differently and are
	reported differently: a view that cannot be acquired means the bytes are
	the wrong length for the layout, a validator that refuses means they are
	the right length and say something the schema forbids, and an expectation
	that does not hold means the schema reads the right number of bytes out
	of the wrong place.

	"No byte is left over" is not the same as "the frame's length is the
	vector's". A struct with a `located` member has a fixed frame and a
	message that reaches past it to wherever the data puts that member (9.8)
	-- `example/bmp`'s `bitmap_file` is 54 bytes of headers and a 78-byte
	vector -- so surplus is only surplus where the schema says where the
	bytes end. `_surplus` is where that decision is written down.
	"""
	module = _module(schema, resolved, name)
	macros = _enum_macros(schema, resolved)
	found: list[Outcome] = []

	for case in parse_vectors(vectors):
		held   = getattr(module, case.struct, None)
		struct = resolved.find_struct(case.struct)
		if held is None or struct is None:
			found.append(Outcome(case, f"no struct `{case.struct}` in this schema"))
			continue

		found.append(_outcome(module, resolved, macros, held, struct, case))

	return found


def _outcome(module: object, resolved: ResolvedSchema, macros: dict[str, int],
		held: object, struct: ResolvedStruct, case: Case) -> Outcome:
	"""One vector: acquire, validate, measure, and read what it claims."""
	unwalked = _unwalked(resolved, struct)
	message  = getattr(module, "Message")

	try:
		# A fixed-size struct's `at` takes no length: its extent is the
		# constant, and offering one is a TypeError rather than a check.
		fixed = getattr(held, "SIZE_BYTES", 0)
		view  = (held.at(message(bytearray(case.data)), 0)   # type: ignore[attr-defined]
		         if fixed else
		         held.at(message(bytearray(case.data)), 0,   # type: ignore[attr-defined]
		                 len(case.data)))
		view.validate()
	except Exception as refused:                        # noqa: BLE001
		return Outcome(case, f"{type(refused).__name__}: {refused}",
		               unwalked=unwalked)

	surplus = _surplus(held, struct, case.data)
	if surplus is not None:
		return Outcome(case, surplus, unwalked=unwalked)

	mismatches = _expectations(resolved, macros, view, struct.name, case)
	if mismatches:
		return Outcome(case,
		               f"{len(mismatches)} of {len(case.expectations)} "
		               f"expectations do not hold",
		               mismatches, unwalked)

	return Outcome(case, None, unwalked=unwalked)


def _surplus(held: object, struct: ResolvedStruct, data: bytes) -> str | None:
	"""Bytes the layout does not reach, where the schema says where it ends.

	The docstring on `check` has always said "a view over exactly its bytes"
	and only the lower half of that was true: `at` refuses a frame shorter
	than the layout and nothing refused a longer one, so 228 bytes conformed
	to a 28-byte fixed struct. This is the upper half.

	Two cases have no upper bound to enforce and are not made to have one:

	  * A struct with a `located` member is placed where the *data* says,
	    measured from the start of the message (9.8), so the message
	    legitimately runs past the frame. `example/bmp`'s `bitmap_file` is
	    exactly this -- 54 bytes of headers, a 78-byte file -- and the same
	    case is spelled out in `codegen/c/vectors._check`, which refused that
	    vector until it learned the difference.
	  * A variable struct whose extent the bytes do not settle. `required`
	    is the accessors' own answer to "how far does this reach", and for a
	    run walked to the end of the message it cannot answer: netlink's
	    `attrs` walk consumes every attribute and then asks for one more, so
	    `nl_message.required` raises on a vector that is complete. A raise
	    here means unknown, not short -- short is `at`'s to refuse -- so
	    nothing is claimed.
	"""
	if any(entry.placement.located is not None for entry in struct.entries):
		return None

	extent = getattr(held, "SIZE_BYTES", 0)
	if not extent:
		required = getattr(held, "required", None)
		if required is None:
			return None
		try:
			extent = int(required(bytes(data)))
		except Exception:                               # noqa: BLE001
			return None

	if len(data) <= extent:
		return None

	return (f"the layout covers {extent} of the vector's {len(data)} bytes; "
	        f"{len(data) - extent} are surplus")


def _expectations(resolved: ResolvedSchema, macros: dict[str, int], view: object,
		struct: str, case: Case) -> list[str]:
	"""Every expectation the case states, against what the schema reads."""
	found: list[str] = []

	for path, value in case.expectations:
		try:
			problem = _expectation(resolved, macros, view, struct, path, value,
			                       path)
		except Exception as refused:                    # noqa: BLE001
			problem = (f"`{path}` could not be read: "
			           f"{type(refused).__name__}: {refused}")
		if problem is not None:
			found.append(problem)

	return found


def _expectation(resolved: ResolvedSchema, macros: dict[str, int], view: object,
		struct: str, path: str, value: str, label: str) -> str | None:
	"""One expectation, in the form the field it names admits.

	The same three questions `codegen/c/vectors` asks of the same line, in
	the same order and with the same helpers: is this a member of a nested
	struct, reached through its own view; is it a run of bytes, compared as
	bytes; or is it a value, compared as a number.
	"""
	if resolved.find(f"{struct}.{path}") is None:
		return f"`{label}` is not a field of `{struct}`"

	nested = _nested_of(resolved, struct, path)
	if nested is not None:
		member, inner, rest = nested
		return _expectation(resolved, macros, getattr(view, py_name(member)),
		                    inner, rest, value, label)

	count = _byte_run(resolved, struct, path)
	read  = getattr(view, py_name(path))

	if count is not None or isinstance(read, (bytes, bytearray, memoryview)):
		return _byte_expectation(bytes(read), count, value, label)

	want = _number(macros, value)
	if want is None:
		return (f"`{label}`: `{value}` is neither an integer nor an enum "
		        f"constant of this schema")
	if int(read) != want:
		return (f"`{label}` is {int(read)}, and the vector expects "
		        f"{_spelled(want, value)}")

	return None


def _byte_expectation(read: bytes, count: int | None, value: str,
		label: str) -> str | None:
	"""A run of bytes, compared as bytes.

	A value that is not bytes, or is the wrong number of them, is a defect in
	the vector file rather than in the schema, and says so -- which is the
	same judgement `codegen/c/vectors._check` makes before it emits C that
	would compare a prefix.
	"""
	try:
		want = _hex(value)
	except ValueError as exc:
		return (f"`{label}` is a run of bytes, so its expectation is bytes "
		        f"and `{value}` is not ({exc})")

	if len(want) != len(read):
		return (f"`{label}` is {len(read)} bytes and its expectation is "
		        f"{len(want)}")

	if want != read:
		return f"`{label}` is {_hexed(read)}, and the vector expects {value}"

	return None


def _spelled(want: int, value: str) -> str:
	"""The expected number, and how the vector wrote it if that differs."""
	return str(want) if value.strip() == str(want) else f"{want} ({value})"


def _hexed(data: bytes) -> str:
	return " ".join(f"{byte:02X}" for byte in data) or "(nothing)"


def _number(macros: dict[str, int], value: str) -> int | None:
	"""An expectation's value, as the number the C suite would compare.

	A vector states an enum member the way the generated header spells it --
	`SITU_OPERATION_REPLY` -- because that is what `gen-tests` drops into
	cmocka, so the macro table is how one is read here. Everything else is a
	C integer constant, octal and all: `010` is eight to the compiler that
	reads the generated suite, and reading it as ten here would make the two
	commands disagree about a corpus rather than about a schema.
	"""
	text = value.strip()
	if text in macros:
		return macros[text]
	return _c_integer(text)


def _c_integer(text: str) -> int | None:
	sign = -1 if text.startswith("-") else 1
	body = text[1:] if text[:1] in "+-" else text
	body = body.rstrip("uUlL")

	try:
		if body[:2].lower() == "0x":
			return sign * int(body[2:], 16)
		if body[:2].lower() == "0b":
			return sign * int(body[2:], 2)
		if len(body) > 1 and body[0] == "0":
			return sign * int(body[1:], 8)
		return sign * int(body, 10)
	except ValueError:
		return None


def _enum_macros(schema: ast.Schema, resolved: ResolvedSchema) -> dict[str, int]:
	"""Every enum member, under the name the generated C header gives it.

	`situ` rather than a `--prefix`, because that is `gen-tests`' default and
	this command takes no such option: a vector file states one spelling and
	both commands have to read it.
	"""
	values = resolved.layout.env.enums
	found: dict[str, int] = {}

	for decl in schema.enums():
		for member in decl.members:
			held = values.get(decl.name, {})
			if member.name in held:
				found[macro("situ", decl.name, member.name)] = held[member.name]

	return found


def _unwalked(resolved: ResolvedSchema, struct: ResolvedStruct) -> tuple[str, ...]:
	"""Arrays whose elements carry constraints that `validate` does not read.

	Deliberate, documented, and shared by all four backends: an array of
	structs gets an accessor per element and no per-element validation,
	"because walking every element on every parse is a cost the caller should
	choose" (`traverse.Check`). An enum array is the same bargain -- an
	out-of-range `k` in `k kinds[2]` passes -- and so is a run walked to a
	terminator.

	Nothing here changes that. What it changes is the report: a command whose
	whole purpose is telling CI that bytes conform must not print an
	unqualified "N vectors conform" while inheriting a gap it knows the name
	of. This finds the gaps that are actually in this schema, so a corpus
	with no arrays is still told plainly that it passed.
	"""
	found: list[str] = []
	_gather(resolved, struct, found, set())
	return tuple(found)


def _gather(resolved: ResolvedSchema, struct: ResolvedStruct, found: list[str],
		seen: set[str]) -> None:
	if struct.name in seen:
		return
	seen.add(struct.name)

	for entry in struct.entries:
		placement = entry.placement
		# An element entry describes every element of an array at once; the
		# array member itself is the one that is or is not walked.
		if placement.kind == "element" or "[]" in placement.path:
			continue

		inner    = resolved.structs.get(placement.type_name or "")
		repeated = (placement.array_count is not None
		            or data_sized(placement)
		            or placement.repeat_while is not None)

		if repeated:
			if inner is not None and _has_checks(resolved, inner, set()):
				found.append(f"`{placement.path}`: every `{placement.type_name}` "
				             f"in it carries constraints, and none was read")
			elif (placement.scalar is not None
			      and placement.type_name in resolved.layout.env.enums):
				found.append(f"`{placement.path}`: every element is a "
				             f"`{placement.type_name}`, and no element's "
				             f"membership was read")
			continue

		if inner is not None and placement.scalar is None:
			# `validate` calls through to a nested struct's own validator, so
			# an array inside one is just as unwalked and just as worth saying.
			_gather(resolved, inner, found, seen)


def _has_checks(resolved: ResolvedSchema, struct: ResolvedStruct,
		seen: set[str]) -> bool:
	"""Whether this struct's `validate` would have anything to say."""
	if struct.name in seen:
		return False
	seen.add(struct.name)

	for entry in struct.entries:
		placement = entry.placement
		if classify_check(struct, placement, resolved.structs) is not Check.NOTHING:
			return True
		inner = resolved.structs.get(placement.type_name or "")
		if inner is not None and _has_checks(resolved, inner, seen):
			return True

	return False


def render(found: list[Outcome], schema_path: str, vectors_path: str) -> str:
	"""The report, in the shape section 17 asks diagnostics to take."""
	failed = [one for one in found if not one.ok]

	lines = []
	for one in failed:
		lines.append(f"error: {one.case.struct} `{one.case.name}` does not "
		             f"conform")
		lines.append(f"   --> {vectors_path}:{one.case.line}")
		lines.append(f"    = {one.refusal}")
		for mismatch in one.mismatches:
			lines.append(f"    = {mismatch}")
		lines.append(f"    = {len(one.case.data)} bytes, from an implementation "
		             f"that is not this schema")
		lines.append("")

	# Deduplicated across the corpus and in the order the schema declares
	# them: two vectors for one struct have the same arrays in them, and
	# saying so twice is noise.
	arrays = list(dict.fromkeys(one for outcome in found
	                            for one in outcome.unwalked))

	if not found:
		lines.append(f"situc: {vectors_path} holds no vectors")
	elif failed:
		lines.append(f"situc: {len(failed)} of {len(found)} vectors do not "
		             f"conform to {schema_path}")
	elif arrays:
		# Not "N vectors conform to X", which is what this printed while
		# an out-of-range enum sat unread inside an array.
		lines.append(f"situc: {len(found)} vectors conform to {schema_path}, "
		             f"except inside arrays")
	else:
		lines.append(f"situc: {len(found)} vectors conform to {schema_path}")

	if found and arrays:
		lines.append("note: an array's elements are validated by the caller "
		             "that chooses to walk them (traverse.Check), so these "
		             "were not read:")
		lines.extend(f"    = {one}" for one in arrays)

	return "\n".join(lines) + "\n"
