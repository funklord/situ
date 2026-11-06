"""`byte_sum`: the bytes added up, negated where they sum to zero (26.893).

ACPI, the SMBIOS entry point, Intel HEX and openmlx4's FS2 all state a checksum
this way, and it had no spelling: `require` is compile-time and the arithmetic
workaround ran out at a sibling expression. The Intel HEX record is the
published worked example, so its last byte is not situ's.
"""

from __future__ import annotations

import ctypes
import importlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from situc.diagnostics import SituError
from situc.parser import parse_text
from walker.derived import byte_sum as derived_sum
from walker.image import KERNEL_BYTE_SUM, Kernel

ROOT = Path(__file__).resolve().parents[2]

HEAD = "target buffer;\nendian big;\n"
SUM8 = ("codec sum8 { kernel = byte_sum(width = 8, negate); }\n"
        "impl sum8 derived;\n")

#: `:10 0100 00 214601360121470136007EFE09D21901 40`: the record's own bytes
#: and its published checksum.
HEX = bytes.fromhex("10010000214601360121470136007EFE09D2190140")

RECORD = HEAD + SUM8 + """struct record {
	authenticated body {
		u8  length;
		u16 address;
		u8  kind;
		u8  data[length];
	}
	checksum u8 sum[1] covers(body) is sum8;
}
"""

#: A table header, whose checksum is inside what it covers.
TABLE = HEAD + SUM8 + """struct table {
	authenticated whole {
		u8 signature[4];
		u8 length;
		checksum u8 sum[1] covers(whole) is sum8 [self_as = 0];
		u8 body[length];
	}
}
"""


def checked(tmp_path: Path, text: str, name: str, packet: bytes) -> bool:
	"""What the generated reader's own check says of `packet`."""
	source = tmp_path / "bs.situ"
	source.write_text(text, encoding="ascii")
	subprocess.run([sys.executable, "-m", "situc", "build", str(source),
	                "--target", "python", "--out", str(tmp_path)],
	               check=True, capture_output=True, cwd=ROOT)
	sys.path[:0] = [str(tmp_path), str(ROOT / "runtime" / "python")]
	try:
		sys.modules.pop("bs", None)
		built = importlib.import_module("bs")
		view  = getattr(built, name).at(built.Message(bytearray(packet)), 0,
		                                 len(packet))
		try:
			view.sum_check()
		except built.ChecksumError:
			return False
		return True
	finally:
		del sys.path[:2]
		sys.modules.pop("bs", None)


def test_the_published_record_sums_to_zero() -> None:
	assert sum(HEX) % 256 == 0


def test_the_reader_accepts_the_published_record(tmp_path: Path) -> None:
	assert checked(tmp_path, RECORD, "record", HEX)


def test_the_reader_refuses_a_flipped_byte(tmp_path: Path) -> None:
	broken = bytearray(HEX)
	broken[5] ^= 1
	assert not checked(tmp_path, RECORD, "record", bytes(broken))


def test_a_sum_inside_what_it_covers(tmp_path: Path) -> None:
	"""ACPI's shape: the field reads as zero and brings the total to zero."""
	body  = bytes.fromhex("44534454" "03") + b"\x00" + b"abc"
	fixed = bytearray(body)
	fixed[5] = -sum(body) % 256
	assert sum(fixed) % 256 == 0
	assert checked(tmp_path, TABLE, "table", bytes(fixed))
	fixed[5] ^= 1
	assert not checked(tmp_path, TABLE, "table", bytes(fixed))


@pytest.mark.parametrize("negate", [True, False])
def test_c_and_rust_compute_the_python_sum(tmp_path: Path, negate: bool) -> None:
	"""Each language's own function over the published record's bytes."""
	text = RECORD if negate else RECORD.replace(", negate", "")
	source = tmp_path / "bs.situ"
	source.write_text(text, encoding="ascii")
	want = (-sum(HEX[:-1]) if negate else sum(HEX[:-1])) & 0xFF
	for target in ("c", "rust"):
		for command in (["gen-derived", str(source), "--target", target,
		                 "--out", str(tmp_path)],
		                ["build", str(source), "--target", target,
		                 "--out", str(tmp_path)]):
			subprocess.run([sys.executable, "-m", "situc", *command],
			               check=True, capture_output=True, cwd=ROOT)

	# Named per case: pytest reuses a passed test's directory, and `dlopen`
	# answers a path it has seen with the library it already holds.
	library = tmp_path / f"bs_{negate}.so"
	subprocess.run(["cc", "-shared", "-fPIC", "-Wall", "-Wextra", "-Werror",
	                "-I", str(ROOT / "runtime" / "c"), "-I", str(tmp_path),
	                "-o", str(library), str(tmp_path / "bs_derived.c")],
	               check=True, capture_output=True)
	function = ctypes.CDLL(str(library)).situ_sum8
	function.restype = ctypes.c_uint8
	assert function(HEX[:-1], ctypes.c_uint32(len(HEX) - 1)) == want

	if shutil.which("rustc") is None:
		return
	rust = tmp_path / "bs_derived.rs"
	rust.write_text(rust.read_text(encoding="ascii") + "fn main() { println!"
	                f'("{{}}", sum8(&{list(HEX[:-1])})); }}\n', encoding="ascii")
	binary = tmp_path / "bs_rust"
	subprocess.run(["rustc", "-A", "warnings", "-o", str(binary), str(rust)],
	               check=True, capture_output=True)
	said = subprocess.run([str(binary)], check=True, capture_output=True,
	                      text=True).stdout
	assert int(said) == want


def test_the_walker_computes_it_too() -> None:
	"""Its own arithmetic, with `negate` carried in the kernel's last word."""
	kernel = Kernel(KERNEL_BYTE_SUM, 8, 0, 0, 0, 0, True)
	assert derived_sum(kernel, HEX[:-1]) == HEX[-1]


@pytest.mark.parametrize("width", [7, 12, 24])
def test_a_width_nobody_stores_is_refused(width: int) -> None:
	with pytest.raises(SituError) as caught:
		parse_text(f"codec odd {{ kernel = byte_sum(width = {width}); }}\n",
		           path="s.situ")
	assert "not 8, 16, 32 or 64 bits" in caught.value.diagnostic.render()
