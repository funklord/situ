"""What a size expression turns into, in a language that has to run it.

Every backend carried the expression through as source text and emitted it
unchanged, so `align_up(n, 4)` arrived in four languages as a call to a
function that exists in none of them, and the generated code did not compile.
Division was the other half: `/` truncates in C, C++ and Rust and returns a
float in Python, so `body[n / 2]` produced a slice bound of `2.5`.

Neither had been noticed because no schema in the repository used a builtin or
a division in a size -- which is a fact about which schemas somebody wrote, not
about what the language offers. The measurement is in 26.37: across every
schema this repository builds, the only operators that ever reached a
backend's renderer were `+`, `-` and `*`.

These run the arithmetic rather than reading it. Compiling proves the names
exist; the answers prove the rounding.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from situc.codegen.c import generate as generate_c
from situc.codegen.python import generate as generate_py
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve

from every_schema import ROOT

RUNTIME = ROOT / "runtime"
HOST_CC = shutil.which("gcc") or shutil.which("cc")

WARNINGS = [
	"-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
	"-Wconversion", "-Wsign-conversion",
]

#: A netlink attribute in miniature: a length that counts its own header, a
#: payload the length decides, and the padding that rounds the whole thing up
#: to four bytes. `align_up(n, 4) - n` is the kernel's `nla_padlen`.
PADDED = """target buffer;
endian big;
struct s {
	u16	n	[min = 4, max = 64];
	u8	payload[n - 4];
	reserved u8 [align_up(n, 4) - n];
	u16	tail;
}
"""

#: `n = 7`: three bytes of payload, one of padding, and the tail at 6.
BYTES = bytes([0x00, 0x07, 0x11, 0x22, 0x33, 0x00, 0xBE, 0xEF])
#: The same, with the pad byte holding something a sender should not put there.
DIRTY = bytes([0x00, 0x07, 0x11, 0x22, 0x33, 0x99, 0xBE, 0xEF])


def python_module(tmp_path: Path, source: str) -> ModuleType:
	schema   = parse_text(source)
	resolved = resolve(schema, solve(schema))
	(tmp_path / "unit.py").write_text(
		generate_py(schema, resolved, "unit").module, encoding="ascii")

	spec = importlib.util.spec_from_file_location(
		"situ_runtime", RUNTIME / "python" / "situ_runtime.py")
	assert spec is not None and spec.loader is not None
	if "situ_runtime" not in sys.modules:
		runtime = importlib.util.module_from_spec(spec)
		sys.modules["situ_runtime"] = runtime
		spec.loader.exec_module(runtime)

	sys.path.insert(0, str(tmp_path))
	try:
		spec = importlib.util.spec_from_file_location("unit", tmp_path / "unit.py")
		assert spec is not None and spec.loader is not None
		module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(module)
		return module
	finally:
		sys.path.remove(str(tmp_path))


def test_python_rounds_the_padding_and_finds_the_tail(tmp_path: Path) -> None:
	module  = python_module(tmp_path, PADDED)
	runtime = sys.modules["situ_runtime"]

	message = runtime.Message(bytearray(BYTES))
	view    = module.s.at(message, 0, len(message.buffer))

	assert bytes(view.payload) == b"\x11\x22\x33"
	assert view.tail == 0xBEEF
	view.validate()


def test_python_refuses_padding_that_is_not_zero(tmp_path: Path) -> None:
	"""8.8's malleability argument, on bytes whose count the message chose.
	The run was skipped entirely here and crashed the compiler in the other
	three, so nothing enforced it anywhere."""
	module  = python_module(tmp_path, PADDED)
	runtime = sys.modules["situ_runtime"]

	view = module.s.at(runtime.Message(bytearray(DIRTY)), 0, len(DIRTY))
	with pytest.raises(runtime.ConstraintError):
		view.validate()


def test_python_divides_as_the_other_three_do(tmp_path: Path) -> None:
	"""`n / 2` was float division: a slice bound of `2.5`, and every offset
	after it a float."""
	module = python_module(tmp_path, """target buffer;
endian big;
struct s {
	u16	n	[min = 2, max = 64];
	u8	body[n / 2];
	u16	tail;
}
""")
	runtime = sys.modules["situ_runtime"]
	message = runtime.Message(bytearray([0x00, 0x05, 1, 2, 0xBE, 0xEF]))
	view    = module.s.at(message, 0, len(message.buffer))

	assert bytes(view.body) == b"\x01\x02"		# 5 / 2 == 2, not 2.5
	assert view.tail == 0xBEEF


PROBE = """
#include <string.h>
#include "unit.h"

int main(void)
{
	static const uint8_t good[] = {0x00, 0x07, 0x11, 0x22, 0x33, 0x00, 0xBE, 0xEF};
	static const uint8_t bad[]  = {0x00, 0x07, 0x11, 0x22, 0x33, 0x99, 0xBE, 0xEF};
	situ_msg_t  msg;
	situ_view_t view;

	msg.base = (uint8_t *)good;
	msg.size = sizeof good;
	msg.generation = 0u;
	msg.dirty = 0u;
	if (situ_s_view(&msg, 0u, sizeof good, &view) != SITU_OK) {
		return 1;
	}
	if (situ_s_tail_get(view) != 0xBEEFu) {
		return 2;
	}
	if (situ_s_validate(view) != SITU_OK) {
		return 3;
	}

	msg.base = (uint8_t *)bad;
	if (situ_s_view(&msg, 0u, sizeof bad, &view) != SITU_OK) {
		return 4;
	}
	if (situ_s_validate(view) != SITU_ERR_CONSTRAINT) {
		return 5;
	}
	return 0;
}
"""


@pytest.mark.skipif(HOST_CC is None, reason="no host compiler")
def test_c_rounds_the_padding_and_finds_the_tail(tmp_path: Path) -> None:
	"""Compiled *and run*: a probe that only compiles asserts that the names
	exist, which for arithmetic is the half that was never in doubt."""
	schema    = parse_text(PADDED)
	resolved  = resolve(schema, solve(schema))
	generated = generate_c(schema, resolved, "unit")

	(tmp_path / "unit.h").write_text(generated.header, encoding="ascii")
	(tmp_path / "unit.c").write_text(generated.source, encoding="ascii")
	(tmp_path / "probe.c").write_text(PROBE, encoding="ascii")

	binary = tmp_path / "probe"
	build  = subprocess.run(
		[HOST_CC or "cc", *WARNINGS, f"-I{RUNTIME / 'c'}", f"-I{tmp_path}",
		 str(tmp_path / "probe.c"), str(tmp_path / "unit.c"),
		 str(RUNTIME / "c" / "situ.c"), "-o", str(binary)],
		capture_output=True, text=True)
	assert build.returncode == 0, build.stderr

	run = subprocess.run([str(binary)], capture_output=True)
	assert run.returncode == 0, f"probe returned {run.returncode}"
