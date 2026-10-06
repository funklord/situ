"""A length only a transform can produce is not a length (26.570).

26.440 fixed this for a length that NAMES a field: `u8 body[len]` inside a
sealed region reads `len` through the gate, while the arithmetic placing
whatever follows the region runs on the plain view, where there is none.
Its predicate asks the renderer and catches `UnknownName`, which is exact
-- for a name.

A self-delimiting member has no name to ask about. A varint's length is in
its own continuation bits, a delimited run's is wherever the scan stopped,
and a `while` run's is where the condition failed. Inside a coded region
every one of those bytes is the codec's output, so the length is no more
readable than a driver behind the gate -- and the three sibling branches of
each backend's length builder inherited the fault the name branch had
fixed:

	sealed(aead, nonce = nonce) { vlen v; u16 w; }

	C       situ_m_sealed_v_len(view) EMITTED, reading the varint out of
	        ciphertext -- so the tag was located from a number no sender
	        wrote (26.440's silent half, same subsystem)
	C++     calls sealed_v_len(), defines it nowhere: no compile
	Rust    the same, E0599
	Python  the same, AttributeError on the first accessor touched

`coded` as well as `sealed`, which is why the predicate asks about `codec`
rather than about the seal: a plain coded region has the same bytes and no
gate at all, so a check keyed on the seal would have found half of it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from situc.codegen.c import generate as generate_c
from situc.codegen.cpp import generate as generate_cpp
from situc.codegen.python import generate as generate_py
from situc.codegen.rust import generate as generate_rs
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve
from situc.traverse import length_is_transform_output

ROOT    = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "runtime"
GCC     = shutil.which("gcc") or shutil.which("cc")
GXX     = shutil.which("g++") or shutil.which("c++")
RUSTC   = shutil.which("rustc")

PREAMBLE = """target buffer;
bit_order msb_first;
endian big;

codec aead {
	tag_bytes   = 16;
	nonce_bytes = 12;
	granularity = byte;
	length_preserving;
	seekable;
	authenticated;
	invertible;
	deterministic;
}

impl aead extern "my_aead";

codec plain {
	granularity = byte;
	seekable    = linear;
	invertible;
	deterministic;
	expansion   = ratio_exact(2, 1);
}

impl plain extern "my_plain";

varint_type vlen {
	encoding  = be128;
	max_bits  = 64;
	max_bytes = 9;
}

"""

#: Each is a member whose length is in its own bytes, with a fixed-size
#: member after it so that something needs the length. Named by the
#: construct rather than by the schema, because the construct is the
#: subject.
SELF_DELIMITING = {
	"varint":        "vlen v;\n\t\tu16 w;",
	"delimited_run": 'u8 v[] until ",";\n\t\tu16 w;',
	"text_number":   'decimal u16 v until "\\r\\n" max 8;\n\t\tu16 w;',
}

#: Interiors whose length is readable from the plain view, and the reason
#: the guard is not "anything variable inside a region". `remaining` runs
#: to the end of the frame, `outer_sized` names a plaintext field parsed
#: before the region -- which is what `example/packet` does and comments on
#: -- and `inner_sized` names a field inside it, which is 26.440's case and
#: already answered.
READABLE = {
	"remaining":   "u16 w;\n\t\tu8 x[remaining];",
	"inner_sized": "u16 n;\n\t\tu8 x[n];",
	"outer_sized": "u8 x[len];",
	"fixed":       "u16 w;\n\t\tu8 x[4];",
}


def _sealed(interior: str) -> str:
	return (PREAMBLE + "struct m {\n\tu8 len;\n\tu8 nonce[12];\n\n"
	        f"\tsealed(aead, nonce = nonce) {{\n\t\t{interior}\n\t}}\n\n"
	        "\ttag u8[16];\n}\n")


def _coded(interior: str) -> str:
	return (PREAMBLE + "struct m {\n\tu8 len;\n\n"
	        f"\tcoded body(plain) {{\n\t\t{interior}\n\t}}\n\n"
	        "\tu8 trailer;\n}\n")


#: Each shape under each region kind, with the member path the predicate
#: must name. The sealed region is anonymous and the coded one is called
#: `body`, so the path differs and is passed rather than guessed.
SHAPES = [(label, kind, interior, f"m.{held}.v")
          for label, interior in sorted(SELF_DELIMITING.items())
          for kind, held in ((_sealed, "sealed"), (_coded, "body"))]

IDS = [f"{label}-{held}"
       for label in sorted(SELF_DELIMITING)
       for held in ("sealed", "body")]


def _emit(text: str) -> dict[str, str]:
	schema   = parse_text(text, path="m.situ")
	resolved = resolve(schema, solve(schema))
	built_c  = generate_c(schema, resolved, "m")
	return {
		"c":      built_c.header + "\n" + built_c.source,
		"cpp":    generate_cpp(schema, resolved, "m").header,
		"python": generate_py(schema, resolved, "m").module,
		"rust":   generate_rs(schema, resolved, "m").module,
	}


# -- the predicate ----------------------------------------------------------


@pytest.mark.parametrize("label,region,interior,member", SHAPES, ids=IDS)
def test_the_predicate_names_the_member(label: str, region: object,
		interior: str, member: str) -> None:
	"""The population, asserted rather than the three instances.

	Exactly one member of each fixture answers yes and every other answers
	no -- so a construct that arrives later and is NOT covered shows up
	here as a member the guard passes, rather than as a header that does
	not compile.
	"""
	schema   = parse_text(region(interior), path="m.situ")	# type: ignore[operator]
	resolved = resolve(schema, solve(schema))

	named = {entry.placement.path
	         for struct in resolved.structs.values()
	         for entry in struct.entries
	         if length_is_transform_output(entry.placement)}
	assert named == {member}, named


@pytest.mark.parametrize("label,interior", sorted(READABLE.items()))
def test_a_readable_length_is_not_refused(label: str, interior: str) -> None:
	"""The control, and it is the half 26.440 got wrong twice.

	Both of that entry's wrong fixes declined `[remaining]`, which has no
	closed form and is perfectly resolvable as the rest of the frame -- six
	corpus files each time, caught by diffing generated C and by no test.
	This is that diff kept as one.
	"""
	for text in (_sealed(interior), _coded(interior)):
		schema   = parse_text(text, path="m.situ")
		resolved = resolve(schema, solve(schema))
		assert not any(length_is_transform_output(entry.placement)
		               for struct in resolved.structs.values()
		               for entry in struct.entries), text


# -- what the backends do with it -------------------------------------------


@pytest.mark.skipif(GCC is None, reason="no gcc")
@pytest.mark.parametrize("label,region,interior,member", SHAPES, ids=IDS)
def test_the_c_it_emits_compiles(tmp_path: Path, label: str,
		region: object, interior: str, member: str) -> None:
	"""And C is the one that compiled before and was wrong.

	It emitted `<region>_v_len(view)` and read the varint out of ciphertext,
	so the only evidence available was the answer. Compiled here because C
	also grew a second fault on the way: a setter that marks a tag dirty
	called an `_offset` the decline path no longer emits, which nothing had
	reached before because every covered scalar used to have an offset.
	"""
	schema   = parse_text(region(interior), path="m.situ")	# type: ignore[operator]
	resolved = resolve(schema, solve(schema))
	built    = generate_c(schema, resolved, "m")
	(tmp_path / "m.h").write_text(built.header, encoding="ascii")
	(tmp_path / "m.c").write_text(built.source, encoding="ascii")

	assert GCC is not None
	done = subprocess.run(
		[GCC, "-std=c11", "-fsyntax-only", f"-I{RUNTIME / 'c'}",
		 f"-I{tmp_path}", str(tmp_path / "m.c")],
		capture_output=True, text=True)
	assert done.returncode == 0, done.stderr


@pytest.mark.skipif(GXX is None, reason="no g++")
@pytest.mark.parametrize("label,region,interior,member", SHAPES, ids=IDS)
def test_the_cpp_it_emits_compiles(tmp_path: Path, label: str,
		region: object, interior: str, member: str) -> None:
	schema   = parse_text(region(interior), path="m.situ")	# type: ignore[operator]
	resolved = resolve(schema, solve(schema))
	(tmp_path / "m.hpp").write_text(
		generate_cpp(schema, resolved, "m").header, encoding="ascii")
	(tmp_path / "main.cpp").write_text(
		'#include "m.hpp"\nint main() { return 0; }\n', encoding="ascii")

	assert GXX is not None
	done = subprocess.run(
		[GXX, "-std=c++20", "-fsyntax-only", f"-I{RUNTIME / 'c'}",
		 f"-I{RUNTIME / 'cpp'}", f"-I{tmp_path}", str(tmp_path / "main.cpp")],
		capture_output=True, text=True)
	assert done.returncode == 0, done.stderr


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
@pytest.mark.parametrize("label,region,interior,member", SHAPES, ids=IDS)
def test_the_rust_it_emits_compiles(tmp_path: Path, label: str,
		region: object, interior: str, member: str) -> None:
	schema   = parse_text(region(interior), path="m.situ")	# type: ignore[operator]
	resolved = resolve(schema, solve(schema))
	src = tmp_path / "src"
	src.mkdir()
	(src / "situ_rt.rs").write_text(
		(RUNTIME / "rust" / "situ_rt.rs").read_text(
			encoding="ascii").replace("#![no_std]\n", ""), encoding="ascii")
	(src / "m.rs").write_text(
		generate_rs(schema, resolved, "m").module, encoding="ascii")
	(src / "lib.rs").write_text("pub mod situ_rt;\npub mod m;\n",
	                            encoding="ascii")

	assert RUSTC is not None
	done = subprocess.run(
		[RUSTC, "--edition", "2021", "--crate-type", "lib", "lib.rs",
		 "-o", str(tmp_path / "libm.rlib")],
		capture_output=True, text=True, cwd=src)
	assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("label,region,interior,member", SHAPES, ids=IDS)
def test_the_python_it_emits_can_be_used(tmp_path: Path, label: str,
		region: object, interior: str, member: str) -> None:
	"""Python has no compiler, so the question is asked by running it.

	`validate()` alone is not enough and that was a real mistake while this
	was being measured: two of the three shapes put the dangling reference
	only on the GATE, so a probe that never opened one reported them clean.
	So this touches every public property on the view and on the gate, and
	fails on `AttributeError` alone -- a `BoundsError` from a buffer of
	zeroes is the accessor working.
	"""
	import importlib.util
	import sys

	schema   = parse_text(region(interior), path="m.situ")	# type: ignore[operator]
	resolved = resolve(schema, solve(schema))
	(tmp_path / "m.py").write_text(
		generate_py(schema, resolved, "m").module, encoding="ascii")
	shutil.copy(RUNTIME / "python" / "situ_runtime.py", tmp_path)

	sys.path.insert(0, str(tmp_path))
	try:
		spec = importlib.util.spec_from_file_location(
			f"m_{label}_{member.replace('.', '_')}", tmp_path / "m.py")
		assert spec is not None and spec.loader is not None
		module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(module)
	finally:
		sys.path.remove(str(tmp_path))

	view = module.m.at(module.Message(bytearray(120)), 0, 120)
	try:
		view.validate()
	except AttributeError:
		raise
	except Exception:
		pass

	opens = [one for one in dir(type(view)) if one.startswith("open_")]
	held  = [view] + [getattr(view, one)(True) for one in opens]
	for one in held:
		for name in dir(type(one)):
			if name.startswith("_"):
				continue
			if not isinstance(getattr(type(one), name, None), property):
				continue
			try:
				getattr(one, name)
			except AttributeError:
				raise
			except Exception:
				pass


@pytest.mark.parametrize("label,region,interior,member", SHAPES, ids=IDS)
def test_the_python_gate_says_why_a_member_is_missing(label: str,
		region: object, interior: str, member: str) -> None:
	"""A member dropped in silence reads as a member the schema lacks.

	This backend's gate filtered its interior down to what it could place
	and said nothing about the rest, so `test_the_backends_refuse_the_same_
	members` -- which reads a note to know a member was declined -- counted
	a silently dropped member as an emitted one. C++ and Rust wrote the
	note; this one and C did not.

	Only the Python half is fixed here. C still drops such a member without
	a word, which is 26.571's subject and is why no corpus schema carries
	this shape yet: adding one makes a pre-existing C fault live.

	Asserted of `w` rather than of `v`: `w` is the member whose placement
	depends on the unreadable length, and it is declined in all three
	shapes. `v` itself is declined only where it is a varint, because this
	backend's interior filter admits a member with a `scalar` and a varint
	has none -- a second, older silent drop, and 26.571's as well.
	"""
	if not region.__name__.endswith("sealed"):		# type: ignore[attr-defined]
		return					# no gate outside a seal

	schema, resolved = _schema(region(interior))	# type: ignore[operator]
	built = generate_py(schema, resolved, "m").module
	after = member.rsplit(".", 1)[0] + ".w"
	assert f"# {after}: this backend cannot resolve where it" in built, built


def _schema(text: str):  # type: ignore[no-untyped-def]
	"""The parse and resolve pair."""
	schema = parse_text(text, path="m.situ")
	return schema, resolve(schema, solve(schema))
