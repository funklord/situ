"""0068's decision table: one argued child, every parent context.

A parent handing a child an argument of its own (0068 step 5) is built
context by context, and each context lifts the nesting refusal for exactly
the shapes it builds. That leaves three states a schema can be in, and only
two of them are allowed:

- **refused** by `check_parameter_nesting`, which is how an unbuilt context
  stays honest -- the record says as much, and it is why an unplumbed path
  in C is a `situc build` refusal rather than a wrong byte in somebody's
  program;
- **generated, and the C compiles** under this project's own flags;
- **generated, and the C does not compile.** This is the state the table
  exists to forbid, and the run that wrote it met the state four times in
  one afternoon (26.812, 26.813, 26.814, 26.815).

The verdict is recorded per case rather than asserted as "no case is
broken", for the reason the Rust census in
`test_a_generated_length_never_wraps.py` carries: a floor on a total cannot
tell "this regressed" from "there is more here now". Per case it can, and a
case that improves has to be written down or the table stops measuring.

One case is recorded BROKEN: an argued child inside a transparent block,
where the parent flattens the child's members into its own accessors and
that path emits a setter calling an offset function nothing defines. It is
a missing emission rather than a wrong signature, and 26.817 has it.
Fixing it fails this test with "good news", which is the prompt to move
its row.

The table is the instrument rather than a test of one construct: step 4 and
26.811 both used a parent FIELD, which needs none of the plumbing a
parameter needs, so no construct in the corpus asked these questions and
every one of them was answerable in a few seconds.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from every_schema import ROOT
from situc.codegen.c import generate as generate_c
from situc.diagnostics import SituError
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve

HEAD = "target buffer;\nendian big;\nbit_order msb_first;\n"

#: A byte-moving argued child: `b` carries `[stream]`, so it may size bytes
#: and rule 5 restricts where its value may come from.
KID = "struct kid { parameter u8 b [stream]; u8 body[b]; u8 mark; }\n"

#: A meaning-only argued child, fixed size however the argument reads, for
#: the contexts that refuse a dynamic member for reasons of their own.
NOTE = "struct note { parameter u8 mode; u8 v [max = mode]; }\n"

AEAD = ("codec sealing {\n\tlength_preserving;\n\tseekable;\n"
        "\tauthenticated;\n\tinvertible;\n\tdeterministic;\n}\n"
        'impl sealing extern "my_sealing";\n')
DBL = ("codec dbl {\n\texpansion = ratio_exact(2, 1);\n"
       "\tgranularity = byte;\n\tseekable = linear;\n\tinvertible;\n"
       "\tdeterministic;\n}\n"
       'impl dbl extern "my_dbl";\n')

#: case -> (schema body, recorded verdict). "compiles", "refused", or
#: "broken" with the record that explains it.
TABLE: dict[str, tuple[str, str]] = {
	"plain member": (
		KID + "struct p { parameter u8 b [stream]; kid(b = b) k; u8 z; }\n",
		"compiles"),
	# The same base-only spelling for an ordinary member and for a run.
	# All three of these were wrong the same way, which is what said the
	# fault was the shared predicate's rather than any one emitter's.
	"plain member, argument only in the base": (
		KID + "struct p { parameter u8 b [stream]; u8 head[b]; "
		      "kid(b = 2) k; u8 z; }\n",
		"compiles"),
	"counted run, argument only in the base": (
		KID + "struct p { parameter u8 b [stream]; u8 head[b]; u8 n; "
		      "kid(b = 2) k[n]; u8 z; }\n",
		"compiles"),
	"counted run": (
		KID + "struct p { parameter u8 b [stream]; u8 n; "
		      "kid(b = b) k[n]; u8 z; }\n",
		"compiles"),
	"literal run": (
		KID + "struct p { parameter u8 b [stream]; kid(b = b) k[4]; "
		      "u8 z; }\n",
		"compiles"),
	"remaining run": (
		KID + "struct p { parameter u8 b [stream]; u8 lead; "
		      "kid(b = b) k[remaining]; }\n",
		"compiles"),
	# The argument reaches the arm's extent helper through `_child_args`.
	"variant arm": (
		KID + "struct p { parameter u8 b [stream]; u8 t;\n"
		      "\tvariant v switch (t) "
		      "{ case 1: kid(b = b) k; default: error; }\n\tu8 z;\n}\n",
		"compiles"),
	# And here only through the arm's BASE, the child being handed a
	# constant -- which is why a fix keyed on the handed text alone is not
	# enough, and why both spellings are in the table.
	"variant arm, argument only in the base": (
		KID + "struct p { parameter u8 b [stream]; u8 head[b]; u8 t;\n"
		      "\tvariant v switch (t) "
		      "{ case 1: kid(b = 2) k; default: error; }\n\tu8 z;\n}\n",
		"compiles"),
	"positional block, fixed child": (
		NOTE + "struct p { parameter u8 m;\n\tpositional {\n"
		       "\t\tnote(mode = m) k;\n\t\tu16 pad;\n\t}\n\tu8 z;\n}\n",
		"compiles"),
	# Refused for a reason that predates arguments entirely: a positional
	# block's whole point is that its members sit at computed offsets.
	"positional block, dynamic child": (
		KID + "struct p { parameter u8 b [stream];\n\tpositional {\n"
		      "\t\tkid(b = b) k;\n\t}\n\tu8 z;\n}\n",
		"refused"),
	"authenticated block": (
		KID + "struct p { parameter u8 b [stream];\n"
		      "\tauthenticated body {\n\t\tkid(b = b) k;\n\t}\n"
		      "\ttag u8 mac[16] covers(body);\n}\n",
		"broken 26.817"),
	"sealed region": (
		AEAD + KID + "struct p { parameter u8 b [stream];\n"
		             "\tsealed body(sealing) {\n\t\tkid(b = b) k;\n\t}\n"
		             "\ttag u8 mac[16] covers(body);\n}\n",
		"refused"),
	"coded region": (
		DBL + KID + "struct p { parameter u8 b [stream]; u8 len;\n"
		            "\tcoded body(dbl) {\n\t\tkid(b = b) k;\n\t}\n}\n",
		"refused"),
	"indexed element": (
		KID + "struct p { parameter u8 b [stream]; u8 cnt;\n"
		      "\tindexed(offset_type = u16, count = cnt, base = cnt) {\n"
		      "\t\tkid(b = b) cells[];\n\t}\n}\n",
		"refused"),
	"self reference": (
		"struct p { parameter u8 b [stream]; u8 n; p(b = b) kid; }\n",
		"refused"),
}

FLAGS = ("-std=c11", "-Os", "-Wall", "-Wextra", "-Werror", "-Wconversion",
         "-Wsign-conversion")

#: The runtime the generated header includes. Copied beside it rather than
#: reached by `-I`, because a quoted include searches the including file's
#: own directory first and a copy there is what a caller actually does.
RUNTIME = ROOT / "runtime" / "c" / "situ.h"


def _verdict(body: str, tmp_path) -> tuple[str, str]:  # type: ignore[no-untyped-def]
	"""This case's verdict, and whatever explains it."""
	# A refusal can come from any stage before codegen, and which stage it
	# is is not the question: `positional` declines a dynamic member in the
	# layout solver while `check_parameter_nesting` declines an argued one
	# at parse. What the table asks is whether situc said no at all.
	try:
		schema   = parse_text(HEAD + body)
		resolved = resolve(schema, solve(schema))
		header   = generate_c(schema, resolved, "situ").header
	except SituError as refused:
		return "refused", str(refused).splitlines()[0]
	(tmp_path / "t.h").write_text(header, encoding="ascii")
	(tmp_path / "situ.h").write_text(RUNTIME.read_text(encoding="ascii"),
	                                 encoding="ascii")
	(tmp_path / "p.c").write_text('#include "t.h"\nint main(void)'
	                              "{return 0;}\n", encoding="ascii")
	run = subprocess.run(["gcc", *FLAGS, "-I", str(tmp_path),
	                      "-c", str(tmp_path / "p.c"),
	                      "-o", str(tmp_path / "p.o")],
	                     capture_output=True, text=True)
	if run.returncode == 0:
		return "compiles", ""
	errors = [line for line in run.stderr.splitlines() if "error:" in line]
	return "broken", f"{len(errors)} error(s): " + "; ".join(
		line.split("error:")[-1].strip() for line in errors[:3])


@pytest.mark.parametrize("case", sorted(TABLE), ids=sorted(TABLE))
def test_an_argued_child_in_every_parent_context(case, tmp_path) -> None:  # type: ignore[no-untyped-def]
	"""Refused or compiling, and never silently in between."""
	if shutil.which("gcc") is None:
		pytest.skip("no gcc")
	body, recorded = TABLE[case]
	verdict, why = _verdict(body, tmp_path)
	kind = recorded.split()[0]

	if verdict == kind:
		return
	if kind == "broken" and verdict != "broken":
		pytest.fail(
			f"good news: {case} is recorded broken ({recorded}) and now "
			f"{verdict}. Move its row to {verdict!r} so the table keeps "
			f"measuring, and close the record")
	if verdict == "broken":
		pytest.fail(
			f"{case} is accepted by situc and its C does not compile, which "
			f"is the one state this table forbids -- an unbuilt context has "
			f"to be refused rather than generated wrong. {why}")
	pytest.fail(
		f"{case} is recorded {recorded!r} and is now {verdict!r}. A context "
		f"that starts or stops being refused is a change to what situc "
		f"accepts, so the row is part of the change. {why}")
