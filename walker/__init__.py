"""The walker: a packed layout image, read over bytes.

A separate binary from `situc`, in this repository since decision 0026's
amendment of 2026-08-07 so that it can join the differential check as a fifth
column. Nothing under `situc/` imports this package, and nothing here imports
`situc` -- a fifth column that shared the compiler's traversal would be
comparing a backend against itself.
"""

__all__ = ["image", "vm", "walk"]
