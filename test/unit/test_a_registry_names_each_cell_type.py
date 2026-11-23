"""A registry names each cell type, in every backend (26.1169).

An entry's `cell` is a number, and the number is the order a type is first
used in the registry, so two rows swapped renumber the types. fuzznet
(inbox 20261123T1218Z) builds its settings table from the generated rows
and could only map the bare number: a reordering would silently turn a
count into text. Each backend now names every type, and each row's `cell`
is written as that name.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "test" / "unit"))

from situc.cli import main						# noqa: E402
from test_a_registry_is_generated_in_c import SCHEMA		# noqa: E402
from test_a_registry_is_generated_in_python import _module	# noqa: E402

#: The registry's rows, a key of each type, in the order the types are
#: first used.
FIRST = {"count": "revocation/k", "flag": "routing/public",
         "duration": "archive/gone", "text": "host/name",
         "erasure": "archive/erasure"}


def _module_of(tmp_path: Path, text: str) -> object:
	"""`_module`'s load, of a schema this test wrote."""
	import importlib.util
	import shutil
	schema = tmp_path / "settings.situ"
	schema.write_text(text, encoding="ascii")
	assert main(["build", str(schema), "--target", "python",
	             "--out", str(tmp_path)]) == 0
	shutil.copy(ROOT / "runtime" / "python" / "situ_runtime.py", tmp_path)
	sys.path.insert(0, str(tmp_path))
	try:
		spec = importlib.util.spec_from_file_location(
			"settings_moved", tmp_path / "settings.py")
		assert spec is not None and spec.loader is not None
		module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(module)
	finally:
		sys.path.remove(str(tmp_path))
	return module


def _source(tmp_path: Path, target: str) -> str:
	schema = tmp_path / "settings.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	out = tmp_path / target
	out.mkdir()
	assert main(["build", str(schema), "--target", target,
	             "--out", str(out)]) == 0
	return "\n".join(path.read_text(encoding="ascii")
	                 for path in sorted(out.iterdir()))


def test_python_names_each_type_and_the_rows_use_them(tmp_path: Path) -> None:
	node = _module(tmp_path).node			# type: ignore[attr-defined]
	held = {name: getattr(node, f"CELL_{name.upper()}") for name in FIRST}
	assert sorted(held.values()) == list(range(len(FIRST)))
	for name, key in FIRST.items():
		assert node.find(key).cell == held[name], name


def test_swapping_two_rows_moves_the_numbers_and_not_the_names(
		tmp_path: Path) -> None:
	"""The fault the names exist for: the numbers move, and a consumer
	reading the name still reads a count as a count."""
	first = SCHEMA.index('"revocation/k"')
	text  = SCHEMA.index('"host/name"')
	line_k = SCHEMA[first:SCHEMA.index('"the estate\'s k";', first)
	                + len('"the estate\'s k";')]
	line_t = SCHEMA[text:SCHEMA.index("\n", text)]
	moved  = SCHEMA.replace(line_k, "@@").replace(line_t, line_k) \
		.replace("@@", line_t)
	node = _module_of(tmp_path, moved).node	# type: ignore[attr-defined]
	assert node.CELL_TEXT == 0 and node.CELL_COUNT != 0
	assert node.find("revocation/k").cell == node.CELL_COUNT
	assert node.find("host/name").cell == node.CELL_TEXT


def test_c_cpp_and_rust_name_each_type(tmp_path: Path) -> None:
	c    = _source(tmp_path, "c")
	cpp  = _source(tmp_path, "cpp")
	rust = _source(tmp_path, "rust")
	for index, name in enumerate(FIRST):
		assert f"#define SITU_NODE_CELL_{name.upper()} {index}u" in c
		assert f"static constexpr std::uint32_t cell_{name} = {index}u;" in cpp
		assert f"pub const CELL_{name.upper()}: u32 = {index};" in rust
	assert ".cell      = SITU_NODE_CELL_COUNT," in c
	assert "cell_count}," in cpp
	assert "cell: Node::CELL_COUNT" in rust
