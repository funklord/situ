// desktop_scheme.cpp -- see desktop_scheme.h. After snurra's src/theme.cpp.

#include "desktop_scheme.h"

#include <QApplication>
#include <QDir>
#include <QFile>
#include <QGuiApplication>
#include <QHash>
#include <QPalette>
#include <QStyleFactory>
#include <QStyleHints>
#include <QTextStream>

namespace {

// Rec. 709 luma. Any correct luminance answers a background against its
// own foreground, the two being far apart in any scheme one can read.
double luma(const QColor &colour)
{
	return 0.2126 * colour.redF() + 0.7152 * colour.greenF() +
	       0.0722 * colour.blueF();
}

// The keys of one [section] of an INI-like file, as written.
QHash<QString, QString> section_of(const QString &path, const QString &section)
{
	QHash<QString, QString> keys;
	QFile file(path);
	if (!file.open(QIODevice::ReadOnly | QIODevice::Text) ||
	    file.size() > 1024 * 1024)
		return keys;
	QTextStream in(&file);
	bool inside = false;
	const QString header = QLatin1Char('[') + section + QLatin1Char(']');
	while (!in.atEnd()) {
		const QString line = in.readLine().trimmed();
		if (line.startsWith(QLatin1Char('['))) {
			inside = line == header;
			continue;
		}
		const qsizetype equals = line.indexOf(QLatin1Char('='));
		if (inside && equals > 0)
			keys.insert(line.left(equals).trimmed(),
			            line.mid(equals + 1).trimmed());
	}
	return keys;
}

// "r,g,b", as kdeglobals writes a colour; invalid otherwise.
QColor from_triple(const QString &text)
{
	const QStringList parts = text.split(QLatin1Char(','));
	if (parts.size() != 3)
		return QColor();
	int rgb[3];
	for (int i = 0; i < 3; ++i) {
		bool number = false;
		rgb[i] = parts.at(i).trimmed().toInt(&number);
		if (!number || rgb[i] < 0 || rgb[i] > 255)
			return QColor();
	}
	return QColor(rgb[0], rgb[1], rgb[2]);
}

QString home_or(const char *variable, const QString &fallback)
{
	const QString set = qEnvironmentVariable(variable);
	return set.isEmpty() ? fallback : set;
}

desktop_scheme from_kdeglobals(const QString &path)
{
	const QHash<QString, QString> general =
	    section_of(path, QStringLiteral("General"));
	return scheme_of(
	    from_triple(general.value(QStringLiteral("windowBackground"))),
	    from_triple(general.value(QStringLiteral("windowForeground"))));
}

desktop_scheme from_lxqt(const QString &path)
{
	const QHash<QString, QString> palette =
	    section_of(path, QStringLiteral("Palette"));
	return scheme_of(QColor(palette.value(QStringLiteral("window_color"))),
	                 QColor(palette.value(
	                     QStringLiteral("window_text_color"))));
}

QPalette dark_palette()
{
	QPalette dark;
	const QColor ground(0x2b, 0x2b, 0x2b);
	const QColor text(0xe0, 0xe0, 0xe0);
	dark.setColor(QPalette::Window, ground);
	dark.setColor(QPalette::WindowText, text);
	dark.setColor(QPalette::Base, QColor(0x1f, 0x1f, 0x1f));
	dark.setColor(QPalette::AlternateBase, QColor(0x26, 0x26, 0x26));
	dark.setColor(QPalette::Text, text);
	dark.setColor(QPalette::Button, QColor(0x36, 0x36, 0x36));
	dark.setColor(QPalette::ButtonText, text);
	dark.setColor(QPalette::BrightText, QColor(0xff, 0x6b, 0x6b));
	dark.setColor(QPalette::ToolTipBase, QColor(0x36, 0x36, 0x36));
	dark.setColor(QPalette::ToolTipText, text);
	dark.setColor(QPalette::PlaceholderText, QColor(0x90, 0x90, 0x90));
	dark.setColor(QPalette::Highlight, QColor(0x3d, 0x7e, 0xb8));
	dark.setColor(QPalette::HighlightedText, QColor(0xff, 0xff, 0xff));
	dark.setColor(QPalette::Mid, QColor(0x6a, 0x6a, 0x6a));
	dark.setColor(QPalette::Dark, QColor(0x1a, 0x1a, 0x1a));
	dark.setColor(QPalette::Midlight, QColor(0x44, 0x44, 0x44));
	dark.setColor(QPalette::Light, QColor(0x50, 0x50, 0x50));
	dark.setColor(QPalette::Shadow, QColor(0x10, 0x10, 0x10));
	dark.setColor(QPalette::Link, QColor(0x7a, 0xb8, 0xf5));
	dark.setColor(QPalette::LinkVisited, QColor(0xb0, 0x9a, 0xf5));
	const QColor faded(0x80, 0x80, 0x80);
	dark.setColor(QPalette::Disabled, QPalette::WindowText, faded);
	dark.setColor(QPalette::Disabled, QPalette::Text, faded);
	dark.setColor(QPalette::Disabled, QPalette::ButtonText, faded);
	return dark;
}

}

desktop_scheme scheme_of(const QColor &background, const QColor &foreground)
{
	if (!background.isValid() || !foreground.isValid())
		return desktop_scheme::unknown;
	return luma(background) < luma(foreground) ? desktop_scheme::dark
	                                           : desktop_scheme::light;
}

desktop_scheme current_scheme()
{
	const QByteArray asked = qgetenv("SITU_THEME");
	if (asked == "dark")
		return desktop_scheme::dark;
	if (asked == "light")
		return desktop_scheme::light;

	switch (QGuiApplication::styleHints()->colorScheme()) {
	case Qt::ColorScheme::Dark:
		return desktop_scheme::dark;
	case Qt::ColorScheme::Light:
		return desktop_scheme::light;
	default:
		break;
	}

	// The running session's file first: an old one of another desktop
	// says nothing about this one.
	const QString home = QDir::homePath();
	const QString trinity = home_or("TDEHOME", home + QStringLiteral("/.trinity"))
	                        + QStringLiteral("/share/config/kdeglobals");
	const QString kde3 = home_or("KDEHOME", home + QStringLiteral("/.kde"))
	                     + QStringLiteral("/share/config/kdeglobals");
	const QString lxqt = home_or("XDG_CONFIG_HOME",
	                             home + QStringLiteral("/.config"))
	                     + QStringLiteral("/lxqt/lxqt.conf");
	const QString desktop =
	    qEnvironmentVariable("XDG_CURRENT_DESKTOP").toUpper();
	struct source {
		QString path;
		desktop_scheme (*read)(const QString &);
		bool session;
	};
	const source files[] = {
		{trinity, from_kdeglobals, desktop.contains(QLatin1String("TDE"))},
		{kde3, from_kdeglobals, desktop.contains(QLatin1String("KDE"))},
		{lxqt, from_lxqt, desktop.contains(QLatin1String("LXQT"))},
	};
	for (const source &file : files)
		if (file.session)
			if (const desktop_scheme said = file.read(file.path);
			    said != desktop_scheme::unknown)
				return said;
	if (desktop.isEmpty())
		for (const source &file : files)
			if (const desktop_scheme said = file.read(file.path);
			    said != desktop_scheme::unknown)
				return said;

	const QPalette given = QGuiApplication::palette();
	return scheme_of(given.color(QPalette::Window),
	                 given.color(QPalette::WindowText));
}

QString scheme_name(desktop_scheme scheme)
{
	switch (scheme) {
	case desktop_scheme::dark:
		return QStringLiteral("dark");
	case desktop_scheme::light:
		return QStringLiteral("light");
	default:
		return QStringLiteral("unknown");
	}
}

void follow_desktop(QApplication &application)
{
	if (current_scheme() != desktop_scheme::dark)
		return;
	// Fusion draws every control from the palette; a native style may not,
	// and would leave light buttons on the dark ground.
	if (QStyle *fusion = QStyleFactory::create(QStringLiteral("Fusion")))
		application.setStyle(fusion);
	application.setPalette(dark_palette());
}
