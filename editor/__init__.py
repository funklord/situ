"""The situ editor: a document model over a packed image, and its frontends.

Decision 0034. What this package holds is the *core* -- opening an image and
a buffer, placing the fields, reading their values -- and nothing about how
any of it is displayed. Three frontends drive it and none of them contains an
editing rule, because three frontends with their own idea of what a write
costs is the failure `traverse.py` and `relation.py` each exist to prevent.

**Nothing here imports `situc`.** 0026 keeps the compiler and the interpreter
apart, and 0034 keeps that boundary while still letting a user open a
`.situ`: the editor reads images, and opening a schema runs `situc pack`
first. A process boundary rather than a link-time one, which is what 0026
already made a binary boundary.
"""
