"""The situ compiler: a schema language for byte-exact layouts with statically
inferred capability properties.

Package layout follows project.md section 23. Each module has a single
responsibility, stated in its own docstring.
"""

# The version is stated once, in the VERSION file, and read rather than
# restated here. Two locations, because the file sits beside this module once
# installed (/usr/lib/situc/VERSION) and one level up in the source tree.
# Reading only the source-tree path made the installed package fail on
# import, which the build cannot notice: it packages the tree, it does not
# import what it packaged.
from pathlib import Path


def _read_version() -> str:
	here = Path(__file__).resolve().parent
	for candidate in (here / "VERSION", here.parent / "VERSION"):
		if candidate.is_file():
			return candidate.read_text().strip()
	raise RuntimeError("VERSION not found beside situc or at the tree root")


__version__ = _read_version()
