"""A discriminant or a length may read a field any depth down, through
plain nested structs (26.1097).

One level was the rule, so openmlx4's FS2 sentinel -- `variant rest switch
(section.hdr.next)`, `hdr` a struct inside `gph_section` -- was refused as
"not readable before variant", which openmlx4 took for situ declining to
steer a parse by bytes inside the CRC-covered body. It was depth: one level
into the same authenticated body built. Every step before the leaf must be
a struct held once at a place its parent decides; an arm's member, a run's
element and a located member still are not readable before.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

import pytest							# noqa: E402

import fourway							# noqa: E402
from situc.diagnostics import Source, SituError			# noqa: E402
from situc.layout import solve					# noqa: E402
from situc.parser import parse					# noqa: E402

HEAD = "target buffer;\nendian big;\n"

SCHEMA = HEAD + """struct deep_inner { u8 kind; u8 len; }
struct deep_mid { u8 tag; deep_inner inner; }
struct deep_switch {
	deep_mid mid;
	variant body switch (mid.inner.kind) {
		case 1:  u8 one;
		default: u8 rest[mid.inner.len];
	}
}
"""


def _solve(text: str) -> None:
	solve(parse(Source("deep.situ", text)))


def test_two_levels_down_is_in_scope() -> None:
	_solve(SCHEMA)


@pytest.mark.parametrize("through, why", [
	("struct pick { u8 k; variant v switch (k) { case 1: deep_inner a; "
	 "default: deep_inner b; } }", "an arm's member"),
	("struct pick { deep_inner runs[2]; }", "a run's element"),
	("struct pick { u8 at; deep_inner there at at; }", "a located member"),
])
def test_a_step_that_is_not_a_held_struct_is_not(through: str,
		why: str) -> None:
	path = {"an arm's member": "p.v.a.kind", "a run's element": "p.runs.kind",
	        "a located member": "p.there.kind"}[why]
	text = HEAD + "struct deep_inner { u8 kind; u8 len; }\n" + through + f"""
struct outer {{
	pick p;
	variant w switch ({path}) {{ case 1: u8 one; default: u8 two; }}
}}
"""
	with pytest.raises(SituError, match="not readable before"):
		_solve(text)


#: The same bytes read as `deep_switch` (a tag, then kind and len) or as
#: `off_switch` (a pad, then kind and len): one layout, two routes to it.
CASES = [(b"\x00\x01\x00\x07", "0"),		# kind 1: one byte
         (b"\x00\x01\x00", "1"),			# ... missing
         (b"\x00\x02\x03abc", "0"),		# default: three bytes
         (b"\x00\x02\x03ab", "1")]


def _validate(said: str, struct: str) -> str:
	lines = said.splitlines()
	at = lines.index(f"-- {struct}")
	return next(line.split()[1] for line in lines[at + 1:]
	            if line.startswith("validate "))


def test_both_walkers_switch_through_a_nested_struct(tmp_path: Path) -> None:
	"""And at an offset: the arms table carries an index and no base, so a
	discriminant a byte into the owner was dropped -- the C walker said
	cannot-say and the Python walker BOUNDS over bytes every backend takes."""
	from test_walker_c import (COMPILER, VERDICT, _build_driver, c_verdict,
	                           packed_text, python_verdict, shape_named_text)
	text = SCHEMA + """struct off_switch {
	u8         pad;
	deep_inner inner;
	variant body switch (inner.kind) {
		case 1:  u8 one;
		default: u8 rest[inner.len];
	}
}
"""
	blob  = packed_text(text)
	drive = (_build_driver(tmp_path, blob, VERDICT)
	         if COMPILER is not None else None)
	for struct in ("deep_switch", "off_switch"):
		shape = shape_named_text(text, struct)
		for raw, want in CASES:
			assert python_verdict(blob, raw, shape) == want, (struct, raw)
			if drive is not None:
				assert c_verdict(tmp_path, blob, raw, shape, drive) \
					== want, ("c walker", struct, raw)


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_four_backends_switch_and_size_two_levels_down(
		tmp_path: Path) -> None:
	schema = tmp_path / "deep.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	assert set(command) == {"c", "cpp", "rust", "python"}, command
	for raw, want in CASES:
		for name, argv in command.items():
			got = _validate(fourway.answers(argv, raw, tmp_path),
			                "deep_switch")
			assert got == want, (name, raw, got)
