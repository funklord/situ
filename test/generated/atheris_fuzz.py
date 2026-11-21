"""Run one generated Python fuzz harness under atheris (`make fuzz-python`).

    python3 atheris_fuzz.py DIR NAME [libFuzzer arguments...]

DIR holds `NAME.py`, `NAME_fuzz.py` and `situ_runtime.py`. The harness's
`one_input` takes no dependency (26.847); this file is the two lines on
top that make it coverage-guided, with the accessors and the runtime
instrumented, since they are what is under test.
"""

from __future__ import annotations

import importlib
import sys


def main() -> None:
	if len(sys.argv) < 3:
		sys.exit("usage: atheris_fuzz.py DIR NAME [libFuzzer arguments...]")
	directory, name = sys.argv[1], sys.argv[2]
	# Through `importlib`, so the module is typed the same whether atheris
	# is installed (its stubs lack `instrument_imports`) or not.
	try:
		atheris = importlib.import_module("atheris")
	except ImportError:
		sys.exit("atheris is not installed: `pip install --user atheris` "
		         "(it is not packaged for Debian)")
	sys.path.insert(0, directory)
	with atheris.instrument_imports():
		importlib.import_module("situ_runtime")
		importlib.import_module(name)
		harness = importlib.import_module(f"{name}_fuzz")
	atheris.Setup([sys.argv[0], *sys.argv[3:]], harness.one_input)
	atheris.Fuzz()


if __name__ == "__main__":
	main()
