"""A builder writes a checksum packed into bits (26.1027).

USB's token packet is a PID byte and sixteen bits holding a seven-bit
address, a four-bit endpoint and `checksum u5 crc`. Every builder refused it:
"a 5-bit tag, and the runtime stores 8, 16, 32 and 64". But the reader
already emits `crc_store`, which puts a value where `crc` reads it, bits and
all. So the group is written with the CRC's bits zero, and once the message
is whole the CRC is computed, stored through the reader and checked. C
only.

Held to two things that are not situ: `2D 00 10`, SETUP to device 0
endpoint 0, the first packet of every USB enumeration; and a CRC5 written
here from the USB 2.0 specification's definition (section 8.3.5.1:
x^5 + x^2 + 1, the register preset to ones, the remainder inverted, bits
LSB first, so the register shifts right against the reflected polynomial)
over every address and a spread of endpoints.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from test_an_arm_sized_by_its_argument_is_written import (COMPILER, ROOT,
                                                          _built)


def _run(tmp_path: Path, out: Path, body: str) -> None:
	"""The shared runner, with the codec's own C source as well: the CRC5
	kernel is in `usb_derived.c`, which `gen-derived` writes."""
	done = subprocess.run(
		[sys.executable, "-m", "situc", "gen-derived",
		 str(ROOT / "example" / "usb" / "usb.situ"), "--out", str(out)],
		cwd = ROOT, capture_output = True, text = True)
	assert done.returncode == 0, done.stderr
	(tmp_path / "drive.c").write_text(
		'#include <stddef.h>\n#include <string.h>\n#include "usb_build.h"\n\n'
		f"int main(void)\n{{\n{body}\treturn 0;\n}}\n", encoding = "ascii")
	runtime = ROOT / "runtime" / "c"
	built = subprocess.run(
		[COMPILER or "cc", "-std=c11", "-pedantic-errors", "-Wall", "-Wextra",
		 "-Werror", "-Wconversion", "-Wsign-conversion", f"-I{runtime}",
		 f"-I{out}", str(tmp_path / "drive.c"), str(out / "usb.c"),
		 str(out / "usb_derived.c"), str(runtime / "situ.c"),
		 "-o", str(tmp_path / "drive")],
		capture_output = True, text = True)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(tmp_path / "drive")], capture_output = True)
	assert ran.returncode == 0, f"check {ran.returncode} failed"


def _crc5(value: int, bits: int = 11) -> int:
	"""USB's CRC5 over `bits` bits of `value`, least significant first."""
	register = 0x1F
	for i in range(bits):
		if (register ^ (value >> i)) & 1:
			register = (register >> 1) ^ 0x14	# 0x05, reflected
		else:
			register >>= 1
	return register ^ 0x1F


def _token(pid: int, address: int, endpoint: int) -> bytes:
	field = address | endpoint << 7
	field |= _crc5(field) << 11
	return bytes([pid, field & 0xFF, field >> 8])


def test_the_oracle_gives_the_setup_everybody_has_seen() -> None:
	assert _token(0x2D, 0, 0) == bytes([0x2D, 0x00, 0x10])


@pytest.mark.skipif(COMPILER is None, reason = "needs a C compiler")
def test_c_writes_every_address(tmp_path: Path) -> None:
	cases = [(address, endpoint) for address in range(128)
	         for endpoint in (0, 1, 5, 15)]
	table = ",\n\t\t".join(
		"{ %d, %d, { %s } }" % (address, endpoint, ", ".join(
			f"0x{byte:02x}" for byte in _token(0x69, address, endpoint)))
		for address, endpoint in cases)
	_run(tmp_path, _built(tmp_path, "usb"), f"""
	static const struct {{ uint8_t address, endpoint, want[3]; }} cases[] = {{
		{table}
	}};
	uint8_t  buf[8];
	uint32_t wrote = 0;

	for (size_t i = 0; i < sizeof cases / sizeof cases[0]; i++) {{
		if (situ_token_build(buf, sizeof buf, SITU_PACKET_ID_IN,
		                     cases[i].address, cases[i].endpoint, &wrote)
		    != SITU_OK || wrote != 3u || memcmp(buf, cases[i].want, 3u) != 0) {{
			return 1;
		}}
	}}
	if (situ_token_build(buf, sizeof buf, SITU_PACKET_ID_SETUP, 0u, 0u,
	                     &wrote) != SITU_OK
	    || buf[0] != 0x2D || buf[1] != 0x00 || buf[2] != 0x10) {{
		return 2;
	}}
""")


@pytest.mark.skipif(COMPILER is None, reason = "needs a C compiler")
def test_an_sd_card_cmd0(tmp_path: Path) -> None:
	"""The other packed CRC in the tree: `edges`' MMC command, a seven-bit
	CRC-7 below a stop bit. CMD0 is `40 00 00 00 00 95` on every SD card
	ever initialised (SD Physical Layer specification, section 4.5)."""
	out = tmp_path / "edges"
	done = subprocess.run(
		[sys.executable, "-m", "situc", "build",
		 str(ROOT / "test" / "schema" / "edges.situ"), "--target", "c",
		 "--layer", "edit", "--out", str(out)],
		cwd = ROOT, capture_output = True, text = True)
	assert done.returncode == 0, done.stderr
	done = subprocess.run(
		[sys.executable, "-m", "situc", "gen-derived",
		 str(ROOT / "test" / "schema" / "edges.situ"), "--out", str(out)],
		cwd = ROOT, capture_output = True, text = True)
	assert done.returncode == 0, done.stderr
	(tmp_path / "drive.c").write_text("""#include <string.h>
#include "edges_build.h"

int main(void)
{
	static const uint8_t want[6] = { 0x40, 0, 0, 0, 0, 0x95 };
	uint8_t  buf[8];
	uint32_t wrote = 0;

	return situ_mmc_command_build(buf, sizeof buf, 0x40u, 0u, &wrote)
	       != SITU_OK || wrote != 6u || memcmp(buf, want, 6u) != 0;
}
""", encoding = "ascii")
	runtime = ROOT / "runtime" / "c"
	derived = [str(path) for path in out.glob("*_derived.c")]
	built = subprocess.run(
		[COMPILER or "cc", "-std=c11", "-w", f"-I{runtime}", f"-I{out}",
		 str(tmp_path / "drive.c"), str(out / "edges.c"), *derived,
		 str(runtime / "situ.c"), "-o", str(tmp_path / "drive")],
		capture_output = True, text = True)
	assert built.returncode == 0, built.stderr
	assert subprocess.run([str(tmp_path / "drive")]).returncode == 0


def test_a_tag_the_runtime_has_no_word_for_stays_refused(
		tmp_path: Path) -> None:
	"""BLE's CRC-24 is three whole bytes, not bits in a group, and the
	runtime stores 8, 16, 32 and 64: still refused, and saying so."""
	schema = tmp_path / "t.situ"
	schema.write_text("""target buffer;
endian big;
codec crc24 {
	kernel = polynomial(width = 24, poly = 0x00065B, init = 0x555555, reflect);
}
impl crc24 derived;
struct framed24 {
	authenticated body {
		u8 x;
	}
	checksum u8 crc[3] covers(body) is crc24;
}
""", encoding = "ascii")
	done = subprocess.run(
		[sys.executable, "-m", "situc", "build", str(schema), "--target",
		 "c", "--layer", "edit", "--out", str(tmp_path / "out")],
		cwd = ROOT, capture_output = True, text = True)
	assert done.returncode == 0, done.stderr
	assert "`crc` is a 24-bit tag, and the runtime stores" in done.stderr, \
		done.stderr
