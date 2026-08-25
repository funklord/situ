"""The code this compiler emits obeys section 25 too.

The conventions were enforced on the sources and on nothing else. A generated
file has a suffix the lint would check, but it is written into `build/`, which
the lint skips -- and before it is written it lives in string literals inside
the emitters, which `literal_lines` deliberately excludes so that section 17's
golden diagnostics keep their space gutter. Between those two exclusions, every
emitted line in four backends and five artifact generators was held to the tab
rule by the habits of whoever last edited the emitter.

They were clean, which is not the same as being checked. This checks them:
every schema this repository builds, through every generator that produces
source, against the same rule `make lint` applies to the tree.

Not the capability map or the wire report: those are tables, aligned with
spaces on purpose, and the lint exempts `.map` for the same reason.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from situc import dissector
from situc.codegen import c as generate_c
from situc.codegen import cpp as generate_cpp
from situc.codegen import python as generate_py
from situc.codegen import rust as generate_rs
from situc.codegen.c import checks as generate_checks
from situc.codegen.c import fuzz as generate_fuzz
from situc.diagnostics import Source
from situc.layout import solve
from situc.parser import parse
from situc.resolve import resolve

from every_schema import ROOT, SCHEMAS, ids

sys.path.insert(0, str(ROOT / "tool"))

import style_gate  # noqa: E402


def emitted(path: Path) -> dict[str, str]:
	"""Every source text this schema produces, by the name it would be written
	under -- the name matters, because it is the suffix that says which rule
	applies."""
	schema   = parse(Source(str(path), path.read_text(encoding="ascii")))
	resolved = resolve(schema, solve(schema))
	stem     = path.stem

	c   = generate_c.generate(schema, resolved, stem)
	cpp = generate_cpp.generate(schema, resolved, stem)
	rs  = generate_rs.generate(schema, resolved, stem)
	py  = generate_py.generate(schema, resolved, stem)

	return {
		f"{stem}.h":          c.header,
		f"{stem}.c":          c.source,
		f"{stem}.hpp":        cpp.header,
		f"{stem}.rs":         rs.module,
		f"{stem}.py":         py.module,
		f"{stem}_checks.c":   generate_checks.generate(schema, resolved, stem),
		f"{stem}_fuzz.c":     generate_fuzz.generate(schema, resolved, stem),
		f"{stem}.lua":        dissector.generate(schema, resolved, stem),
	}


@pytest.mark.parametrize("path", SCHEMAS, ids=ids(SCHEMAS))
def test_what_this_schema_generates_is_tab_indented(path: Path) -> None:
	problems = []

	for name, text in emitted(path).items():
		where = Path(f"<generated>/{path.parent.name}/{name}")
		# Python is the one emitted language whose own convention is spaces,
		# and this project emits it with tabs like the rest (section 25). The
		# generator has no multi-line literals of its own to exempt.
		problems.extend(style_gate.check_text(text, where))

	assert not problems, "\n" + "\n".join(str(problem) for problem in problems)
