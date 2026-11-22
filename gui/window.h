// window.h -- the Qt Widgets frontend of decision 0034.
//
// A table of the document's fields, with the value column editable where
// `situ-edit` says the field is writable, and a status line for what a write
// cost or why it was refused. Stock widgets only, and no painting of its
// own, so that qtty can put the same window on a terminal.

#ifndef SITU_GUI_WINDOW_H
#define SITU_GUI_WINDOW_H

#include "session.h"

#include <QMainWindow>

// Who wrote it, as situc says it (situc/cli.py COPYRIGHT): in --version and
// in the About window.
#define SITU_COPYRIGHT "Copyright (C) 2026 Nabeel Sowan <nabeel@vibes.se>"

class QLabel;
class QTableWidget;
class QTableWidgetItem;

// One per document: every window over the same working bytes listens, and
// any of them that writes says so, so a field edited in a nested struct's
// window shows in its parent's at once and the other way round.
class document_hub : public QObject {
	Q_OBJECT

public:
	using QObject::QObject;

signals:
	void changed();
};

class edit_window : public QMainWindow {
	Q_OBJECT

public:
	// A window with no `hub` is a document's first, and owns the hub its
	// nested windows share.
	explicit edit_window(QWidget *parent = nullptr,
	                     document_hub *hub = nullptr);
	~edit_window() override;

	// Opens a message against a schema; false with the reason shown in the
	// window when `situ-edit` refuses it.
	bool open(const open_options &options, const QString &out);

	// What the last refusal said, for a caller that never shows the window.
	QString error() const { return session_.error(); }

	// The status line, which is where an edit's cost or refusal is said.
	QString status_text() const;

	// The table as lines, for `--no-ui`: what the window shows, not what
	// the CLI prints, so a cell the window got wrong is visible to a test.
	QString as_text() const;

	// Save to `--out` without a dialog. False with the reason in `error()`.
	bool write_out();

	// Opens the nested struct in the row named `which` in a window of its
	// own over the same bytes -- what a double click does; `name:N` opens
	// element N of a run of structs. Null where that row is not a nested
	// struct, has no such element, or the CLI refuses to open it.
	edit_window *open_field(const QString &which);

protected:
	void closeEvent(QCloseEvent *event) override;

private:
	bool open_nested(const session &parent, const QJsonObject &nested,
	                 const QString &out);
	void reload();
	void populate();
	void edited(QTableWidgetItem *item);
	void open_dialog();
	bool save();
	bool save_as();
	void say(const QString &text);

	session           session_;
	document_hub     *hub_     = nullptr;
	QString           out_;
	QLabel           *header_  = nullptr;
	QTableWidget     *table_   = nullptr;
	bool              filling_ = false;
};

#endif
