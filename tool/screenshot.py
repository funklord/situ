#!/usr/bin/env python3
"""`make screenshots`: the README's pictures, made from the current build.

Two, both overwritten when what they show changes and never per build:

- `doc/screenshot/situ-edit-gui.png`: the editor's window over a TCP SYN
  header, grabbed headless (`-platform offscreen`, `--grab`), so the
  picture is the build's and not a hand-made one. The header is made here,
  not captured: a SYN to port 443 with the options a stack sends, nothing
  from anybody's network.
- `doc/screenshot/situc-advise.svg`: `situc advise example/tcp/tcp.situ`,
  rendered as SVG text -- a few kilobytes, sharp at any size, readable in a
  diff. situc colours nothing, so no terminal is needed to run it.

Stdlib only. Each step is bounded by a timeout, and the tool writes those
two files and a scratch directory it removes.
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
OUT  = ROOT / "doc" / "screenshot"

COLUMNS = 100		# where a terminal this wide wraps the advice
LINES   = 26		# the warning and the first suggestions
CELL_W  = 8.4		# a 14 px monospace cell
CELL_H  = 18
PAD     = 14


def syn_header() -> bytes:
	"""A TCP SYN with the options a stack sends: MSS 1460, SACK permitted,
	timestamps, a NOP and window scale 7."""
	options = (bytes([2, 4, 0x05, 0xB4, 4, 2, 8, 10])
	           + struct.pack(">II", 2216843, 0) + bytes([1, 3, 3, 7]))
	words = 5 + len(options) // 4
	return struct.pack(">HHIIBBHHH", 49152, 443, 0x1A2B3C4D, 0,
	                   words << 4, 0x02, 64240, 0, 0) + options


def grab_window(binary: Path) -> None:
	with tempfile.TemporaryDirectory() as scratch:
		message = Path(scratch) / "syn.bin"
		message.write_bytes(syn_header())
		target = OUT / "situ-edit-gui.png"
		subprocess.run(
			[str(binary), "--grab", str(target), "--struct", "tcp_header",
			 str(ROOT / "example" / "tcp" / "tcp.situ"), str(message)],
			check=True, timeout=120,
			env={**os.environ, "QT_QPA_PLATFORM": "offscreen",
			     # The picture is the default palette's, whatever the
			     # desktop running this happens to be.
			     "SITU_THEME": "dark"})
		print(f"screenshot: wrote {target.relative_to(ROOT)}")


def wrapped(text: str) -> list[str]:
	lines: list[str] = []
	for line in text.splitlines():
		while len(line) > COLUMNS:
			lines.append(line[:COLUMNS])
			line = line[COLUMNS:]
		lines.append(line)
	return lines


def as_svg(command: str, lines: list[str]) -> str:
	shown  = [f"$ {command}", *lines]
	width  = int(PAD * 2 + COLUMNS * CELL_W)
	height = int(PAD * 2 + len(shown) * CELL_H)
	rows = []
	for at, line in enumerate(shown):
		y = PAD + (at + 1) * CELL_H - 4
		fill = "#8ab4f8" if at == 0 else "#d6d6d6"
		rows.append(f'<text x="{PAD}" y="{y}" fill="{fill}"'
		            f' xml:space="preserve">{escape(line)}</text>')
	return "\n".join([
		f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}"'
		f' height="{height}" viewBox="0 0 {width} {height}"'
		' font-family="DejaVu Sans Mono, Menlo, Consolas, monospace"'
		' font-size="14">',
		f'<rect width="{width}" height="{height}" rx="6" fill="#1e1e1e"/>',
		*rows,
		"</svg>",
		"",
	])


def advise() -> None:
	schema  = "example/tcp/tcp.situ"
	command = f"situc advise {schema}"
	done = subprocess.run([sys.executable, "-m", "situc", "advise", schema],
	                      cwd=ROOT, capture_output=True, text=True,
	                      timeout=120, check=True)
	# The warning arrives on stderr and the suggestions on stdout; a
	# terminal shows the first above the second.
	lines  = wrapped(done.stderr + done.stdout)[:LINES]
	target = OUT / "situc-advise.svg"
	target.write_text(as_svg(command, lines), encoding="utf-8")
	print(f"screenshot: wrote {target.relative_to(ROOT)}")


def main() -> int:
	binary = Path(sys.argv[1]) if len(sys.argv) > 1 else None
	OUT.mkdir(parents=True, exist_ok=True)
	advise()
	if binary is None or not binary.exists():
		print("screenshot: no situ-edit-gui given or built; "
		      "`make screenshots` builds it", file=sys.stderr)
		return 1
	grab_window(binary)
	return 0


if __name__ == "__main__":
	sys.exit(main())
