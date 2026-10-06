"""A fixed-digit text number's `_ptr` returns a pointer (26.572).

Its C accessor is declared `const uint8_t *` and returned
`situ_<struct>_<field>_offset(view)`, which is a `uint32_t`:

	static inline const uint8_t *situ_m_d_ptr(situ_view_t view)
	{
		return situ_m_d_offset(view);
	}

	error: returning 'uint32_t' from a function with return type
	'const uint8_t *' makes pointer from integer without a cast
	[-Wint-conversion]

`-Werror` is in this project's own `WARNFLAGS`, so situc emitted C its own
build refuses -- 26.563's class, by a different route.

The cause was a proxy. The emitter added `situ_base(view) +` only when the
base `isdigit()` or ended in `u`, which tests "is this a constant" where
the question is "is this an offset needing a base". `_base_expression` has
three return paths and all three are byte counts, so the answer is always
yes.

It survived because every case anybody has takes the branch that works:
all 28 fixed-digit text numbers in this repository's schemas sit at STATIC
offsets -- cpio's 26, smtp's `reply_line.code` and `edges.situ`'s
`text_driver.n`. A dynamic one had no coverage at all, which is what the
fixture below and the corpus struct beside it are for.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from situc.codegen.c import generate as generate_c
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve

ROOT    = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "runtime" / "c"
GCC     = shutil.which("gcc") or shutil.which("cc")

#: The project's own flags, copied from `Makefile`'s `WARNFLAGS` rather
#: than a milder set: `-Werror` is what turns this from a warning nobody
#: reads into a build that stops, and a test compiling without it would
#: pass over the defect.
WARNINGS = ["-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
            "-Wconversion", "-Wsign-conversion"]

PREAMBLE = """endian big;
bit_order msb_first;

varint_type vlen {
	encoding  = be128;
	max_bits  = 64;
	max_bytes = 9;
}

"""

#: A fixed-digit text number, reached each way. `static` is every case the
#: corpus has and is the control: it compiled before and must still.
SHAPES = {
	"static":  "struct m {\n\tu8 head;\n\tdecimal u16 d[3];\n\tu16 tail;\n}\n",
	"dynamic": ("struct m {\n\tvlen v;\n\tdecimal u16 d[3];\n"
	            "\tu16 tail;\n}\n"),
}


def _emit(body: str) -> tuple[str, str]:
	schema   = parse_text(PREAMBLE + body, path="m.situ")
	resolved = resolve(schema, solve(schema))
	built    = generate_c(schema, resolved, "m")
	return built.header, built.source


@pytest.mark.parametrize("label", sorted(SHAPES))
def test_the_pointer_accessor_adds_the_base(label: str) -> None:
	"""Read out of the text, so it fails the same way with no compiler.

	Asserted as "the return adds `situ_base`" rather than as the absence of
	the old spelling: a member whose offset is a constant produced
	`situ_base(view) + 1u` before this and still does, so an assertion on
	the absence of `_offset(view)` would pass for the control by accident.
	"""
	header, _ = _emit(SHAPES[label])
	start = header.index("_d_ptr(situ_view_t view)")
	body  = header[start:start + 120]
	assert "return situ_base(view) + " in body, body


@pytest.mark.skipif(GCC is None, reason="no gcc")
@pytest.mark.parametrize("label", sorted(SHAPES))
def test_it_compiles_under_the_project_s_own_flags(tmp_path: Path,
		label: str) -> None:
	"""The real question, and the flags are the ones that make it one."""
	header, source = _emit(SHAPES[label])
	(tmp_path / "m.h").write_text(header, encoding="ascii")
	(tmp_path / "m.c").write_text(source, encoding="ascii")

	assert GCC is not None
	done = subprocess.run(
		[GCC, *WARNINGS, "-fsyntax-only", f"-I{RUNTIME}", f"-I{tmp_path}",
		 str(tmp_path / "m.c")],
		capture_output=True, text=True)
	assert done.returncode == 0, done.stderr


def test_the_flags_are_the_makefile_s() -> None:
	"""A copied flag list goes stale, and the one that matters is `-Werror`.

	Without it `-Wint-conversion` is a warning gcc prints and a test
	ignores, so the whole module would have passed over the defect it was
	written for. Read out of the Makefile rather than trusted.
	"""
	makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
	line = next(one for one in makefile.splitlines()
	            if one.startswith("WARNFLAGS"))
	named = set(line.split(":=", 1)[1].split())
	assert named <= set(WARNINGS), f"{named - set(WARNINGS)} not covered here"
	assert "-Werror" in named, line
