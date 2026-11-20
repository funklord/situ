// desktop_scheme.h -- whether the desktop is dark, so the window can be.
//
// The desktop's darkness and never its hue: the window paints a dark scheme
// of its own. snurra's `snurra_follow_desktop` (snurra's
// include/snurra/theme.h) is the shape, taken rather than invented a fifth
// time across the workspace: the first source that answers, in this order --
// $SITU_THEME, Qt's own colour scheme, the file a desktop with no dark status
// writes (TDE's and KDE 3's kdeglobals, LXQt's lxqt.conf; the running
// session's first), and only then the palette Qt was given. The palette
// always answers -- light, on a desktop Qt is not integrated with -- so it
// is asked last, or a file that says dark would never be read.
//
// Unknown rather than a guess where nothing answers: a wrong dark is
// unreadable text on a pale ground, a wrong light only plain. The
// freedesktop portal is not asked, as snurra does not ask it; QtDBus is
// not linked for that alone.

#ifndef SITU_GUI_DESKTOP_SCHEME_H
#define SITU_GUI_DESKTOP_SCHEME_H

#include <QColor>
#include <QString>

class QApplication;

enum class desktop_scheme { unknown, light, dark };

// A background against its own foreground: darker is dark.
desktop_scheme scheme_of(const QColor &background, const QColor &foreground);

// What the desktop says, by the order above.
desktop_scheme current_scheme();

// "dark", "light" or "unknown", for --scheme.
QString scheme_name(desktop_scheme scheme);

// The dark scheme put on `application` where the desktop is dark, and
// nothing otherwise.
void follow_desktop(QApplication &application);

#endif
