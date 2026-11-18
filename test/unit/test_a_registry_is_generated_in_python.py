"""A registry generates a class in Python (0070, step 6).

The C's table and four functions (26.1081) in Python's idiom: `find`,
`check` -- which raises the cell type's own error, as `validate` does --
`value` and `safer`, over the same registry and held to the same 33 cases.
And the module passes `mypy --strict`, which is the only gate that reads a
generated annotation.
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

import pytest							# noqa: E402

from situc.cli import main					# noqa: E402
from test_a_registry_is_generated_in_c import CASES, SCHEMA	# noqa: E402


def _module(tmp_path: Path) -> object:
	schema = tmp_path / "settings.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	assert main(["build", str(schema), "--target", "python",
	             "--out", str(tmp_path)]) == 0
	shutil.copy(ROOT / "runtime" / "python" / "situ_runtime.py", tmp_path)
	sys.path.insert(0, str(tmp_path))
	try:
		spec = importlib.util.spec_from_file_location(
			"settings_registry", tmp_path / "settings.py")
		assert spec is not None and spec.loader is not None
		module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(module)
	finally:
		sys.path.remove(str(tmp_path))
	return module


def test_the_python_registry_answers_what_the_c_one_does(
		tmp_path: Path) -> None:
	module = _module(tmp_path)
	node   = module.node				# type: ignore[attr-defined]
	from situ_runtime import BoundsError, SituError	# type: ignore[import-not-found]

	def ask(op: str, key: str, arg: str) -> str:
		entry = node.find(key)
		if op == "find":
			return entry.key if entry else "-"
		if op == "check":
			try:
				node.check(entry, arg.encode())
			except BoundsError:
				return "1"
			except SituError:
				return "2"
			return "0"
		if op == "value":
			held = node.value(entry, arg.encode())
			return str(-1 if held is None else held)
		first, second = arg.split(",")
		return str(int(node.safer(entry, first.encode(), second.encode())))

	said = [(case, ask(*case[:3])) for case in CASES]
	assert [got for _, got in said] == [case[-1] for case in CASES], [
		(case, got) for case, got in said if got != case[-1]]
	gone = node.find("archive/gone")
	assert (gone.default, gone.scope, gone.display) == ("14d", 1, 4)
	assert module.SCOPE_ESTATE == 1			# type: ignore[attr-defined]


@pytest.mark.skipif(subprocess.run([sys.executable, "-m", "mypy", "--version"],
                                   capture_output=True).returncode != 0,
                    reason="no mypy")
def test_the_python_registry_type_checks(tmp_path: Path) -> None:
	_module(tmp_path)
	checked = subprocess.run(
		[sys.executable, "-m", "mypy", "--strict", "--no-pretty",
		 "--no-error-summary", str(tmp_path / "settings.py")],
		capture_output=True, text=True, cwd=tmp_path)
	assert checked.returncode == 0, checked.stdout + checked.stderr
