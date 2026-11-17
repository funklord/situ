"""`covers(before)`: every byte of the message ahead of the tag (26.1050).

PCI VPD's RV is the case: one byte, inside a keyword field nested in a list
of resources, that makes the image from its first byte through itself sum
to zero. `covers` names regions of the struct holding the tag, and RV's
bytes are the message's, wherever the field happens to fall -- openmlx4's
vpd.situ could not state it (inbox 20261114T0041Z).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from situc.cli import main
from situc.diagnostics import SituError
from situc.parser import parse_text

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "runtime"

SCHEMA = ("target buffer;\nendian little;\n"
          "codec vpd_sum {\n\tkernel = byte_sum(width = 8, negate);\n}\n"
          "impl vpd_sum derived;\n"
          "struct rv_body {\n\tchecksum u8 sum[1] covers(before) is vpd_sum;\n}\n"
          "struct field {\n\tu16 kw;\n\tu8  length;\n"
          "\tvariant body switch (kw) {\n"
          "\t\tcase 0x5652: rv_body rv;\n"
          "\t\tdefault:     u8 data[length];\n\t}\n}\n"
          "struct image {\n\tu8    tag;\n\tfield first;\n\tfield second;\n}\n")

def _checksum_code() -> int:
	"""`SITU_ERR_CHECKSUM`'s value, read from the runtime that defines it."""
	found = re.search(r"SITU_ERR_CHECKSUM\s*=\s*(\d+)",
	                  (RUNTIME / "c" / "situ.h").read_text())
	assert found is not None
	return int(found.group(1))


AHEAD = bytes([0x90, 0x41, 0x42, 2, 1, 2, 0x52, 0x56, 1])
GOOD  = AHEAD + bytes([(-sum(AHEAD)) & 0xFF])
BAD   = AHEAD + bytes([(-sum(AHEAD) + 1) & 0xFF])


def test_before_stands_alone() -> None:
	with pytest.raises(SituError, match="cannot be listed with regions"):
		parse_text(SCHEMA.replace("covers(before)", "covers(before, x)"),
		           path="s.situ")


@pytest.mark.skipif(shutil.which("gcc") is None, reason="no C compiler")
def test_c_sums_the_message_ahead_of_the_tag(tmp_path: Path) -> None:
	schema = tmp_path / "rv.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	out = tmp_path / "c"
	assert main(["build", str(schema), "--target", "c", "--out", str(out)]) == 0
	assert main(["gen-derived", str(schema), "--target", "c",
	             "--out", str(out)]) == 0
	rows = ",\n".join("\t{" + ", ".join(str(b) for b in data) + "}"
	                  for data in (GOOD, BAD))
	(out / "main.c").write_text(f'''#include <stdio.h>
#include <string.h>
#include "rv.h"
static const uint8_t cases[2][{len(GOOD)}] = {{
{rows}
}};
int main(void)
{{
	for (unsigned i = 0; i < 2; i++) {{
		uint8_t buf[{len(GOOD)}];
		situ_msg_t msg;
		situ_view_t img, second, rv;
		uint32_t at = 99u, n = 99u, sum = 999u;

		memcpy(buf, cases[i], sizeof buf);
		situ_msg_init(&msg, buf, sizeof buf);
		if (situ_image_view(&msg, 0u, sizeof buf, &img) != SITU_OK
				|| situ_image_second_view(img, &second) != SITU_OK
				|| situ_field_body_rv_view(second, &rv) != SITU_OK) {{
			return 1;
		}}
		const situ_err_t covered = situ_rv_body_sum_covered(rv, &at, &n);
		const situ_err_t computed = situ_rv_body_sum_compute(rv, &sum);
		printf("%d %u %u %d %u %d\\n", (int)covered, at, n, (int)computed,
		       sum, (int)situ_rv_body_sum_check(rv));
	}}
	return 0;
}}
''', encoding="ascii")
	built = subprocess.run(
		["gcc", "-std=c99", "-Os", "-Wall", "-Wextra", "-Werror",
		 f"-I{RUNTIME / 'c'}", f"-I{out}", str(out / "main.c"),
		 str(out / "rv.c"), str(out / "rv_derived.c"),
		 str(RUNTIME / "c" / "situ.c"), "-o", str(out / "r")],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(out / "r")], capture_output=True, text=True)
	checksum = _checksum_code()
	want = GOOD[-1]
	assert ran.stdout.splitlines() == [
		f"0 0 {len(AHEAD)} 0 {want} 0",
		f"0 0 {len(AHEAD)} 0 {want} {checksum}",
	], ran.stdout + ran.stderr


def _c_side_derived(tmp_path: Path, schema: Path) -> Path:
	"""The derived sum's C, built against the C header, for C++ to link."""
	side = tmp_path / "c-side"
	assert main(["build", str(schema), "--target", "c", "--out", str(side)]) == 0
	assert main(["gen-derived", str(schema), "--target", "c",
	             "--out", str(side)]) == 0
	made = subprocess.run(
		["gcc", "-std=c99", "-Os", f"-I{RUNTIME / 'c'}", f"-I{side}", "-c",
		 str(side / "rv_derived.c"), "-o", str(side / "rv_derived.o")],
		capture_output=True, text=True)
	assert made.returncode == 0, made.stderr
	return side / "rv_derived.o"


@pytest.mark.skipif(shutil.which("g++") is None, reason="no C++ compiler")
def test_cpp_sums_the_message_ahead_of_the_tag(tmp_path: Path) -> None:
	schema = tmp_path / "rv.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	out = tmp_path / "cpp"
	assert main(["build", str(schema), "--target", "cpp", "--out", str(out)]) == 0
	derived = _c_side_derived(tmp_path, schema)
	rows = ",\n".join("\t{" + ", ".join(str(b) for b in data) + "}"
	                  for data in (GOOD, BAD))
	(out / "main.cpp").write_text(f'''#include <cstdio>
#include <cstring>
#include "rv.hpp"
static const std::uint8_t cases[2][{len(GOOD)}] = {{
{rows}
}};
int main()
{{
	for (const auto &c : cases) {{
		std::uint8_t buf[{len(GOOD)}];
		std::memcpy(buf, c, sizeof buf);
		situ_msg_t msg;
		situ_view_t raw;
		situ_msg_init(&msg, buf, sizeof buf);
		if (situ_view_at(&msg, 0u, sizeof buf, &raw) != SITU_OK) return 1;
		const ::situ::image img(raw);
		::situ::field second;
		::situ::rv_body rv;
		if (img.second(second) != ::situ::rt::err::ok
				|| second.body_rv(rv) != ::situ::rt::err::ok) return 1;
		std::uint32_t at = 99, n = 99, sum = 999;
		const auto covered = rv.sum_covered(at, n);
		const auto computed = rv.sum_compute(sum);
		std::printf("%d %u %u %d %u %d\\n", (int)covered, at, n, (int)computed,
		            sum, (int)rv.sum_check());
	}}
	return 0;
}}
''', encoding="ascii")
	built = subprocess.run(
		["g++", "-std=c++17", "-Os", "-Wall", "-Wextra", "-Werror",
		 f"-I{RUNTIME / 'c'}", f"-I{RUNTIME / 'cpp'}", f"-I{out}",
		 str(out / "main.cpp"), str(RUNTIME / "c" / "situ.c"), str(derived),
		 "-o", str(out / "r")], capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(out / "r")], capture_output=True, text=True)
	checksum = _checksum_code()
	assert ran.stdout.splitlines() == [
		f"0 0 {len(AHEAD)} 0 {GOOD[-1]} 0",
		f"0 0 {len(AHEAD)} 0 {GOOD[-1]} {checksum}",
	], ran.stdout + ran.stderr


@pytest.mark.skipif(shutil.which("rustc") is None, reason="no Rust compiler")
def test_rust_sums_the_message_ahead_of_the_tag(tmp_path: Path) -> None:
	schema = tmp_path / "rv.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	out = tmp_path / "rust"
	assert main(["build", str(schema), "--target", "rust", "--out", str(out)]) == 0
	shutil.copy(RUNTIME / "rust" / "situ_rt.rs", out / "situ_rt.rs")
	rows = ", ".join("vec![" + ", ".join(str(b) for b in data) + "]"
	                 for data in (GOOD, BAD))
	(out / "main.rs").write_text(f'''mod situ_rt;
#[allow(dead_code)]
mod rv;
fn code(r: Result<(), situ_rt::Error>) -> &'static str {{
	match r {{
		Ok(()) => "ok",
		Err(situ_rt::Error::Checksum) => "checksum",
		Err(_) => "other",
	}}
}}
fn main() {{
	for buf in [{rows}] {{
		let img = rv::Image::new(&buf).unwrap();
		let second = img.second().unwrap();
		let tag = second.body_rv().unwrap();
		let (at, n) = tag.sum_covered_in(&buf).unwrap();
		let sum = tag.sum_compute_in(&buf).unwrap();
		println!("{{}} {{}} {{}} {{}}", at, n, sum, code(tag.sum_check_in(&buf)));
	}}
}}
''', encoding="ascii")
	built = subprocess.run(
		["rustc", "--edition", "2021", "-O", "-o", str(out / "r"),
		 str(out / "main.rs")], capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(out / "r")], capture_output=True, text=True)
	assert ran.stdout.splitlines() == [
		f"0 {len(AHEAD)} {GOOD[-1]} ok",
		f"0 {len(AHEAD)} {GOOD[-1]} checksum",
	], ran.stdout + ran.stderr


def test_python_sums_the_message_ahead_of_the_tag(tmp_path: Path) -> None:
	schema = tmp_path / "rv.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	out = tmp_path / "python"
	assert main(["build", str(schema), "--target", "python", "--out", str(out)]) == 0
	shutil.copy(RUNTIME / "python" / "situ_runtime.py", out / "situ_runtime.py")
	(out / "main.py").write_text(f'''import situ_runtime as rt
import rv
for data in ({GOOD!r}, {BAD!r}):
	img = rv.image.at(rt.Message(bytearray(data)), 0, len(data))
	tag = img.second.body_rv
	try:
		tag.sum_check()
		word = "ok"
	except rt.ChecksumError:
		word = "checksum"
	at, n = tag.sum_covered()
	print(at, n, tag.sum_compute(), word)
''', encoding="ascii")
	ran = subprocess.run([sys.executable, str(out / "main.py")],
	                     capture_output=True, text=True, cwd=out)
	assert ran.stdout.splitlines() == [
		f"0 {len(AHEAD)} {GOOD[-1]} ok",
		f"0 {len(AHEAD)} {GOOD[-1]} checksum",
	], ran.stdout + ran.stderr
	typed = subprocess.run([sys.executable, "-m", "mypy", "--strict",
	                        str(out / "rv.py")], capture_output=True,
	                       text=True, cwd=out)
	assert typed.returncode == 0, typed.stdout


def test_a_write_ahead_of_the_tag_stales_it() -> None:
	"""The lattice half: any byte ahead of the tag is covered by it, in
	whichever struct the write is made, and nothing after it is."""
	from situc.layout import solve
	from situc.resolve import resolve
	schema = parse_text(SCHEMA, path="rv.situ")
	held = {entry.placement.path: entry.placement.covered_by
	        for entry in resolve(schema, solve(schema)).structs["image"].entries}
	both = ("first.body.rv.sum", "second.body.rv.sum")
	assert held["image.tag"] == both
	assert held["image.first.kw"] == both
	assert held["image.second.kw"] == ("second.body.rv.sum",)
	# The tag's own bytes are not its coverage, and nothing follows the
	# last one.
	assert held["image.second.body.rv.sum"] == ()
	assert held["image.first.body.rv.sum"] == ("second.body.rv.sum",)


def test_python_setter_marks_the_tag_stale(tmp_path: Path) -> None:
	schema = tmp_path / "rv.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	out = tmp_path / "python"
	assert main(["build", str(schema), "--target", "python", "--out", str(out)]) == 0
	shutil.copy(RUNTIME / "python" / "situ_runtime.py", out / "situ_runtime.py")
	(out / "main.py").write_text(f'''import situ_runtime as rt
import rv
msg = rt.Message(bytearray({GOOD!r}))
img = rv.image.at(msg, 0, {len(GOOD)})
print(msg.dirty & img.DIRTY_SECOND_BODY_RV_SUM)
img.set_tag(msg, 0x91)
print(msg.dirty & img.DIRTY_SECOND_BODY_RV_SUM == img.DIRTY_SECOND_BODY_RV_SUM)
''', encoding="ascii")
	ran = subprocess.run([sys.executable, str(out / "main.py")],
	                     capture_output=True, text=True, cwd=out)
	assert ran.stdout.split() == ["0", "True"], ran.stdout + ran.stderr


#: openmlx4's VPD as vpd.c walks it, with RV stated: the resource list, the
#: VPD-R resource's bytes held as its keyword fields, RV's value the arm
#: whose first byte is the sum. RV ends VPD-R, which is what `while` says.
VPD = """target buffer;
endian little;
bit_order msb_first;

codec vpd_sum {
	kernel = byte_sum(width = 8, negate);
}
impl vpd_sum derived;

struct rv_value {
	parameter u8 n [stream, min = 1];
	checksum u8 sum[1] covers(before) is vpd_sum;
	u8 reserved[n - 1];
}

struct vpd_field {
	u16  keyword;
	u8   length;
	variant value switch (keyword) {
		case 0x5652: rv_value(n = length) rv;
		default:     u8 data[length];
	}
}

struct vpd_fields {
	vpd_field items[] while (keyword != 0x5652) | end;
}

struct vpd_large_body {
	u16  length;
	u8   data[length] [holds = vpd_fields];
}

struct vpd_resource [allow_straddle] {
	bit  is_large;
	u4   item_high;
	u3   item_low;
	variant body switch (is_large) {
		case 1:  vpd_large_body large;
		default: u8 small_data[item_low];
	}
}

struct vpd {
	vpd_resource items[] while (is_large == 1 || item_high != 0x0F) max 32768;
}
"""


def _vpd_image(good: bool = True) -> bytes:
	ident = bytes([0x82, 4, 0]) + b"ABCD"
	pn    = b"PN" + bytes([3]) + b"X1Y"
	rv_kw = b"RV" + bytes([2])
	size  = len(pn) + len(rv_kw) + 2
	head  = ident + bytes([0x90, size & 0xFF, size >> 8]) + pn + rv_kw
	rv    = (-sum(head) + (0 if good else 1)) & 0xFF
	return head + bytes([rv, 0, 0x78])


def test_pci_vpd_states_its_rv_checksum(tmp_path: Path) -> None:
	"""The real shape: three resources, two keyword fields in VPD-R, and
	the image summing to zero from its first byte through RV."""
	schema = tmp_path / "vpd.situ"
	schema.write_text(VPD, encoding="ascii")
	out = tmp_path / "python"
	assert main(["build", str(schema), "--target", "python", "--out", str(out)]) == 0
	shutil.copy(RUNTIME / "python" / "situ_runtime.py", out / "situ_runtime.py")
	(out / "main.py").write_text(f'''import situ_runtime as rt
import vpd
for data in ({_vpd_image()!r}, {_vpd_image(False)!r}):
	image = vpd.vpd.at(rt.Message(bytearray(data)), 0, len(data))
	image.validate()
	fields = image.items(1).body_large.data_view
	tag = fields.items(1).value_rv
	try:
		tag.sum_check()
		word = "ok"
	except rt.ChecksumError:
		word = "checksum"
	print(image.items_count, fields.items_count, tag.sum_covered()[1], word)
''', encoding="ascii")
	ran = subprocess.run([sys.executable, str(out / "main.py")],
	                     capture_output=True, text=True, cwd=out)
	assert ran.stdout.splitlines() == ["3 2 19 ok", "3 2 19 checksum"], \
		ran.stdout + ran.stderr
	assert sum(_vpd_image()[:20]) % 256 == 0


def test_the_dissector_checks_the_message_ahead_of_the_tag(tmp_path: Path) -> None:
	"""Wireshark holds the whole packet, so it can sum it: `Good`, or what
	RV should be. The RV struct is dissected over a sub-tvb, so the sum is
	taken over the message the outermost dissector recorded, up to the
	tag's place in it."""
	from situc.cli import analyse
	from situc.dissector import generate
	from situc.parser import parse
	from test_dissector import LUA, read_back
	if LUA is None:
		pytest.skip("no Lua interpreter")
	path = tmp_path / "rv.situ"
	path.write_text(SCHEMA, encoding="ascii")
	source, resolved, _ = analyse(path)
	lua = tmp_path / "rv.lua"
	lua.write_text(generate(parse(source), resolved, "rv"), encoding="ascii")
	said = []
	for data in (GOOD, BAD):
		_, rows = read_back(lua, "image", data)
		said.append([text for field, _, _, text in rows
		             if field.endswith(".status")])
	assert said == [["Good"], [f"Bad, should be 0x{GOOD[-1]:02x}"]], said
