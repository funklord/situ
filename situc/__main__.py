"""Entry point for `python3 -m situc`.

`python3 -m situc.cli` has always worked; this makes the shorter form work
too, so the module path a user guesses first is the one that runs.
"""

import sys

from situc.cli import main

if __name__ == "__main__":
	sys.exit(main())
