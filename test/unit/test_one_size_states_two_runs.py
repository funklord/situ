"""One size field stating two byte runs (26.1006).

ARP's `hardware_length` is the length of both the sender's and the target's
hardware address, and every builder refused `arp_generic`: one value cannot
say both. It can, where the two are as long as each other -- which is what
the field claims. The field is computed from the first, and the second is
refused unless it matches.

Held to RFC 826's request: Ethernet, IPv4, "who has 192.168.1.1, tell
192.168.1.2", with the target hardware address all zeros.
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT     = Path(__file__).resolve().parents[2]
COMPILER = shutil.which("cc") or shutil.which("gcc")
SENDER   = bytes.fromhex("aabbccddeeff")
REQUEST  = bytes.fromhex("0001" "0800" "06" "04" "0001" "aabbccddeeff"
                         "c0a80102" "000000000000" "c0a80101")


def test_python_writes_an_arp_request(tmp_path: Path) -> None:
	out = tmp_path / "gen"
	done = subprocess.run(
		[sys.executable, "-m", "situc", "build",
		 str(ROOT / "example/arp/arp.situ"), "--target", "python",
		 "--layer", "edit", "--out", str(out)],
		cwd = ROOT, capture_output = True, text = True)
	assert done.returncode == 0, done.stderr
	sys.path[:0] = [str(out), str(ROOT / "runtime" / "python")]
	for name in ("arp", "arp_build"):
		sys.modules.pop(name, None)
	try:
		build = importlib.import_module("arp_build")
	finally:
		del sys.path[:2]
		for name in ("arp", "arp_build"):
			sys.modules.pop(name, None)
	buf = bytearray(64)
	n = build.build_arp_generic(buf, 1, 0x0800, 1, SENDER,
	                            bytes([192, 168, 1, 2]), bytes(6),
	                            bytes([192, 168, 1, 1]))
	assert bytes(buf[:n]) == REQUEST
	with pytest.raises(build.ConstraintError):
		build.build_arp_generic(bytearray(64), 1, 0x0800, 1, SENDER,
		                        bytes(4), bytes(5), bytes(4))


def test_c_refuses_two_lengths(tmp_path: Path) -> None:
	if COMPILER is None:
		return
	out = tmp_path / "gen"
	done = subprocess.run(
		[sys.executable, "-m", "situc", "build",
		 str(ROOT / "example/arp/arp.situ"), "--target", "c",
		 "--layer", "edit", "--out", str(out)],
		cwd = ROOT, capture_output = True, text = True)
	assert done.returncode == 0, done.stderr
	(tmp_path / "drive.c").write_text("""#include <string.h>
#include "arp_build.h"

int main(void)
{
	static const uint8_t mac[6] = { 0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xff };
	static const uint8_t zero[6] = { 0 };
	static const uint8_t me[4] = { 192, 168, 1, 2 }, you[4] = { 192, 168, 1, 1 };
	static const uint8_t want[28] = {
		0x00, 0x01, 0x08, 0x00, 0x06, 0x04, 0x00, 0x01,
		0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xff, 192, 168, 1, 2,
		0, 0, 0, 0, 0, 0, 192, 168, 1, 1 };
	uint8_t  buf[64];
	uint32_t wrote = 0;

	if (situ_arp_generic_build(buf, sizeof buf, (situ_hardware_type_t)1,
	                           0x0800u, (situ_operation_t)1, mac, 6u, me, 4u,
	                           zero, 6u, you, 4u, &wrote) != SITU_OK
	    || wrote != 28u || memcmp(buf, want, 28u) != 0) {
		return 1;
	}
	if (situ_arp_generic_build(buf, sizeof buf, (situ_hardware_type_t)1,
	                           0x0800u, (situ_operation_t)1, mac, 6u, me, 4u,
	                           zero, 5u, you, 4u, &wrote)
	    != SITU_ERR_CONSTRAINT) {
		return 2;
	}
	return 0;
}
""", encoding = "ascii")
	runtime = ROOT / "runtime" / "c"
	compiled = subprocess.run(
		[COMPILER, "-std=c11", "-pedantic-errors", "-Wall", "-Wextra",
		 "-Werror", "-Wconversion", "-Wsign-conversion", f"-I{runtime}",
		 f"-I{out}", str(tmp_path / "drive.c"), str(out / "arp.c"),
		 str(runtime / "situ.c"), "-o", str(tmp_path / "drive")],
		capture_output = True, text = True)
	assert compiled.returncode == 0, compiled.stderr
	ran = subprocess.run([str(tmp_path / "drive")], capture_output = True)
	assert ran.returncode == 0, f"check {ran.returncode} failed"
