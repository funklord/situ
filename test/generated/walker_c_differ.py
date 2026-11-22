"""Build and run the C walker against the generated C reader, coverage-
guided over both (26.1152).

    python3 walker_c_differ.py SCHEMA WORKDIR CC [libFuzzer flags...]

`make fuzz-walker-c-differ` runs it once per corpus schema. It writes, in
WORKDIR, the schema's C accessors and the four-way differential's C driver
with its `main` renamed, the packed image and each struct's index in it,
then builds `walker_c_differ.c` with CC under libFuzzer, ASan and UBSan
and replaces itself with the result, so libFuzzer's exit status is this
command's. The Python twin is `walker_differ_fuzz.py`, which compares the
Python walker's listing with the Python reader's; this compares verdicts,
in C, over every input the fuzzer runs rather than the corpus it keeps --
replaying the kept corpora is what found 26.1148 and 26.1149.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

from situc import pack as packer				# noqa: E402
from situc.cli import analyse					# noqa: E402
from situc.codegen import differ				# noqa: E402
from situc.codegen.c import generate as generate_c		# noqa: E402
from situc.codegen.c.derived import generate as generate_derived_c	# noqa: E402
from situc.parser import parse					# noqa: E402
from walker.image import load					# noqa: E402

SCHEMA, WORK, CC = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
WORK.mkdir(parents=True, exist_ok=True)

source, resolved, _outcomes = analyse(SCHEMA)
parsed = parse(source)
for name, text in generate_c(parsed, resolved, "unit").files().items():
	(WORK / name).write_text(text, encoding="ascii")
(WORK / "unit_derived.c").write_text(generate_derived_c(parsed, "unit"),
                                     encoding="ascii")
(WORK / "differ_driver.c").write_text(differ.generate(parsed, resolved, "c"),
                                      encoding="ascii")

blob, _coverage = packer.pack(parsed, resolved, metadata=True)
(WORK / "walk_image.h").write_text(
	"static const uint8_t IMAGE_BLOB[] = {\n"
	+ ",\n".join(", ".join(f"0x{byte:02x}" for byte in blob[at:at + 16])
	             for at in range(0, len(blob), 16))
	+ "\n};\n", encoding="ascii")

# Each struct by the name its driver section prints, and its index in the
# image. A struct the driver prints under `argued` is not listed: it is
# handed invented arguments there, and the walker would need the same.
image = load(blob)
argued = {name for name, struct in resolved.structs.items()
          if differ.argued(struct)}
rows = [f'\t{{"{image.struct_name(index)}", {index}u}},'
        for index in range(len(image.structs))
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", image.struct_name(index))
        and image.struct_name(index) not in argued]
(WORK / "walk_shapes.h").write_text(
	"static const struct { const char *name; uint32_t shape; } SHAPES[] = {\n"
	+ "\n".join(rows or ['\t{"", 0xffffffffu},']) + "\n};\n",
	encoding="ascii")

binary = WORK / "walker_c_differ"
subprocess.run(
	[CC, "-g", "-O1", "-fsanitize=fuzzer,address,undefined",
	 "-fno-sanitize-recover=undefined", "-fno-omit-frame-pointer",
	 "-std=gnu11", f"-I{WORK}", f"-I{ROOT / 'runtime' / 'c'}",
	 f"-I{ROOT / 'walker' / 'c'}",
	 str(HERE / "walker_c_differ.c"), str(WORK / "unit.c"),
	 str(WORK / "unit_derived.c"), str(ROOT / "runtime" / "c" / "situ.c"),
	 str(ROOT / "walker" / "c" / "situ_walk.c"), "-o", str(binary)],
	check=True)
os.execv(str(binary), [str(binary), *sys.argv[4:]])
