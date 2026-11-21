"""Write the crate `make fuzz-rust` builds: cargo_fuzz_crate.py DIR NAME...

cargo-fuzz looks for a crate above `fuzz/`, so an empty one stands at DIR,
and `DIR/fuzz` holds one binary per schema, each the harness `gen-fuzz
--target rust` wrote into `fuzz_targets/NAME/`. A file is rewritten only
when what it says changes, so a second `make` writes nothing and cargo
does not rebuild for a manifest that did not move.
"""

from __future__ import annotations

import sys
from pathlib import Path

HOST = """[package]
name = "situ-generated"
version = "0.0.0"
edition = "2021"
publish = false

[workspace]
exclude = ["fuzz"]
"""

FUZZ = """[package]
name = "situ-fuzz"
version = "0.0.0"
edition = "2021"
publish = false

[package.metadata]
cargo-fuzz = true

[dependencies]
libfuzzer-sys = "0.4"

[lints.rust]
unexpected_cfgs = { level = "warn", check-cfg = ["cfg(fuzzing)"] }

[workspace]
"""

TARGET = """
[[bin]]
name = "{name}"
path = "fuzz_targets/{name}/{name}_fuzz.rs"
test = false
doc = false
bench = false
"""


def write(path: Path, text: str) -> None:
	"""`text` into `path`, unless it is there already."""
	path.parent.mkdir(parents=True, exist_ok=True)
	if path.exists() and path.read_text(encoding="ascii") == text:
		return
	path.write_text(text, encoding="ascii")


def main() -> None:
	if len(sys.argv) < 3:
		sys.exit("usage: cargo_fuzz_crate.py DIR NAME...")
	root = Path(sys.argv[1])
	write(root / "Cargo.toml", HOST)
	write(root / "src" / "lib.rs", "")
	write(root / "fuzz" / "Cargo.toml",
	      FUZZ + "".join(TARGET.format(name=name) for name in sys.argv[2:]))


if __name__ == "__main__":
	main()
