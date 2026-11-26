"""Both walkers read a field of a struct placed after a run (26.1178).

ZIP is read from its end: the end record follows everything else, so its
offset is measured, and the central directory is placed `at
end.cd_offset` and handed `end.total_entries`. The packed image could not
name either -- `FIELD_IN`'s base is a constant, and the end record's is not
-- so the packer called the argument unencodable and neither walker could
follow the directory. `FIELD_AT` carries the member whose offset is
measured at walk time, and the base is added to it.

The walker comparison over random bytes cannot see this: random bytes are
not a ZIP. So this asks both walkers over the two archives Info-ZIP wrote
for example/zip, where the directory's place and extent are what the end
record states and what CPython's zipfile read: 102 bytes at 86, and 75 at
68.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT   = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "example" / "zip" / "zip.situ"

#: (vector name, where the directory starts, how long it is).
ARCHIVES = [("stored", 86, 102), ("extra", 68, 75)]


def _vectors() -> dict[str, str]:
	found = {}
	for line in (ROOT / "example" / "zip" / "zip.vectors").read_text().splitlines():
		words = line.split()
		if len(words) > 2 and words[0] == "archive":
			found[words[1]] = "".join(words[2:])
	return found


@pytest.fixture(scope="module")
def image(tmp_path_factory: pytest.TempPathFactory) -> Path:
	out  = tmp_path_factory.mktemp("zip") / "zip.img"
	done = subprocess.run([sys.executable, "-m", "situc", "pack", str(SCHEMA),
	                       "-o", str(out)], cwd=ROOT,
	                      capture_output=True, text=True, timeout=300)
	assert done.returncode == 0 and out.is_file(), done.stderr[-2000:]
	# Nothing left out: the argument and the `when` are both in the image.
	asked = subprocess.run([sys.executable, "-m", "situc", "pack", str(SCHEMA),
	                        "--coverage"], cwd=ROOT,
	                       capture_output=True, text=True, timeout=300)
	assert asked.returncode == 0, asked.stderr[-2000:]
	assert "unencodable" not in asked.stdout + asked.stderr
	return out


@pytest.mark.parametrize("name,start,extent", ARCHIVES)
def test_the_python_walker_finds_the_directory(image: Path, name: str,
		start: int, extent: int) -> None:
	done = subprocess.run([sys.executable, str(ROOT / "bin" / "situ-walk"),
	                       str(image), _vectors()[name]], cwd=ROOT,
	                      capture_output=True, text=True, timeout=120)
	assert done.returncode == 0, done.stderr[-2000:]
	# `archive` is the last struct, and its located member the one with an
	# extent the directory's length.
	archive = done.stdout.split("-- struct[")[-1]
	assert re.search(rf"ok=1 extent={extent}\b", archive), archive
	assert "validate 0" in archive, archive


@pytest.mark.skipif(shutil.which("cc") is None and shutil.which("gcc") is None,
                    reason="no C compiler for the C walker")
@pytest.mark.parametrize("name,start,extent", ARCHIVES)
def test_the_c_walker_finds_the_directory(image: Path, name: str,
		start: int, extent: int, tmp_path: Path) -> None:
	built = subprocess.run(["make", "-s", "-C", str(ROOT), "walk-c",
	                        f"BUILD_DIR={tmp_path}"],
	                       capture_output=True, text=True, timeout=300)
	assert built.returncode == 0, built.stdout + built.stderr
	struct = str(_struct_index(image))
	done = subprocess.run([str(tmp_path / "situ-walk-c"), str(image),
	                       _vectors()[name], struct],
	                      capture_output=True, text=True, timeout=60)
	assert done.returncode == 0, done.stderr[-2000:]
	assert re.search(rf"\[2\] @{start} \+{extent}\b", done.stdout), done.stdout


def _struct_index(image: Path) -> int:
	"""Where `archive` is among the image's structs: the last one, as the
	Python walker's listing shows."""
	done = subprocess.run([sys.executable, str(ROOT / "bin" / "situ-walk"),
	                       str(image), _vectors()["stored"]], cwd=ROOT,
	                      capture_output=True, text=True, timeout=120)
	return max(int(found) for found in
	           re.findall(r"^-- struct\[(\d+)\]", done.stdout, re.M))
