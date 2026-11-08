"""The offset cache places each member where its accessor does.

`--materialize` resolves every dynamic offset in one pass, and nothing held
that pass to the per-member `_offset` it replaces. On radiotap it was wrong
twice: the 32 present bits were summed as nothing, each being under a byte,
and an `[align]` pad in an arm was summed as nothing too, so `channel` behind
one byte of flags was cached at 5 past the header where it starts at 6
(26.928's note). The message is worked by hand: present = flags and channel,
flags at byte 8, one byte of pad, the channel at 10.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT    = Path(__file__).resolve().parents[2]
SCHEMA  = ROOT / "example" / "radiotap" / "radiotap.situ"
RUNTIME = ROOT / "runtime"
HOST_CC = shutil.which("gcc") or shutil.which("cc")

MESSAGE = bytes([0, 0, 14, 0, 0x0A, 0, 0, 0, 0x10, 0xEE,
                 0x6C, 0x09, 0xA0, 0x00])

C_PROBE = """#include <stdio.h>
#include "radiotap.h"
int main(void)
{
	uint8_t buf[14] = { %s };
	situ_msg_t msg; situ_view_t view; situ_radiotap_offsets_t o;
	situ_msg_init(&msg, buf, sizeof buf);
	if (situ_radiotap_view(&msg, 0, sizeof buf, &view) != SITU_OK) return 1;
	situ_radiotap_offsets(view, &o);
	printf("%%u %%u %%d\\n", o.channel_field,
	       situ_radiotap_channel_field_channel_data_offset(view),
	       (int)situ_radiotap_validate(view));
	return 0;
}
"""


def _build(target: str, out: Path) -> None:
	done = subprocess.run(
		[sys.executable, "-m", "situc", "build", str(SCHEMA), "--target",
		 target, "--materialize", "--out", str(out)],
		capture_output=True, text=True, cwd=ROOT, timeout=600)
	assert done.returncode == 0, done.stderr[-700:]


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
def test_c_caches_the_offset_its_accessor_gives(tmp_path: Path) -> None:
	_build("c", tmp_path)
	probe = tmp_path / "probe.c"
	probe.write_text(C_PROBE % ", ".join(str(byte) for byte in MESSAGE),
	                 encoding="ascii")
	assert HOST_CC is not None
	built = subprocess.run(
		[HOST_CC, "-std=c11", "-Wall", "-Wextra", "-Wconversion",
		 "-Wsign-conversion", "-Werror", f"-I{RUNTIME / 'c'}",
		 f"-I{tmp_path}", str(probe), str(tmp_path / "radiotap.c"),
		 str(RUNTIME / "c" / "situ.c"), "-o", str(tmp_path / "probe")],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr[-2000:]
	ran = subprocess.run([str(tmp_path / "probe")], capture_output=True,
	                     text=True, timeout=60)
	assert ran.stdout == "10 10 0\n"


def test_python_caches_the_offset_its_accessor_gives(tmp_path: Path) -> None:
	_build("python", tmp_path)
	ran = subprocess.run(
		[sys.executable, "-I", "-c",
		 "import sys; sys.path[:0] = sys.argv[1:3]\n"
		 "import radiotap\n"
		 "from situ_runtime import Message\n"
		 f"buf = bytearray({list(MESSAGE)!r})\n"
		 "v = radiotap.radiotap.at(Message(buf), 0, len(buf))\n"
		 "print(v.resolve_offsets()['channel_field'],"
		 " v.channel_field_channel_data._at)",
		 str(tmp_path), str(RUNTIME / "python")],
		capture_output=True, text=True, timeout=120)
	assert ran.stdout == "10 10\n", ran.stderr[-1500:]
