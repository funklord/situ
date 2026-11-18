"""A registry written at runtime, without the compiler (0070, step 5).

The holder: a registry "would need to be written dynamically at runtime in
some cases. As there might be a schema editor in GUI." So the cell types
are solved once -- `situc pack` over a schema of structs with no registry
in it -- and the registry is written by the program: `walker.registry.
build_rows` makes the rows `std/image.situ` declares, and `attach` reads
them beside the types. What a cell is worth is read from the base image
alone, its `let value` or its one text-number member, so the editor names a
type and never an index.

Held to the same 33 cases the generated C and both walkers answer for the
same registry packed by `situc`: a walker cannot tell the two apart.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

import pytest							# noqa: E402

from situc.diagnostics import Source				# noqa: E402
from situc.layout import solve					# noqa: E402
from situc.pack import pack					# noqa: E402
from situc.parser import parse					# noqa: E402
from situc.resolve import resolve				# noqa: E402
from test_a_registry_is_generated_in_c import CASES, SCHEMA	# noqa: E402
from test_a_registry_is_walked import _ask			# noqa: E402
from walker import registry					# noqa: E402
from walker.image import load					# noqa: E402

#: The same registry the schema declares, as a program would write it.
CELLS = [
	{"key": "revocation/k", "type": "count", "min": 1, "max": 255,
	 "safe": "higher", "unit": "root signatures",
	 "summary": "the estate's k", "words": {"scope": ["estate"]}},
	{"key": "routing/public", "type": "flag", "default": "off",
	 "safe": "lower",
	 "words": {"scope": ["estate"], "display": ["advertised"]}},
	{"key": "archive/gone", "type": "duration", "min": 1, "max": 315360000,
	 "default": "14d",
	 "words": {"scope": ["estate"], "display": ["advanced"]}},
	{"key": "host/name", "type": "text", "min_size": 1, "max_size": 8,
	 "words": {"scope": ["host"]}},
	{"key": "archive/erasure", "type": "erasure", "default": "4+2",
	 "words": {"scope": ["estate"]}},
	{"key": "stream/grant/", "type": "count", "max": 3,
	 "words": {"scope": ["estate"], "display": ["verb"]}},
]


def _types_only() -> object:
	"""The schema's structs, packed with no registry at all."""
	text = SCHEMA[:SCHEMA.index("registry node {")]
	schema = parse(Source("types.situ", text))
	blob = pack(schema, resolve(schema, solve(schema)), metadata=True)[0]
	image = load(blob)
	assert image.registry == []
	return image


def test_rows_written_at_runtime_answer_as_packed_ones() -> None:
	base  = _types_only()
	image = registry.attach(base, registry.build_rows(base, "node", CELLS))
	said  = [(case, _ask(image, *case[:3])) for case in CASES]
	assert [got for _, got in said] == [case[-1] for case in CASES], [
		(case, got) for case, got in said if got != case[-1]]
	gone = registry.find(image, "archive/gone")	# type: ignore[arg-type]
	assert gone is not None
	assert registry.words(image, gone, "display") == ["advanced"]	# type: ignore[arg-type]


def test_the_written_rows_are_the_packed_rows() -> None:
	"""Byte for byte where it matters: each row's fields, the strings
	compared by what they spell rather than where the pools put them."""
	base    = _types_only()
	written = registry.attach(base, registry.build_rows(base, "node", CELLS))
	schema  = parse(Source("settings.situ", SCHEMA))
	packed  = load(pack(schema, resolve(schema, solve(schema)),
	                    metadata=True)[0])

	def spelled(image: object, entry: object) -> tuple[object, ...]:
		from walker.image import NONE, _string_at
		def text(held: int) -> str | None:
			return None if held == NONE else _string_at(image.strings, held)  # type: ignore[attr-defined]
		return (text(entry.registry), text(entry.key), entry.cell,  # type: ignore[attr-defined]
		        entry.family, entry.safe, entry.value_kind, entry.bounded,  # type: ignore[attr-defined]
		        entry.min, entry.max, entry.min_size, entry.max_size,  # type: ignore[attr-defined]
		        text(entry.default), text(entry.unit), text(entry.summary),  # type: ignore[attr-defined]
		        sorted((text(p), text(w)) for p, w in entry.words))  # type: ignore[attr-defined]
	assert [spelled(written, one) for one in written.registry] \
		== [spelled(packed, one) for one in packed.registry]


def test_a_type_with_no_value_cannot_be_bounded() -> None:
	base = _types_only()
	with pytest.raises(ValueError, match="has no value to bound"):
		registry.build_rows(base, "node", [
			{"key": "a", "type": "text", "max": 3}])


@pytest.mark.skipif(__import__("shutil").which("cc") is None,
                    reason="needs a C compiler")
def test_the_c_walker_attaches_rows_written_at_runtime(
		tmp_path: Path) -> None:
	import shutil
	import subprocess

	from test_a_registry_is_walked import C_PROBE

	text = SCHEMA[:SCHEMA.index("registry node {")]
	schema = parse(Source("types.situ", text))
	blob = pack(schema, resolve(schema, solve(schema)), metadata=True)[0]
	rows = registry.build_rows(load(blob), "node", CELLS)
	(tmp_path / "image.bin").write_bytes(blob)
	(tmp_path / "rows.bin").write_bytes(rows)

	opened = ("\tif (situ_walk_open(&image, blob, (uint32_t)n) != SITU_WALK_OK)\n"
	          "\t\treturn 2;\n")
	assert opened in C_PROBE
	probe = C_PROBE.replace(opened, opened + """
	static uint8_t rows[1 << 16];
	FILE *r = fopen(argv[2], "rb");
	size_t m = fread(rows, 1, sizeof rows, r);
	fclose(r);
	if (situ_walk_registry_attach(&image, rows, (uint32_t)m) != SITU_WALK_OK)
		return 3;
""").replace("for (int i = 2; i + 2 < argc; i += 3)",
             "for (int i = 3; i + 2 < argc; i += 3)")
	(tmp_path / "probe.c").write_text(probe, encoding="ascii")
	walker = ROOT / "walker" / "c"
	binary = tmp_path / "probe"
	built  = subprocess.run(
		[shutil.which("cc") or "cc", "-std=c11", "-O2", "-Wall", "-Wextra",
		 "-Werror", f"-I{walker}", str(tmp_path / "probe.c"),
		 str(walker / "situ_walk.c"), "-o", str(binary)],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	argv = [str(binary), str(tmp_path / "image.bin"), str(tmp_path / "rows.bin")]
	for op, key, arg, _ in CASES:
		argv += [op, key, arg]
	said = subprocess.run(argv, capture_output=True, text=True,
	                      check=True).stdout.splitlines()
	want = [("found" if expected != "-" else "-") if op == "find"
	        else expected for op, _, _, expected in CASES]
	assert said == want, [(case, got) for case, got, expect
	                      in zip(CASES, said, want) if got != expect]
