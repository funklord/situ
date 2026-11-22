// session.h -- one message opened against one schema, through `situ-edit`.
//
// The GUI of decision 0034 holds no editing rule. Every question it asks --
// what a field is, whether it may be written, what a write cost -- is put to
// `situ-edit --format json`, the reference frontend, across a process
// boundary. This class is that boundary and nothing else: it runs the CLI,
// reads what it printed, and keeps the working bytes in a file the CLI
// writes.
//
// The schema is packed once on open (`situc pack --metadata`), exactly as
// `editor.text.image_bytes` does it, so an edit costs one walk rather than a
// compile.

#ifndef SITU_GUI_SESSION_H
#define SITU_GUI_SESSION_H

#include <QJsonObject>
#include <QString>
#include <QStringList>
#include <QTemporaryDir>

#include <memory>

struct open_options {
	QString     schema;
	QString     message;
	QString     structure;	// --struct, empty for the schema's first
	qint64      offset = 0;	// --offset
	qint64      length = -1;	// --length, -1 for the rest of the file
	bool        hex = false;	// --hex: the message file holds hex
	QStringList arguments;	// --arg, in order
	// --within STRUCT:OFFSET:LENGTH, the windows this one was opened inside,
	// outermost first: what lets a byte run change length in a nested
	// struct, moving what frames it (26.1141).
	QStringList within;
};

class session {
public:
	// Where `situ-edit` and `situc` are found: an explicit directory first
	// (the SITU_BIN environment variable), then beside this program, then
	// the source tree this was built from, then PATH.
	static QString find_tool(const QString &name);

	// Packs the schema and copies the message into the working file. False
	// with `error()` set when either step is refused.
	bool open(const open_options &options);

	// Re-reads the working bytes. The document is `document()`.
	bool refresh();

	// Stores `text` into `field` as the CLI's `--put` reads it -- hex for a
	// byte run, a number otherwise. On success the working bytes carry the
	// write and `notes()` says what it cost; on refusal nothing changes.
	bool set(const QString &field, const QString &text);

	// Copies the working bytes to `path`.
	bool save(const QString &path);

	// Opens a struct nested in `parent`'s document -- the CLI's `--struct`
	// and `--offset` -- over the SAME working bytes, so an edit in either
	// is in both, and saving either writes the one buffer. `length` is -1
	// for the rest of the buffer.
	bool open_nested(const session &parent, const QString &structure,
	                 qint64 offset, qint64 length);

	const QJsonObject &document() const { return document_; }
	const QString &error() const { return error_; }
	const QStringList &notes() const { return notes_; }
	bool modified() const { return *modified_; }
	const open_options &options() const { return options_; }

private:
	bool run(const QStringList &extra, QByteArray *out);
	QStringList common_arguments() const;
	void follow_frames(const QJsonObject &after);

	// Shared with every session opened nested in this one: the scratch
	// directory the working bytes live in, and whether they have changed
	// since they were last written out.
	open_options                   options_;
	std::shared_ptr<QTemporaryDir> scratch_;
	QString                        image_;
	QString                        work_;
	QJsonObject                    document_;
	QString                        error_;
	QStringList                    notes_;
	std::shared_ptr<bool>          modified_ = std::make_shared<bool>(false);
	// How many writes have changed the working file's length, shared by
	// every window over it, and how many this window has followed. A
	// window over part of the file whose count is behind was framed
	// before bytes moved under it: its offset may name another member's
	// bytes, so it refuses to write until reopened (26.1145).
	std::shared_ptr<qint64>        resizes_ = std::make_shared<qint64>(0);
	qint64                         seen_resizes_ = 0;
};

#endif
