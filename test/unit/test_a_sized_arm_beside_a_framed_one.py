"""`required` over a variant whose arms frame themselves differently.

PCI VPD's resource is a large one, a struct that frames itself by a
little-endian `u16` length, or a small one, `u8 small_data[item_low]`, sized
by bits of the tag. Python's `required` framed a variant through the chosen
arm's own `required` only where every arm was a struct or fixed-size, and
otherwise fell back to the arm's EXTENT, which shrinks when the bytes run
out: given `8a 2c`, a large resource and one byte of its length, it read
that byte as the whole length and answered 47, where C, C++ and Rust
answered 3 -- the length is not here yet (26.1052).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from situc.cli import main

ROOT = Path(__file__).resolve().parents[2]

SCHEMA = ("target buffer;\nendian little;\nbit_order msb_first;\n"
          "struct large_body {\n\tu16 length;\n\tu8  data[length];\n}\n"
          "struct resource [allow_straddle] {\n\tbit is_large;\n"
          "\tu4  item_high;\n\tu3  item_low;\n"
          "\tvariant body switch (is_large) {\n"
          "\t\tcase 1:  large_body large;\n"
          "\t\tdefault: u8 small_data[item_low];\n\t}\n}\n")


def test_python_waits_for_the_whole_length(tmp_path: Path) -> None:
	schema = tmp_path / "vpd.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	out = tmp_path / "python"
	assert main(["build", str(schema), "--target", "python", "--out", str(out)]) == 0
	shutil.copy(ROOT / "runtime" / "python" / "situ_runtime.py",
	            out / "situ_runtime.py")
	(out / "main.py").write_text('''import situ_runtime as rt
import vpd
def need(data):
	try:
		return vpd.resource.required(data)
	except rt.TruncatedError as short:
		return f"short {short.needed}"
print(need(bytes([0x8a])))
print(need(bytes([0x8a, 0x2c])))
print(need(bytes([0x8a, 0x02, 0x00, 0xaa, 0xbb])))
print(need(bytes([0x0a, 0x11])))
print(need(bytes([0x0a, 0x11, 0x22])))
''', encoding="ascii")
	ran = subprocess.run([sys.executable, str(out / "main.py")],
	                     capture_output=True, text=True, cwd=out)
	assert ran.stdout.split("\n")[:5] == [
		"short 3", "short 3", "5", "short 3", "3"], ran.stdout + ran.stderr
