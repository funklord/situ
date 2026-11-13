"""A builder writes a whole-byte width the runtime has no word for (26.1003).

Every builder refused a `u24`: "whole bytes and not a width the runtime
stores". A TLS handshake's length and a FLAC metadata block's are 24 bits,
so neither could be built. Written now a byte at a time, unsigned and in a
stated order, and refused rather than truncated where the value is wider
than the field -- the parameter is the next word up.

`test_the_four_builders_agree` reaches `tls` and `flac` and holds the four
to each other; this holds them to the bytes.
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT     = Path(__file__).resolve().parents[2]
COMPILER = shutil.which("cc") or shutil.which("gcc")

SCHEMA = """target buffer;
struct both {
	u24 big [endian = big];
	u24 little [endian = little];
	u40 wide [endian = big];
}
"""


def _built(tmp_path: Path, source: Path, target: str) -> Path:
	out = tmp_path / target
	done = subprocess.run(
		[sys.executable, "-m", "situc", "build", str(source), "--target",
		 target, "--layer", "edit", "--out", str(out)],
		cwd = ROOT, capture_output = True, text = True)
	assert done.returncode == 0, done.stderr
	return out


def _python(tmp_path: Path, source: Path) -> Any:
	out = _built(tmp_path, source, "python")
	sys.path[:0] = [str(out), str(ROOT / "runtime" / "python")]
	for name in (source.stem, f"{source.stem}_build"):
		sys.modules.pop(name, None)
	try:
		return importlib.import_module(f"{source.stem}_build")
	finally:
		del sys.path[:2]
		# Out of the import cache again, so another test in this process
		# importing a module of the same name gets its own.
		for name in (source.stem, f"{source.stem}_build"):
			sys.modules.pop(name, None)


def test_a_tls_handshake_states_its_length_in_three_bytes(
		tmp_path: Path) -> None:
	"""RFC 8446 section 4: `HandshakeType msg_type; uint24 length;`. A
	ClientHello's body is opaque here, so any bytes stand for one."""
	build = _python(tmp_path, ROOT / "example/tls/tls.situ")
	body  = bytes([0x03, 0x03]) + bytes(32) + bytes([0, 0, 2, 0x13, 0x01,
	                                                 1, 0, 0, 0])
	out   = bytearray(64)
	n     = build.build_handshake(out, 1, body)
	assert bytes(out[:4]) == bytes([1, 0x00, 0x00, len(body)])
	assert bytes(out[4:n]) == body


def test_each_order_and_a_wider_value(tmp_path: Path) -> None:
	source = tmp_path / "both.situ"
	source.write_text(SCHEMA, encoding = "ascii")
	build = _python(tmp_path, source)
	out   = bytearray(16)
	n     = build.build_both(out, 0x123456, 0x123456, 0x0102030405)
	assert bytes(out[:n]) == bytes.fromhex("123456 563412 0102030405")
	with pytest.raises((build.ConstraintError, OverflowError)):
		build.build_both(bytearray(16), 0x1000000, 0, 0)


@pytest.mark.skipif(COMPILER is None, reason = "needs a C compiler")
def test_c_writes_them_and_refuses_one_too_wide(tmp_path: Path) -> None:
	source = tmp_path / "both.situ"
	source.write_text(SCHEMA, encoding = "ascii")
	out = _built(tmp_path, source, "c")
	(tmp_path / "drive.c").write_text("""#include <string.h>
#include "both_build.h"

int main(void)
{
	static const uint8_t want[11] = {
		0x12, 0x34, 0x56, 0x56, 0x34, 0x12, 0x01, 0x02, 0x03, 0x04, 0x05 };
	uint8_t  buf[16];
	uint32_t wrote = 0;

	if (situ_both_build(buf, sizeof buf, 0x123456u, 0x123456u,
	                    0x0102030405u, &wrote) != SITU_OK
	    || wrote != 11u || memcmp(buf, want, 11u) != 0) {
		return 1;
	}
	if (situ_both_build(buf, sizeof buf, 0x1000000u, 0u, 0u, &wrote)
	    != SITU_ERR_CONSTRAINT) {
		return 2;
	}
	if (situ_both_build(buf, sizeof buf, 0u, 0u, 0x10000000000u, &wrote)
	    != SITU_ERR_CONSTRAINT) {
		return 3;
	}
	return 0;
}
""", encoding = "ascii")
	runtime = ROOT / "runtime" / "c"
	compiled = subprocess.run(
		[COMPILER or "cc", "-std=c11", "-pedantic-errors", "-Wall", "-Wextra",
		 "-Werror", "-Wconversion", "-Wsign-conversion", f"-I{runtime}",
		 f"-I{out}", str(tmp_path / "drive.c"), str(out / "both.c"),
		 str(runtime / "situ.c"), "-o", str(tmp_path / "drive")],
		capture_output = True, text = True)
	assert compiled.returncode == 0, compiled.stderr
	ran = subprocess.run([str(tmp_path / "drive")], capture_output = True)
	assert ran.returncode == 0, f"check {ran.returncode} failed"
