"""The offset cache places each member where its accessor does.

`--materialize` resolves every dynamic offset in one pass, and nothing held
that pass to the per-member `_offset` it replaces. On radiotap it was wrong
twice: the 32 present bits were summed as nothing, each being under a byte,
and an `[align]` pad in an arm was summed as nothing too, so `channel` behind
one byte of flags was cached at 5 past the header where it starts at 6
(26.928's note). The message is worked by hand: present = flags and channel,
flags at byte 8, one byte of pad, the channel at 10, whose frequency is
0x096c, 2412 MHz. C++ and Rust read the frequency through the accessor
beside the cached offset, so a cache and an accessor that agree on a wrong
place still fail against the hand-worked one.
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
HOST_CXX = shutil.which("g++") or shutil.which("clang++")
RUSTC   = shutil.which("rustc")

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


CPP_PROBE = """#include <cstdio>
#include "radiotap.hpp"
int main()
{
	std::uint8_t buf[14] = { %s };
	::situ::rt::message msg(buf, sizeof buf);
	::situ::radiotap view;
	if (::situ::radiotap::at(msg, 0, sizeof buf, view) != ::situ::rt::err::ok)
		return 1;
	::situ::radiotap::offsets o;
	view.resolve_offsets(o);
	::situ::channel channel;
	if (view.channel_field_channel_data(channel) != ::situ::rt::err::ok)
		return 2;
	std::printf("%%u %%u\\n", static_cast<unsigned>(o.channel_field),
	            static_cast<unsigned>(channel.frequency()));
	return 0;
}
"""


@pytest.mark.skipif(HOST_CC is None or HOST_CXX is None,
                    reason="no host C and C++ compilers")
def test_cpp_caches_the_offset_its_accessor_gives(tmp_path: Path) -> None:
	_build("cpp", tmp_path)
	probe = tmp_path / "probe.cpp"
	probe.write_text(CPP_PROBE % ", ".join(str(byte) for byte in MESSAGE),
	                 encoding="ascii")
	assert HOST_CXX is not None
	built = subprocess.run(
		[HOST_CXX, "-std=c++17", "-Wall", "-Wextra", "-Werror",
		 f"-I{RUNTIME / 'c'}", f"-I{RUNTIME / 'cpp'}", f"-I{tmp_path}",
		 str(probe), str(RUNTIME / "c" / "situ.c"),
		 "-o", str(tmp_path / "probe")],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr[-2000:]
	ran = subprocess.run([str(tmp_path / "probe")], capture_output=True,
	                     text=True, timeout=60)
	assert ran.stdout == "10 2412\n"


RUST_PROBE = """mod situ_rt;
mod radiotap;
fn main() {
	let buf: [u8; 14] = [%s];
	let view = radiotap::Radiotap::new(&buf).unwrap();
	let o = view.resolve_offsets();
	let channel = view.channel_field_channel_data().unwrap();
	println!("{} {}", o.channel_field, channel.frequency());
}
"""


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_rust_caches_the_offset_its_accessor_gives(tmp_path: Path) -> None:
	_build("rust", tmp_path)
	(tmp_path / "situ_rt.rs").write_text(
		(RUNTIME / "rust" / "situ_rt.rs").read_text(encoding="ascii")
		.replace("#![no_std]\n", ""), encoding="ascii")
	(tmp_path / "main.rs").write_text(
		RUST_PROBE % ", ".join(str(byte) for byte in MESSAGE),
		encoding="ascii")
	assert RUSTC is not None
	built = subprocess.run(
		[RUSTC, "--edition", "2021", "-A", "warnings",
		 str(tmp_path / "main.rs"), "-o", str(tmp_path / "probe")],
		capture_output=True, text=True, cwd=tmp_path, timeout=600)
	assert built.returncode == 0, built.stderr[-2000:]
	ran = subprocess.run([str(tmp_path / "probe")], capture_output=True,
	                     text=True, timeout=60)
	assert ran.stdout == "10 2412\n"
