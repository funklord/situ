"""A bound on a `parameter` itself builds in all four (26.805).

    struct t { parameter u8 b [max = 10]; u8 x; }

26.545 keeps such a bound: the solver reads it, so the argument a caller
supplies is bounded where a size or another argument depends on it. No
backend checks it in `validate`, which reads the message and not the
argument. Python and Rust never emitted a check; C called `situ_t_b_get`
and C++ `this->b()`, neither of which exists, so neither header compiled.
They now emit none either, and the four agree, check ids included.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway

SCHEMA = """target buffer;
endian big;
struct t { parameter u8 b [max = 10]; u8 x; }
struct u { parameter u16 b [stream, min = 1, max = 99]; u8 x[b]; }
struct plain { u8 y; }
"""


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs gcc, g++ and rustc")
def test_all_four_build_it(tmp_path: Path) -> None:
	"""`plain` is there because the differential driver compares no struct
	that takes an argument, and building it is what compiles the others."""
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	assert set(fourway.build(tmp_path, schema)) == {"c", "cpp", "rust",
	                                                "python"}


def test_no_backend_checks_it_and_the_ids_agree(tmp_path: Path) -> None:
	"""Built is not checked, and the regression this missed was in what
	`validate` does: Rust compared the BYTE at the parameter's nominal
	offset -- `x`'s, a parameter occupying nothing -- with `max = 10`, and
	Python read a `self.b` that does not exist, so a `t` with `x = 200`
	was refused in one and raised in the other; both gave the bound check
	id 0, where C's `u.x` holds it (26.1051)."""
	import shutil
	import subprocess
	import sys

	from situc.cli import main

	runtime = Path(__file__).resolve().parents[2] / "runtime"
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	for target in ("c", "python", "rust"):
		assert main(["build", str(schema), "--target", target,
		             "--out", str(tmp_path / target)]) == 0
	header = (tmp_path / "c" / "unit.h").read_text(encoding="ascii")
	module = (tmp_path / "python" / "unit.py").read_text(encoding="ascii")
	crate  = (tmp_path / "rust" / "unit.rs").read_text(encoding="ascii")
	assert "SITU_U_X_CHECK 0u" in header
	assert "CHECK_X = 0" in module and "CHECK_B" not in module
	assert "CHECK_X: u32 = 0" in crate and "CHECK_B" not in crate

	shutil.copy(runtime / "python" / "situ_runtime.py",
	            tmp_path / "python" / "situ_runtime.py")
	(tmp_path / "python" / "main.py").write_text(
		"import situ_runtime as rt\nimport unit\n"
		"unit.t.at(rt.Message(bytearray([200])), 0, arg_b=5).validate()\n"
		"print('ok')\n", encoding="ascii")
	ran = subprocess.run([sys.executable, "main.py"], capture_output=True,
	                     text=True, cwd=tmp_path / "python")
	assert ran.stdout.split() == ["ok"], ran.stdout + ran.stderr

	if shutil.which("rustc") is None:
		return
	shutil.copy(runtime / "rust" / "situ_rt.rs", tmp_path / "rust" / "situ_rt.rs")
	(tmp_path / "rust" / "main.rs").write_text(
		"mod situ_rt;\n#[allow(dead_code)]\nmod unit;\nfn main() {\n"
		"\tlet b = [200u8];\n"
		"\tprintln!(\"{}\", unit::T::new(&b, 5).unwrap().validate().is_ok());\n"
		"}\n", encoding="ascii")
	built = subprocess.run(["rustc", "--edition", "2021", "-o",
	                        str(tmp_path / "rust" / "r"),
	                        str(tmp_path / "rust" / "main.rs")],
	                       capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(tmp_path / "rust" / "r")], capture_output=True,
	                     text=True)
	assert ran.stdout.split() == ["true"], ran.stdout + ran.stderr
