"""Every member the capability map calls writable can be written, in all four.

The map is the product (section 1). `mutate = InPlaceFixed` on a field is a
claim that the generated API lets a caller write it where it sits -- and a
backend that emits no setter for such a field has not implemented the schema,
it has implemented a narrower one. The absence is not even visible: nothing
fails, the accessor simply is not there.

This is 26.31's pair of agreement checks on the other axis. Those ask what each
backend *refuses* and fail where the four disagree; this asks what the map
*promises* and fails where a backend does not deliver it. The difference
matters, because the three gaps found this session were all silent in a way
backend-versus-backend cannot see:

  * Rust emitted nothing for a scalar at a dynamic offset -- no setter and no
    note -- while the other three wrote it and the map called it writable;
  * three backends emitted no covered write for a field of a nested struct, so
    the only path to it marked no tag;
  * and each was found by a differential run that had to compile a driver
    first, which is a slow and indirect way to learn that a function is
    missing.

What is *not* asked is written down beside each exclusion. Every one is a
member whose write the map does not promise in the first place, or one whose
setter belongs to another type.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import pytest

from every_schema import SCHEMAS, ids
from situc import ast
from situc.capability import Axis
from situc.cli import analyse
from situc.codegen.c import generate as generate_c
from situc.codegen.c.names import bare_name, c_name
from situc.codegen.python.emit import py_name
from situc.codegen.rust.emit import _ident as rust_ident
from situc.codegen.cpp import generate as generate_cpp
from situc.codegen.python import generate as generate_py
from situc.codegen.rust import generate as generate_rs
from situc.parser import parse
from situc.resolve import ResolvedSchema, ResolvedStruct
from situc.traverse import local_name


class Emitted(Protocol):
	"""What every backend's `generate` returns. Four different `Generated`
	types, so a bare mapping of them infers as `object` and checks nothing."""

	def files(self) -> dict[str, str]: ...


Emit = Callable[[ast.Schema, ResolvedSchema, str], Emitted]

BACKENDS: dict[str, Emit] = {
	"c":      generate_c,
	"cpp":    generate_cpp,
	"rust":   generate_rs,
	"python": generate_py,
}


def spellings(backend: str, struct: str, local: str) -> list[str]:
	"""How each language writes one field. Four surfaces, one operation.

	Python's is a property assignment rather than a method, which is the
	backend's own convention (decision 0022) -- and the reason this asks for
	spellings rather than one name.

	The mangling each backend applies to a member named for one of its own
	keywords is asked for rather than restated. Restating it is how this test
	came to carry `set_r#{local}`, which no backend has ever emitted: Rust
	escapes the whole identifier, and `set_type` is not a keyword. A second
	copy of a naming rule is a copy that disagrees with the emitter, and the
	disagreement shows up as a member the test says has no setter.
	"""
	return {
		"c":      [f"situ_{c_name(struct)}_{local}_set("],
		"cpp":    [f"set_{bare_name(local)}("],
		"rust":   [f"{rust_ident('set_' + local)}("],
		"python": [f"@{py_name(local)}.setter", f"def set_{py_name(local)}("],
	}[backend]


def promised(struct: ResolvedStruct) -> list[str]:
	"""The struct's own fields the map says are writable in place."""
	found: list[str] = []

	for entry in struct.entries:
		placement = entry.placement

		# A nested struct's field belongs to the nested type, which emits its
		# own setter; an element of a run belongs to the element type. Both
		# appear here under a dotted path because the map names them.
		#
		# The exception is a *covered* nested field, whose write has to mark a
		# bit the nested type cannot know about. That one is held to the
		# parent by `test_invariants.py`, which asks for the marking too --
		# the operation is not the same operation.
		if "." in local_name(struct, placement) or "[]" in placement.path:
			continue

		if placement.kind != "field" or placement.scalar is None:
			continue
		# An array is reached through its pointer; a setter writing one
		# element would be a worse API than the pointer and an explicit mark.
		if placement.array_count is not None or placement.sized_by is not None:
			continue
		# A text number is written as digits, and a longer number needs more
		# of them: there is no store that leaves the bytes after it in place.
		if placement.radix is not None:
			continue
		# A marker resolves byte order rather than holding a value.
		if placement.marker is not None:
			continue
		# A sealed interior is reached through the gate, whose type every
		# accessor on it takes.
		if placement.sealed_by:
			continue
		# `[secret]` gets no accessor at all, which is the point (14.6).
		if any(attr.name == "secret" for attr in placement.attrs):
			continue

		if entry.vector.get(Axis.MUTATE).base != "InPlaceFixed":
			continue

		found.append(c_name(local_name(struct, placement)))

	return found


def unwritable(struct: ResolvedStruct) -> list[str]:
	"""The struct's own members that have an accessor and no setter.

	`mutate` weakened below `InPlaceFixed` is the map saying a write does not
	fit where the member sits. Section 1 says the absence is "deliberate,
	explained, and assertable", and the explanation is the part a reader of
	the header needs: a span, a length and no setter, with nothing saying
	which of the two it is -- an oversight, or the schema.
	"""
	found: list[str] = []

	for entry in struct.entries:
		placement = entry.placement

		if "." in local_name(struct, placement) or "[]" in placement.path:
			continue
		if placement.kind not in ("field",):
			continue
		if placement.scalar is None or placement.type_name in ("", None):
			continue
		# A member with no accessor at all has already been declined, and the
		# branch that declined it said why. Two refusals read as two problems.
		if placement.sealed_by or placement.marker is not None:
			continue
		if any(attr.name == "secret" for attr in placement.attrs):
			continue

		if entry.vector.get(Axis.MUTATE).base in ("InPlaceFixed",
		                                          "InPlaceSlack"):
			continue

		found.append(c_name(local_name(struct, placement)))

	return found


@pytest.mark.parametrize("schema", SCHEMAS, ids=ids(SCHEMAS))
def test_every_unwritable_member_says_why(schema: Path) -> None:
	"""The other half of the claim, and the half three backends did not keep.

	`http.request_line.method` is `mutate = Shifting`: a delimited member's
	length is wherever the delimiter turns out to be, so a longer value needs
	the bytes after it to move. Three backends emitted a pointer, a length and
	no setter, and said nothing at all -- Rust alone explained it, from a
	setter emitter it calls for every member (26.35)."""
	source, resolved, _ = analyse(schema)
	parsed = parse(source)

	emitted = {name: "\n".join(generate(parsed, resolved, "unit").files().values())
	           for name, generate in BACKENDS.items()}

	silent: list[str] = []
	for struct in resolved.structs.values():
		if struct.layout.register is not None:
			continue

		for local in unwritable(struct):
			for backend, text in emitted.items():
				# The note names the member or the operation it would have
				# been: `No set_method(): mutate is Shifting.` in Rust, `No
				# name setter: ...` in Python, and a block comment in C and
				# C++ that follows the member's own accessors.
				if "mutate is" not in text:
					silent.append(f"{backend}: {struct.name}.{local}")

	assert not silent, (
		f"{schema.name}: no setter and no explanation:\n  "
		+ "\n  ".join(sorted(set(silent))))


@pytest.mark.parametrize("schema", SCHEMAS, ids=ids(SCHEMAS))
def test_every_writable_member_has_a_setter(schema: Path) -> None:
	source, resolved, _ = analyse(schema)
	parsed = parse(source)

	emitted = {name: "\n".join(generate(parsed, resolved, "unit").files().values())
	           for name, generate in BACKENDS.items()}

	missing: list[str] = []
	for struct in resolved.structs.values():
		# A register is a bus transaction rather than bytes off a wire, and
		# its access modes are section 15.2's rather than `mutate`'s.
		if struct.layout.register is not None:
			continue

		for local in promised(struct):
			for backend, text in emitted.items():
				if not any(one in text
				           for one in spellings(backend, struct.name, local)):
					missing.append(f"{backend}: {struct.name}.{local}")

	assert not missing, (
		f"{schema.name}: the map calls these writable and the backend emits "
		"no setter:\n  " + "\n  ".join(missing))
