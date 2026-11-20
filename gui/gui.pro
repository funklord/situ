# gui/gui.pro -- qmake project for situ-edit-gui, the Qt Widgets frontend of
# decision 0034: a window, or with qtty the same window on a terminal
# (`--tui`).
#
# Built by the root Makefile's `make gui`, which hands qmake SITU_BUILD, the
# directory the binary goes in, and, where vendor/qtty is checked out,
# QTTY_LIB_DIR, where it built libqtty.a. Not by a bare `make`: this is the
# one part of situ with a third-party dependency, and `situc`, `situ-edit`
# and the curses `situ-edit-tui` need Python and nothing else (0034).
#
# qmake rather than CMake, as hull and netcfgd build theirs: qtty is a
# qmake library with a .pri to include, and one build system per window
# across the workspace is the one a reader already knows.
#
# No QtQuick and no QML: qtty draws Widgets, which a scene graph would not
# survive.

TEMPLATE = app
TARGET = situ-edit-gui
QT += widgets
CONFIG += c++17
CONFIG -= app_bundle

isEmpty(SITU_BUILD): error("gui.pro: SITU_BUILD is not set; build with `make gui` from the root")
DESTDIR = $$SITU_BUILD

# -Os, said, or qmake's -O2 stays; and the size rule's unreachable sections
# dropped at link.
QMAKE_CXXFLAGS_RELEASE -= -O2
QMAKE_CXXFLAGS_RELEASE += -Os
QMAKE_CXXFLAGS += -ffunction-sections -fdata-sections -Wall -Wextra
QMAKE_LFLAGS += -Wl,--gc-sections

ROOT = $$PWD/..
# Where `situ-edit` and `situc` are when this runs from the build tree. An
# installed copy finds them beside itself first, so this is the fallback.
DEFINES += SITU_TREE_BIN=\\\"$$ROOT/bin\\\"
DEFINES += SITU_VERSION=\\\"$$cat($$ROOT/VERSION, singleline)\\\"

# qtty: the same window on a terminal. Optional, as the Makefile decides:
# without it situ-edit-gui is the window alone and says so when asked for
# --tui, and the terminal frontend that needs nothing is situ-edit-tui.
!isEmpty(QTTY_LIB_DIR):exists($$QTTY_LIB_DIR/libqtty.a) {
	include($$ROOT/vendor/qtty/qtty.pri)
	DEFINES += SITU_QTTY
	LIBS += -L$$QTTY_LIB_DIR -lqtty
	PRE_TARGETDEPS += $$QTTY_LIB_DIR/libqtty.a
	QT += dbus
}

SOURCES += \
	cell_editor.cpp \
	desktop_scheme.cpp \
	main.cpp \
	session.cpp \
	window.cpp

HEADERS += \
	cell_editor.h \
	desktop_scheme.h \
	session.h \
	window.h
