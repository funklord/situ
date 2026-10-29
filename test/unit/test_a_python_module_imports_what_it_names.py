"""A generated Python module imports every runtime name its classes use.

The import list was built before the classes were generated, and one of
its conditions -- whether any struct publishes check ids, so `NO_CHECK` is
needed -- depends on which accessors exist: a versioned member's check is
emitted only where its getter was (26.74). Asked before the getters were
emitted it said no, and a struct with a constraint behind a `[since]`
raised at import (26.748):

    NO_CHECK = NO_CHECK
    NameError: name 'NO_CHECK' is not defined

Found by the composed sweep; no corpus schema puts a constraint behind a
version on a struct whose other members carry no check, which is the
arrangement where the early answer was wrong.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from situc.codegen.python import generate as generate_py	# noqa: E402
from situc.layout import solve				# noqa: E402
from situc.parser import parse_text			# noqa: E402
from situc.resolve import resolve			# noqa: E402

SCHEMA = """target buffer;
endian big;
bit_order msb_first;
struct s [version = ver] {
	u8   ver;
	u4   hi;
	u4   n;
	u16  run[2];
	u16  tail;
	u16  added [since = 2, must_eq = 4660];
}
"""


def test_a_constraint_behind_a_version_imports(tmp_path: Path) -> None:
	schema   = parse_text(SCHEMA)
	resolved = resolve(schema, solve(schema))
	(tmp_path / "versioned_unit.py").write_text(
		generate_py(schema, resolved, "unit").module, encoding="ascii")
	shutil.copy(ROOT / "runtime" / "python" / "situ_runtime.py", tmp_path)
	sys.path.insert(0, str(tmp_path))
	saved = sys.modules.pop("situ_runtime", None)
	try:
		spec = importlib.util.spec_from_file_location(
			"versioned_unit", tmp_path / "versioned_unit.py")
		assert spec is not None and spec.loader is not None
		module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(module)
	finally:
		sys.path.remove(str(tmp_path))
		sys.modules.pop("situ_runtime", None)
		if saved is not None:
			sys.modules["situ_runtime"] = saved
	assert module.s.NO_CHECK == 0xFFFFFFFF
