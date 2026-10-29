"""Every backend's rung-4 output, compiled, for every schema in the tree.

**Only C's frame layer was ever compiled.** `test_relations.py` says so in
as many words at `test_no_reader_is_emitted_for_an_unframeable_struct` --
*"C's is the only frame layer a test compiles, and that is how it was
found"* -- and the other three were checked by reading their text for a
struct's name. So rung 4 ships four ways and was built one way.

Decision 0032 puts six rungs on one axis chosen at `situc build --layer`,
and `frame` is rung 4. A consumer asking for it gets `<name>_frame.h`,
`.hpp`, `.rs` or `.py` beside the accessors; for four schemas in C++ and
Rust and nine in Python, the file it gets **does not build**, because the
`Reader` it holds calls a `required` the accessors decline to define:

    error: 'required' is not a member of 'situ::pickle_string'
    error[E0599]: no function or associated item named `required` found
                  for struct `padded::PickleString`

26.692 named this and put it at five structs in four schemas, and that
figure is right: measured here, C is clean on all 42 and C++, Rust and
Python each fail on the same five -- `coap`, `proto_message`,
`tagged_options`, `pickle_string` and `byte_run`.

**A first pass of this measurement said Python's set was wider, and it
was the detector.** It split the generated module on `"class " + name`
to find a class body, which prefix-matches: asked about `image` it read
`image_arm`'s body, found no `required` there, and reported `image`,
`image_section`, `png.chunk` and `smtp.command` as missing when all four
have one. Splitting on a line-anchored class header says they do. That is
why the classes here are keyed exactly and why the holdouts are compared
as a set rather than counted -- a count would have accepted the wrong
four.

The cause is in `frame.framed_structs`, which all four backends share: it
asks `traverse.frameable`, and each emitter's `_required` then applies its
OWN narrower conditions and falls through to `_unframeable`, which emits a
comment and no function. The predicate deciding whether to emit a Reader
and the one deciding whether to emit what the Reader calls are different
predicates. **C is clean because C is the widest** -- it has a pad branch
in its extent chain that the other three have not -- so narrowing the
shared predicate would take working Readers away from C. Closing it is
three emitters gaining what C has, which is a feature rather than a fix
and is 26.692's open item.

So this file holds the gap open with its measurement rather than fixing
it, and the holdouts are **exact**: a backend that starts compiling a
held-out schema fails here too, which is what forces a name out of
`FRAME_APART` rather than letting a fix go unrecorded.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pytest						# noqa: E402

from situc.codegen.c import frame as frame_c		# noqa: E402
from situc.codegen.c import generate as generate_c	# noqa: E402
from situc.codegen.cpp import frame as frame_cpp	# noqa: E402
from situc.codegen.cpp import generate as generate_cpp	# noqa: E402
from situc.codegen.python import frame as frame_py	# noqa: E402
from situc.codegen.python import generate as generate_py	# noqa: E402
from situc.codegen.rust import frame as frame_rs	# noqa: E402
from situc.codegen.rust import generate as generate_rs	# noqa: E402
from situc import ast				# noqa: E402
from situc.diagnostics import Source			# noqa: E402
from situc.layout import solve				# noqa: E402
from situc.parser import parse				# noqa: E402
from situc.resolve import ResolvedSchema, resolve	# noqa: E402

from every_schema import SCHEMAS			# noqa: E402

RUNTIME  = ROOT / "runtime"
HOST_CC  = shutil.which("cc") or shutil.which("gcc")
HOST_CXX = shutil.which("g++") or shutil.which("clang++")
RUSTC    = shutil.which("rustc")

#: (backend, schema) -> the structs whose `Reader` calls a `required` that
#: backend does not define. Measured 2026-10-29; the reason is one per
#: struct and is the construct the backend's `_required` has no branch for.
#:
#: `pad_to` is the common one and is 26.692's "three backends short of a
#: construct": C computes an alignment in its extent chain and the other
#: three fall through to a length expression that does not exist. The rest
#: are TLV and opaque regions, where what the backend declines differs.
FRAME_APART: dict[tuple[str, str], frozenset[str]] = {
	("cpp", "coap.situ"):      frozenset({"coap"}),
	("cpp", "protobuf.situ"):  frozenset({"proto_message"}),
	("cpp", "edges.situ"):     frozenset({"tagged_options"}),
	("cpp", "padded.situ"):    frozenset({"pickle_string", "byte_run"}),
	("rust", "coap.situ"):     frozenset({"Coap"}),
	("rust", "protobuf.situ"): frozenset({"ProtoMessage"}),
	("rust", "edges.situ"):    frozenset({"TaggedOptions"}),
	("rust", "padded.situ"):   frozenset({"PickleString", "ByteRun"}),
	# The same five, which is what makes them one gap in three backends
	# rather than three gaps.
	("python", "coap.situ"):     frozenset({"coap"}),
	("python", "protobuf.situ"): frozenset({"proto_message"}),
	("python", "edges.situ"):    frozenset({"tagged_options"}),
	("python", "padded.situ"):   frozenset({"pickle_string", "byte_run"}),
}

#: C has none, and that is the measurement the fix will be judged against.
assert not any(back == "c" for back, _ in FRAME_APART), \
	"C's frame layer compiles for every schema; a holdout here is a loss"

#: schema -> whether each backend's frame layer built, so the companion
#: below can say the sweep asked something.
FRAME_BUILT: dict[tuple[str, str], bool] = {}


def ids(paths: list[Path]) -> list[str]:
	return [path.name for path in paths]


def analysed(path: Path) -> tuple[ast.Schema, ResolvedSchema]:
	source = Source(str(path), path.read_text(encoding="ascii"))
	parsed = parse(source)
	return parsed, resolve(parsed, solve(parsed))


def _missing(text: str) -> set[str]:
	"""The struct names a compiler or reader says have no `required`."""
	# BOTH quotation styles. g++ writes `\u2018required\u2019` with
	# typographic quotes and clang writes ASCII ones, so a pattern with
	# only the ASCII pair finds nothing in g++'s output -- which reads as
	# "no struct is missing one" and is the opposite of the truth.
	found = set(re.findall(
		r"[\u2018']required[\u2019'] is not a member of "
		r"[\u2018']?[\w:]*::(\w+)[\u2019']?", text))
	found |= set(re.findall(
		r"named `required` found for struct `[\w:]*::(\w+)`", text))
	return found


def _judge(backend: str, schema: Path, missing: set[str],
		detail: str) -> None:
	"""Compare what failed with what is held out, exactly."""
	apart = FRAME_APART.get((backend, schema.name), frozenset())
	FRAME_BUILT[(backend, schema.name)] = not missing
	assert missing == set(apart), (
		f"{backend}/{schema.name}: structs with no `required` are "
		f"{sorted(missing) or 'none'}, held out {sorted(apart) or 'none'}. "
		f"A new name is a regression; a name that has gone is a fix, and "
		f"its entry comes out of FRAME_APART with it.\n{detail[:2000]}")


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
@pytest.mark.parametrize("schema", SCHEMAS, ids=ids(SCHEMAS))
def test_the_c_frame_layer_compiles(schema: Path, tmp_path: Path) -> None:
	parsed, resolved = analysed(schema)
	files = frame_c.generate(parsed, resolved, "unit")
	if not files:
		pytest.skip(f"{schema.name} has no frame layer")

	built = generate_c(parsed, resolved, "unit")
	(tmp_path / "unit.h").write_text(built.header, encoding="ascii")
	(tmp_path / "unit.c").write_text(built.source, encoding="ascii")
	for name, text in files.items():
		(tmp_path / name).write_text(text, encoding="ascii")
	(tmp_path / "m.c").write_text(
		"".join(f'#include "{name}"\n' for name in sorted(files))
		+ "int main(void) { return 0; }\n", encoding="ascii")

	run = subprocess.run(
		[HOST_CC or "cc", "-std=c11", "-fsyntax-only", "-Wall", "-Wextra",
		 "-Werror", f"-I{RUNTIME / 'c'}", f"-I{tmp_path}",
		 str(tmp_path / "m.c")], capture_output=True, text=True)
	_judge("c", schema, _missing(run.stderr), run.stderr)
	assert run.returncode == 0, run.stderr


@pytest.mark.skipif(HOST_CXX is None, reason="no host C++ compiler")
@pytest.mark.parametrize("schema", SCHEMAS, ids=ids(SCHEMAS))
def test_the_cpp_frame_layer_compiles(schema: Path, tmp_path: Path) -> None:
	parsed, resolved = analysed(schema)
	files = frame_cpp.generate(parsed, resolved, "unit")
	if not files:
		pytest.skip(f"{schema.name} has no frame layer")

	(tmp_path / "unit.hpp").write_text(
		generate_cpp(parsed, resolved, "unit").header, encoding="ascii")
	for name, text in files.items():
		(tmp_path / name).write_text(text, encoding="ascii")
	(tmp_path / "m.cpp").write_text(
		"".join(f'#include "{name}"\n' for name in sorted(files))
		+ "int main() { return 0; }\n", encoding="ascii")

	run = subprocess.run(
		[HOST_CXX or "g++", "-std=c++17", "-fsyntax-only", "-Wall",
		 "-Wextra", "-Werror", f"-I{RUNTIME / 'c'}", f"-I{RUNTIME / 'cpp'}",
		 f"-I{tmp_path}", str(tmp_path / "m.cpp")],
		capture_output=True, text=True)
	missing = _missing(run.stderr)
	_judge("cpp", schema, missing, run.stderr)
	if not missing:
		assert run.returncode == 0, run.stderr


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
@pytest.mark.parametrize("schema", SCHEMAS, ids=ids(SCHEMAS))
def test_the_rust_frame_layer_compiles(schema: Path, tmp_path: Path) -> None:
	parsed, resolved = analysed(schema)
	files = frame_rs.generate(parsed, resolved, schema.stem)
	if not files:
		pytest.skip(f"{schema.name} has no frame layer")

	src = tmp_path / "src"
	src.mkdir()
	# The runtime is `no_std` alone; as a module of a larger crate the
	# attribute belongs to the crate root, which is what the accessors'
	# own compile test does.
	(src / "situ_rt.rs").write_text(
		(RUNTIME / "rust" / "situ_rt.rs").read_text(encoding="ascii")
		.replace("#![no_std]\n", ""), encoding="ascii")
	(src / f"{schema.stem}.rs").write_text(
		generate_rs(parsed, resolved, schema.stem).module, encoding="ascii")
	for name, text in files.items():
		(src / name).write_text(text, encoding="ascii")
	(src / "lib.rs").write_text(
		"pub mod situ_rt;\n"
		f'#[path = "{schema.stem}.rs"] pub mod {schema.stem};\n'
		+ "".join(f'#[path = "{name}"] pub mod {name[:-3]};\n'
		          for name in sorted(files)), encoding="ascii")

	run = subprocess.run(
		[RUSTC or "rustc", "--edition", "2021", "-D", "warnings",
		 "--crate-type", "lib", str(src / "lib.rs"),
		 "-o", str(tmp_path / "out")],
		capture_output=True, text=True, cwd=tmp_path)
	missing = _missing(run.stderr)
	_judge("rust", schema, missing, run.stderr)
	if not missing:
		assert run.returncode == 0, run.stderr


@pytest.mark.parametrize("schema", SCHEMAS, ids=ids(SCHEMAS))
def test_the_python_frame_layer_calls_what_exists(schema: Path) -> None:
	"""Python has no compiler, so the call target is checked by name.

	A missing method is an `AttributeError` at the call rather than at the
	build, which is this backend's whole difference and why reading the
	text is the only check available before a message arrives.
	"""
	parsed, resolved = analysed(schema)
	files = frame_py.generate(parsed, resolved, "unit")
	if not files:
		pytest.skip(f"{schema.name} has no frame layer")

	module = generate_py(parsed, resolved, "unit").module
	# ANCHORED, because a plain split on `"class " + name` prefix-matches:
	# `class image` finds `class image_arm` first and reads its body.
	parts  = re.split(r"^class (\w+)", module, flags=re.M)
	bodies = {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}

	called  = set(re.findall(r"\b(\w+)\.required\(",
	                         "\n".join(files.values())))
	missing = {name for name in called
	           if name in bodies and "def required(" not in bodies[name]}
	_judge("python", schema, missing, f"called: {sorted(called)}")


def test_the_frame_sweep_asked_something() -> None:
	"""The non-vacuity companion.

	Measured when written: 152 (backend, schema) pairs had a frame layer
	and were judged, of which 140 built clean and 12 are the holdouts
	above. The floor is well under that, because a schema arriving moves
	the figure and what is worth catching is a collapse.
	"""
	if not FRAME_BUILT:
		pytest.skip("no backend's sweep ran")
	assert len(FRAME_BUILT) >= 100, (
		f"only {len(FRAME_BUILT)} (backend, schema) pairs were judged")
	clean = sum(1 for built in FRAME_BUILT.values() if built)
	assert clean >= 90, f"only {clean} of {len(FRAME_BUILT)} built clean"
	assert len(FRAME_APART) <= 12, (
		f"{len(FRAME_APART)} held-out pairs, where 12 were measured; a "
		f"gap this file documents is one somebody added")
