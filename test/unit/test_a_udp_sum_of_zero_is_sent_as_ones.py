"""A ones'-complement sum with `zero_means_none` sends a zero as all ones.

RFC 768: a UDP checksum field of zero means the sender computed none, so a
sum that comes out zero is transmitted as 0xFFFF, and a receiver accepts a
stored zero rather than checking it. `internet_checksum` wrote whatever it
computed, so one UDP datagram in 65,536 from a situ builder went out marked
unchecksummed -- reported by hull, which builds its switch's UDP answers
from `example/udp` (26.754).

Covered here: the derived kernel in C, Rust and Python; `verify` in all
four backends; and the walker's kernel, which the editor recomputes with
and which complemented every sum -- wrong for `edges.summing`, which is
not complemented.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from situc.codegen.c import derived as c_derived	# noqa: E402
from situc.codegen.python import derived as py_derived	# noqa: E402
from situc.codegen.rust import derived as rs_derived	# noqa: E402
from situc.layout import solve				# noqa: E402
from situc.pack import pack				# noqa: E402
from situc.parser import parse_text			# noqa: E402
from situc.resolve import resolve			# noqa: E402
from walker import derived as walker_derived		# noqa: E402
from walker.image import load				# noqa: E402

HOST_CC = shutil.which("cc") or shutil.which("gcc")
RUSTC   = shutil.which("rustc")


def kernel_schema(flags: str) -> str:
	return ("endian big;\n"
	        f"codec sum16 {{ kernel = ones_complement(width = 16{flags}); }}\n"
	        "impl sum16 derived;\n"
	        "struct s { u16 a; }\n")


#: One word of 0xFFFF sums to 0xFFFF, which complements to zero.
ZERO_SUM = bytes([0xFF, 0xFF])

#: flags -> what the kernel answers for `ZERO_SUM`.
EXPECTED = {
	", complement, zero_means_none": 0xFFFF,
	", complement":                  0x0000,
	"":                              0xFFFF,	# uncomplemented: the sum
}


@pytest.mark.parametrize("flags", sorted(EXPECTED))
def test_python_sends_a_zero_sum_as_ones(flags: str, tmp_path: Path) -> None:
	path = tmp_path / "sums.py"
	path.write_text(py_derived.generate(parse_text(kernel_schema(flags)),
	                                    "unit"), encoding="ascii")
	spec = importlib.util.spec_from_file_location("sums", path)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	# Python's derived names carry no prefix: the module is the namespace.
	assert module.sum16(ZERO_SUM) == EXPECTED[flags]


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
@pytest.mark.parametrize("flags", sorted(EXPECTED))
def test_c_sends_a_zero_sum_as_ones(flags: str, tmp_path: Path) -> None:
	# The derived file includes the schema's header for its integer types;
	# `test_kernels` swaps it for <stdint.h> the same way.
	(tmp_path / "unit.c").write_text(
		c_derived.generate(parse_text(kernel_schema(flags)), "unit")
		.replace('#include "unit.h"', "#include <stdint.h>"),
		encoding="ascii")
	(tmp_path / "probe.c").write_text(
		"#include <stdint.h>\n#include <stdio.h>\n"
		"uint16_t situ_sum16(const uint8_t *data, uint32_t len);\n"
		"int main(void) { static const uint8_t d[2] = { 0xff, 0xff };\n"
		"\tprintf(\"%u\\n\", (unsigned)situ_sum16(d, 2u)); return 0; }\n",
		encoding="ascii")
	made = subprocess.run(
		[HOST_CC or "cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
		 str(tmp_path / "probe.c"), str(tmp_path / "unit.c"),
		 "-o", str(tmp_path / "bin")], capture_output=True, text=True)
	assert made.returncode == 0, made.stderr[-2000:]
	ran = subprocess.run([str(tmp_path / "bin")], capture_output=True,
	                     text=True)
	assert int(ran.stdout) == EXPECTED[flags]


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_rust_sends_a_zero_sum_as_ones(tmp_path: Path) -> None:
	(tmp_path / "unit.rs").write_text(
		rs_derived.generate(parse_text(kernel_schema(
			", complement, zero_means_none")), "unit"),
		encoding="ascii")
	(tmp_path / "main.rs").write_text(
		"#[path = \"unit.rs\"] mod unit;\n"
		"fn main() { println!(\"{}\", unit::sum16(&[0xff, 0xff])); }\n",
		encoding="ascii")
	built = subprocess.run(
		[RUSTC or "rustc", "--edition", "2021", "-A", "warnings",
		 "-o", str(tmp_path / "run"), str(tmp_path / "main.rs")],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr[-2000:]
	ran = subprocess.run([str(tmp_path / "run")], capture_output=True,
	                     text=True)
	assert int(ran.stdout) == 0xFFFF


@pytest.mark.parametrize("flags", sorted(EXPECTED))
def test_the_walker_kernel_reads_both_flags(flags: str) -> None:
	"""The editor recomputes through this; it complemented every sum."""
	schema = parse_text(kernel_schema(flags).replace(
		"struct s { u16 a; }",
		"struct s {\n\tauthenticated summed { u16 a; }\n"
		"\tchecksum u8 sum[2] covers(summed) is sum16;\n}"))
	image = load(pack(schema, resolve(schema, solve(schema)),
	                  metadata=True)[0])
	# Tags are keyed by the tag's placement index, and there is one.
	(tag,) = image.tags
	assert walker_derived.compute(image, tag, ZERO_SUM) == EXPECTED[flags]


def checksum_schema(flags: str) -> str:
	return ("target buffer;\nendian big;\n"
	        f"codec sum16 {{ kernel = ones_complement(width = 16{flags}); }}\n"
	        "impl sum16 derived;\n"
	        "struct d {\n"
	        "\tauthenticated summed { u16 a; }\n"
	        "\tchecksum u8 sum[2] covers(summed) is sum16;\n"
	        "}\n")


def _python_view(flags: str, data: bytes,
		tmp_path: Path) -> tuple[Any, Any]:
	from situc.codegen.python import generate as generate_py
	schema   = parse_text(checksum_schema(flags))
	resolved = resolve(schema, solve(schema))
	(tmp_path / "zv_unit.py").write_text(
		generate_py(schema, resolved, "unit").module, encoding="ascii")
	(tmp_path / "unit_derived.py").write_text(
		py_derived.generate(schema, "unit"), encoding="ascii")
	shutil.copy(ROOT / "runtime" / "python" / "situ_runtime.py", tmp_path)
	sys.path.insert(0, str(tmp_path))
	saved = {name: sys.modules.pop(name, None)
	         for name in ("situ_runtime", "unit_derived")}
	try:
		spec = importlib.util.spec_from_file_location(
			"zv_unit", tmp_path / "zv_unit.py")
		assert spec is not None and spec.loader is not None
		module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(module)
		runtime = sys.modules["situ_runtime"]
	finally:
		sys.path.remove(str(tmp_path))
		for name, held in saved.items():
			sys.modules.pop(name, None)
			if held is not None:
				sys.modules[name] = held
	view = module.d.at(runtime.Message(bytearray(data)), 0)
	return view, runtime


@pytest.mark.parametrize("flags, accepted", [
	(", complement, zero_means_none", True),
	(", complement", False),
])
def test_python_verify_accepts_a_stored_zero_only_with_the_flag(
		flags: str, accepted: bool, tmp_path: Path) -> None:
	"""A stored zero is "no sum sent" for UDP and a corrupt sum for TCP."""
	view, runtime = _python_view(flags, bytes([0x12, 0x34, 0x00, 0x00]),
	                             tmp_path)
	if accepted:
		view.sum_check()
	else:
		with pytest.raises(runtime.ChecksumError):
			view.sum_check()


@pytest.mark.parametrize("target, accepting", [
	("c",    "== 0u ||"),
	("cpp",  "== 0u ||"),
	("rust", "held == 0 ||"),
])
def test_the_compiled_backends_accept_a_zero_only_with_the_flag(
		target: str, accepting: str) -> None:
	from situc.codegen.c import generate as generate_c
	from situc.codegen.cpp import generate as generate_cpp
	from situc.codegen.rust import generate as generate_rs
	for flags, expected in ((", complement, zero_means_none", True),
	                        (", complement", False)):
		schema   = parse_text(checksum_schema(flags))
		resolved = resolve(schema, solve(schema))
		if target == "c":
			text = generate_c(schema, resolved, "unit").header
		elif target == "cpp":
			text = generate_cpp(schema, resolved, "unit").header
		else:
			text = generate_rs(schema, resolved, "unit").module
		assert (accepting in text) is expected, (target, flags)
