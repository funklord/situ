"""A trailing pad whose length the struct's own framing computes (26.1004).

`reserved u8 [align_up(nla_len, 4) - nla_len]` was refused by every builder:
the expression calls `align_up`, so it cannot be run backwards to solve for a
field. Nothing needs solving -- every byte before the pad is down when the
writer reaches it, so the struct's own `_required` says where it ends, the
reader's framing rather than a second copy of the expression. Netlink
attributes and tar entries are the real formats it reaches.

Held to outside truth: the kernel's `NLA_ALIGN` for netlink, and Python's
`tarfile`, which writes a member as its header, its data and zeros to the
next 512-byte block.
"""

from __future__ import annotations

import importlib
import io
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

ROOT     = Path(__file__).resolve().parents[2]
COMPILER = shutil.which("cc") or shutil.which("gcc")


def _python(tmp_path: Path, schema: str) -> Any:
	out = tmp_path / "gen"
	done = subprocess.run(
		[sys.executable, "-m", "situc", "build", str(ROOT / schema),
		 "--target", "python", "--layer", "edit", "--out", str(out)],
		cwd = ROOT, capture_output = True, text = True)
	assert done.returncode == 0, done.stderr
	stem = Path(schema).stem
	sys.path[:0] = [str(out), str(ROOT / "runtime" / "python")]
	for name in (stem, f"{stem}_build"):
		sys.modules.pop(name, None)
	try:
		return importlib.import_module(f"{stem}_build")
	finally:
		del sys.path[:2]
		for name in (stem, f"{stem}_build"):
			sys.modules.pop(name, None)


def test_a_netlink_attribute_is_padded_to_four(tmp_path: Path) -> None:
	"""`nla_len` counts the header and payload and not the pad, and the
	next attribute starts at `NLA_ALIGN(nla_len)` (netlink.h:1319)."""
	build = _python(tmp_path, "example/netlink/netlink.situ")
	for size in range(0, 9):
		payload = bytes(range(1, size + 1))
		out = bytearray(32)
		n = build.build_nlattr(out, 2, payload)
		nla_len = 4 + size
		assert n == (nla_len + 3) // 4 * 4, size
		assert int.from_bytes(out[:2], sys.byteorder) == nla_len
		assert bytes(out[4:4 + size]) == payload
		assert bytes(out[nla_len:n]) == bytes(n - nla_len)


def test_a_tar_entry_is_padded_as_tarfile_pads_it(tmp_path: Path) -> None:
	build = _python(tmp_path, "example/ustar/ustar.situ")
	for size in (0, 1, 511, 512, 513):
		data = bytes((i * 7) & 0xFF for i in range(size))
		info = tarfile.TarInfo("situ.bin")
		info.size = size
		info.mtime = 0
		header = info.tobuf(format = tarfile.USTAR_FORMAT)
		wanted = io.BytesIO()
		with tarfile.open(fileobj = wanted, mode = "w",
		                  format = tarfile.USTAR_FORMAT) as archive:
			archive.addfile(info, io.BytesIO(data))
		member = wanted.getvalue()[:512 + (size + 511) // 512 * 512]
		out = bytearray(2048)
		n = build.build_ustar_entry(out, header, data)
		assert bytes(out[:n]) == member, size


def test_c_pads_a_netlink_attribute(tmp_path: Path) -> None:
	if COMPILER is None:
		return
	out = tmp_path / "gen"
	done = subprocess.run(
		[sys.executable, "-m", "situc", "build",
		 str(ROOT / "example/netlink/netlink.situ"), "--target", "c",
		 "--layer", "edit", "--out", str(out)],
		cwd = ROOT, capture_output = True, text = True)
	assert done.returncode == 0, done.stderr
	(tmp_path / "drive.c").write_text("""#include <string.h>
#include "netlink_build.h"

int main(void)
{
	static const uint8_t payload[5] = { 1, 2, 3, 4, 5 };
	uint8_t  buf[32];
	uint32_t wrote = 0;

	memset(buf, 0xAA, sizeof buf);
	if (situ_nlattr_build(buf, sizeof buf, (situ_ifa_attr_t)2, payload, 5u,
	                      &wrote) != SITU_OK || wrote != 12u
	    || buf[9] != 0u || buf[10] != 0u || buf[11] != 0u) {
		return 1;
	}
	if (situ_nlattr_build(buf, 10u, (situ_ifa_attr_t)2, payload, 5u,
	                      &wrote) != SITU_ERR_BOUNDS) {
		return 2;
	}
	return 0;
}
""", encoding = "ascii")
	runtime = ROOT / "runtime" / "c"
	compiled = subprocess.run(
		[COMPILER, "-std=c11", "-pedantic-errors", "-Wall", "-Wextra",
		 "-Werror", "-Wconversion", "-Wsign-conversion", f"-I{runtime}",
		 f"-I{out}", str(tmp_path / "drive.c"), str(out / "netlink.c"),
		 str(runtime / "situ.c"), "-o", str(tmp_path / "drive")],
		capture_output = True, text = True)
	assert compiled.returncode == 0, compiled.stderr
	ran = subprocess.run([str(tmp_path / "drive")], capture_output = True)
	assert ran.returncode == 0, f"check {ran.returncode} failed"
