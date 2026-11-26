"""A recursive struct's nesting is probed once, at the top (26.1175).

`check` on a recursive struct opens with `nesting`, a walk of everything
under it, and every level of the cycle below asked it again through its
own public `check`: example/json validated a document six brackets deep
at 0.40 MB/s, two dozen probes deep. The probe at the top bounds every
level below it -- a level under it nests one less -- so a parent calls a
descending child's `_check_below`, which is its checks without the probe.

Held two ways: structurally, since a regression here is a slowdown no
verdict shows -- below the top no `check` asks `nesting` -- and by the
verdicts staying what they were, the probe still refusing a document one
level too deep at whichever struct a caller starts from.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT    = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "runtime"
sys.path.insert(0, str(ROOT))

from situc.cli import read_source			# noqa: E402
from situc.codegen.c import generate as c_generate	# noqa: E402
from situc.layout import solve				# noqa: E402
from situc.parser import parse				# noqa: E402
from situc.resolve import resolve			# noqa: E402

HOST_CC = shutil.which("gcc") or shutil.which("cc")

#: The corpus's cycles whose structs state one bound.
CYCLES = {
	"example/json/json.situ": ["member", "members", "object", "element",
	                           "elements", "array", "value"],
	"example/sexpr/sexpr.situ": ["list", "sexpr"],
	"example/cbor/cbor.situ": ["open_array", "any_array", "pair", "open_map",
	                           "map", "any_map", "tagged", "item", "array"],
}


def _functions(source: str) -> dict[str, str]:
	"""Each top-level function's body, by name."""
	found = {}
	for head in re.finditer(r"^(?:static )?situ_err_t (\w+)\(situ_view_t view[^)]*\)\n\{",
	                        source, re.M):
		end = source.index("\n}\n", head.end())
		found[head.group(1)] = source[head.end():end]
	return found


@pytest.mark.parametrize("schema", sorted(CYCLES))
def test_only_the_top_probes(schema: str) -> None:
	parsed = parse(read_source(ROOT / schema))
	source = c_generate(parsed, resolve(parsed, solve(parsed)),
	                    Path(schema).stem).source
	bodies = _functions(source)
	for name in CYCLES[schema]:
		check, below = f"situ_{name}_check", f"situ_{name}_check_below"
		assert below in bodies, f"{schema}: no {below}"
		assert re.search(rf"\bsitu_{name}_nesting\(", bodies[check]), \
			f"{check} no longer probes"
		assert f"return {below}(view, which" in bodies[check]
		assert "_nesting(" not in bodies[below], \
			f"{below} probes again"
		for other in CYCLES[schema]:
			assert not re.search(rf"\bsitu_{other}_(?:check|validate)\(",
			                     bodies[below]), \
				f"{below} calls {other}'s probing entry"


C_MAIN = """#include <stdio.h>
#include <string.h>
#include "json.h"
static int ask(const char *text, int which)
{
	situ_msg_t msg; situ_view_t view;
	situ_msg_init(&msg, (uint8_t *)text, (uint32_t)strlen(text));
	if (situ_view_at(&msg, 0, (uint32_t)strlen(text), &view) != SITU_OK) return -1;
	return which ? (int)situ_array_validate(view) : (int)situ_value_validate(view);
}
int main(void)
{
	char deep[64], over[64];
	memset(deep, '[', 16); deep[16] = '1'; memset(deep + 17, ']', 16); deep[33] = 0;
	memset(over, '[', 17); over[17] = '1'; memset(over + 18, ']', 17); over[35] = 0;
	printf("%d %d %d %d\\n", ask(deep, 0), ask(over, 0), ask(deep, 1), ask(over, 1));
	return 0;
}
"""


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
def test_the_top_still_refuses(tmp_path: Path) -> None:
	"""16 brackets nest 64 levels from `value` and 63 from the `array`
	that is their outer bracket; 17 are too many from either."""
	parsed = parse(read_source(ROOT / "example/json/json.situ"))
	built  = c_generate(parsed, resolve(parsed, solve(parsed)), "json")
	(tmp_path / "json.h").write_text(built.header)
	(tmp_path / "json.c").write_text(built.source)
	(tmp_path / "main.c").write_text(C_MAIN)
	assert HOST_CC is not None
	made = subprocess.run([HOST_CC, "-std=c11", "-O1", "-Wall", "-Wextra",
	                       "-Werror", f"-I{RUNTIME / 'c'}", "-I.", "main.c",
	                       "json.c", str(RUNTIME / "c" / "situ.c"), "-o", "t"],
	                      cwd=tmp_path, capture_output=True, text=True)
	assert made.returncode == 0, made.stderr[-2000:]
	ran = subprocess.run(["./t"], cwd=tmp_path, capture_output=True,
	                     text=True, timeout=60)
	ok, constraint = "0", "2"
	assert ran.stdout.split() == [ok, constraint, ok, constraint]
