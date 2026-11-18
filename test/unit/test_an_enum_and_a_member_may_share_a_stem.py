"""An enum and a member path sharing a stem collide only where a name does
(26.1093).

Reported by fuzznet (inbox 20261118T1654Z): `fzn_message_call.outcome` of
an enum `fzn_message_call_outcome` -- idiomatic, and four of its contracts
had the shape -- was refused by `build --target c` because both flatten to
one stem, while `map` and `wire` accepted it, so a committed contract was
first refused by whoever generated C from it. The stem was the wrong
question for this pair: an enum derives `_t`, `_is_known` and its arms'
macros from it, a member `_get`, `_set` and its own, and the four compile
cleanly. So the pair is refused where the emitted header really defines a
name twice -- an arm `check` against the member's `SITU_S_M_CHECK` -- and
`map` and `wire` ask the same question build does.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pytest							# noqa: E402

from situc.cli import main					# noqa: E402

HOST_CC = shutil.which("gcc") or shutil.which("cc")

IDIOM = """target buffer;
endian big;
enum call_outcome : u8 { answered = 0, missed = 1, refused = 2 }
struct call { u8 id; call_outcome outcome; }
"""

#: An arm whose macro is the member's check id: a name really defined twice.
CLASH = """target buffer;
endian big;
enum s_m : u8 { check = 3, other = 1 }
struct s { u8 a; s_m m [max = 3]; }
"""

#: Two structs' stems, which derive the same names: refused as before.
STRUCTS = """target buffer;
endian big;
struct s_m { u8 x; }
struct s { u8 m; }
"""


def _run(tmp_path: Path, text: str, *argv: str) -> tuple[int, str]:
	schema = tmp_path / "unit.situ"
	schema.write_text(text, encoding="ascii")
	out = tmp_path / "out"
	out.mkdir(exist_ok=True)
	done = subprocess.run(
		[sys.executable, "-m", "situc", *argv, str(schema),
		 *(["--out", str(out)] if argv[0] == "build" else [])],
		capture_output=True, text=True, cwd=ROOT)
	return done.returncode, done.stderr


@pytest.mark.skipif(HOST_CC is None, reason="needs a C compiler")
def test_the_idiom_builds_and_compiles(tmp_path: Path) -> None:
	rc, said = _run(tmp_path, IDIOM, "build", "--target", "c")
	assert rc == 0, said
	compiled = subprocess.run(
		[HOST_CC, "-std=c11", "-Wall", "-Wextra", "-Werror",
		 f"-I{ROOT / 'runtime' / 'c'}", "-c", str(tmp_path / "out" / "unit.c"),
		 "-o", str(tmp_path / "unit.o")], capture_output=True, text=True)
	assert compiled.returncode == 0, compiled.stderr
	for command in ("map", "wire"):
		assert _run(tmp_path, IDIOM, command)[0] == 0, command


@pytest.mark.parametrize("text, said", [
	(CLASH, "`s.m` and enum `s_m` generate the same C identifier"),
	(STRUCTS, "`s.m` and struct `s_m` generate the same C identifier"),
])
@pytest.mark.parametrize("command", [("build", "--target", "c"), ("map",),
                                     ("wire",)])
def test_a_real_collision_is_refused_by_every_command(
		tmp_path: Path, text: str, said: str, command: tuple[str, ...]) -> None:
	rc, stderr = _run(tmp_path, text, *command)
	assert rc != 0 and said in stderr, (command, stderr)
