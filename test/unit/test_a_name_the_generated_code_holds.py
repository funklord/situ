"""Names and shapes that broke one backend alone (26.908)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from struct import pack

import pytest

import fourway
from test_walker_c import (COMPILER, _inline_image, c_verdict,
                           python_verdict, shape_named_text)

ROOT = Path(__file__).resolve().parents[2]
HEAD = "target buffer;\nendian big;\n"


@pytest.mark.parametrize("name", ["leaf", "advance", "View", "Message",
                                  "acquire", "BoundsError"])
def test_python_reads_a_struct_its_runtime_holds(name: str,
		tmp_path: Path) -> None:
	"""The struct keeps its name and the import gives way.

	The size read and `required` are the two that went wrong: the first called
	the class, and a first version of the alias renamed `leaf.required`. mypy
	finds that one without a message reaching it.
	"""
	schema = tmp_path / "unit.situ"
	schema.write_text(HEAD + f"struct {name} {{ u8 n; u8 d[n]; }}\n"
	                  f"struct top {{ u8 c; {name} kids[c]; u32 t; }}\n",
	                  encoding="ascii")
	subprocess.run([str(ROOT / "bin" / "situc"), "build", "--target", "python",
	                "--out", str(tmp_path), str(schema)],
	               check=True, capture_output=True, text=True)
	driver = tmp_path / "drive.py"
	driver.write_text(
		"import sys\n"
		f"sys.path[:0] = [{str(tmp_path)!r}, {str(ROOT / 'runtime/python')!r}]\n"
		"import unit\n"
		"from situ_runtime import Message\n"
		"raw = bytes([2, 1, 9, 2, 7, 8, 0, 0, 0, 5])\n"
		"top = unit.top.at(Message(bytearray(raw)), 0, len(raw))\n"
		f"assert unit.{name}.__mro__[1].__name__ == 'View'\n"
		"assert top.t == 5 and bytes(top.kids(1).d) == bytes([7, 8])\n"
		"assert unit.top.required(raw) == len(raw)\n", encoding="ascii")
	subprocess.run([sys.executable, str(driver)], check=True, cwd=tmp_path,
	               capture_output=True, text=True)
	typed = subprocess.run(
		[sys.executable, "-m", "mypy", "--no-incremental",
		 "--cache-dir=/dev/null", "unit.py"], cwd=tmp_path,
		env={"MYPYPATH": str(ROOT / "runtime/python"), "PATH": "/usr/bin:/bin"},
		capture_output=True, text=True)
	assert typed.returncode == 0, typed.stdout


@pytest.mark.skipif(shutil.which("tshark") is None, reason="no tshark")
@pytest.mark.parametrize("name", ["tree", "tvb", "pinfo", "subtree", "at"])
def test_a_struct_named_for_a_dissector_local_loads(name: str,
		tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(HEAD + f"struct {name} {{ u8 n; u8 d[n]; }}\n",
	                  encoding="ascii")
	subprocess.run([sys.executable, "-m", "situc", "gen-dissector", str(schema),
	                "--out", str(tmp_path)],
	               check=True, cwd=ROOT, capture_output=True)
	packet = bytes.fromhex("020102")
	(tmp_path / "unit.pcap").write_bytes(
		pack("<IHHiIII", 0xa1b2c3d4, 2, 4, 0, 0, 65535, 147)
		+ pack("<IIII", 0, 0, len(packet), len(packet)) + packet)
	ran = subprocess.run(
		["tshark", "-X", f"lua_script:{tmp_path / 'unit.lua'}", "-o",
		 f'uat:user_dlts:"User 0 (DLT=147)","situ_unit_{name}","0","","0",""',
		 "-r", str(tmp_path / "unit.pcap"), "-T", "fields",
		 "-e", f"situ.{name}.n", "-e", f"situ.{name}.d"],
		check=True, capture_output=True, text=True, timeout=60)
	assert ran.stdout.replace(":", "").split() == ["2", "0102"]


BEHIND = HEAD + """
struct named {
	parameter u8 k [stream];
	u8 a[k];
	u8 name[] until ':' max 8;
	u8 after;
}
struct holder { u8 k; named(k = k) one; }
"""

CASES = {"02aabb78793a07": "0", "01aa78": "1"}


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs gcc, g++ and rustc")
def test_a_delimited_member_behind_an_argument_builds_in_all_four(
		tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(BEHIND, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for message in CASES:
		given = {fourway.answers(argv, bytes.fromhex(message), tmp_path)
		         for argv in command.values()}
		assert len(given) == 1, message


@pytest.mark.skipif(COMPILER is None, reason="needs a C compiler")
def test_both_walkers_read_it(tmp_path: Path) -> None:
	image = _inline_image(BEHIND)
	shape = shape_named_text(BEHIND, "holder")
	for message, verdict in CASES.items():
		data = bytes.fromhex(message)
		assert python_verdict(image, data, shape) == verdict, message
		assert c_verdict(tmp_path, image, data, shape) == verdict, message
