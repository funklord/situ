# Debian packaging

`make deb` builds two `.deb` files into `build/deb/`:

| package | arch | what is in it |
|---|---|---|
| `situc` | `all` | the compiler: `/usr/bin/situc`, the Python package under `/usr/lib/situc`, and the standard schemas under `/usr/share/situc/std` |
| `libsitu-dev` | the build's | the C runtime: `/usr/include/situ.h` and `/usr/lib/libsitu.a` |

**Two packages rather than one, because they do not have the same
architecture.** `situc` is Python and runs anywhere; `libsitu.a` is compiled
objects and does not. One package would have to claim the narrower of the two,
which would make the schema compiler uninstallable on any machine it was not
built on -- for a tool whose whole point is generating code for targets other
than the build host, that is the wrong way round.

They are independent: a build machine needs `situc` and no runtime, and a
target needs the runtime headers and no compiler. Neither depends on the
other, and `situc` needs nothing but `python3`.

## How it is built

`dpkg-deb --build` over a tree staged by `make install DESTDIR=...`, rather
than `debian/` and `dpkg-buildpackage`. Two reasons:

- **The install rule stays the one thing being tested.** The packages contain
  exactly what `make install` produces, so a packaging bug and an install bug
  cannot be different bugs.
- **It needs only `dpkg-deb`.** Section 24 asks the toolchain to vendor into
  an embedded build environment where `pip install` is not on the table; a
  packaging step that needs `debhelper` and a network is the same problem
  wearing a different hat.

The version comes from `situc/__init__.py` -- the same string `situc
--version` prints -- so there is no second place to forget.
