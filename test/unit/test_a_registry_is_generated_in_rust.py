"""A registry generates its items in Rust (0070, step 6).

The C registry (26.1081) in Rust's idiom: an entry struct, a unit struct
whose `ENTRIES` is the table, and `find`, `check`, `value` and `safer` as
associated functions -- `check` a `Result<()>` carrying the cell type's own
error, `value` a `Result<i64>`. Held to the same 33 cases, built under
`-D warnings`.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

import pytest							# noqa: E402

from situc.cli import main as situc				# noqa: E402
from test_a_registry_is_generated_in_c import CASES, SCHEMA	# noqa: E402

RUSTC = shutil.which("rustc")

MAIN = r"""mod situ_rt;
mod settings;

use settings::Node;

fn main() {
	let args: Vec<String> = std::env::args().collect();
	let mut i = 1;
	while i + 2 < args.len() {
		let (op, key, arg) = (&args[i], &args[i + 1], &args[i + 2]);
		let entry = Node::find(key.as_bytes());
		match op.as_str() {
			"find" => println!("{}", entry.map_or("-", |e| e.key)),
			"check" => println!("{}", match Node::check(entry.unwrap(), arg.as_bytes()) {
				Ok(()) => 0,
				Err(settings_err) => if settings_err == situ_rt::Error::Bounds { 1 } else { 2 },
			}),
			"value" => println!("{}", Node::value(entry.unwrap(), arg.as_bytes()).unwrap_or(-1)),
			_ => {
				let (a, b) = arg.split_once(',').unwrap();
				println!("{}", Node::safer(entry.unwrap(), a.as_bytes(), b.as_bytes()) as u8);
			}
		}
		i += 3;
	}
	let gone = Node::find(b"archive/gone").unwrap();
	println!("row {} {} {} {}", gone.default.unwrap_or("-"), gone.unit.unwrap_or("-"),
	         gone.scope, gone.display);
	let _ = (gone.family, gone.min, gone.max, gone.min_size, gone.max_size,
	         gone.summary, gone.window_ms, gone.cell, gone.safe);
}
"""


@pytest.mark.skipif(RUSTC is None, reason="needs rustc")
def test_the_rust_registry_answers_what_the_c_one_does(tmp_path: Path) -> None:
	schema = tmp_path / "settings.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	src = tmp_path / "src"
	src.mkdir()
	assert situc(["build", str(schema), "--target", "rust",
	              "--out", str(src)]) == 0
	(src / "situ_rt.rs").write_text(
		(ROOT / "runtime" / "rust" / "situ_rt.rs").read_text(encoding="ascii")
		.replace("#![no_std]\n", ""), encoding="ascii")
	(src / "main.rs").write_text(MAIN, encoding="ascii")
	assert RUSTC is not None
	built = subprocess.run(
		[RUSTC, "--edition", "2021", "-D", "warnings", "-A", "dead_code",
		 str(src / "main.rs"), "-o", str(tmp_path / "probe")],
		capture_output=True, text=True, cwd=tmp_path)
	assert built.returncode == 0, built.stderr

	argv = [str(tmp_path / "probe")]
	for op, key, arg, _ in CASES:
		argv += [op, key, arg]
	said = subprocess.run(argv, capture_output=True, text=True,
	                      check=True).stdout.splitlines()
	assert said[:-1] == [want for *_, want in CASES], [
		(case, got) for case, got in zip(CASES, said) if case[-1] != got]
	assert said[-1] == "row 14d - 1 4"
