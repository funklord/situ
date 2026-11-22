// session.cpp -- see session.h. Runs the CLI; decides nothing.

#include "session.h"

#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QProcess>
#include <QStandardPaths>

#ifndef SITU_TREE_BIN
#define SITU_TREE_BIN ""
#endif

namespace {

// A walk that takes longer than this is a hung child, not a slow one: the
// corpus's largest message opens in well under a second.
constexpr int run_limit_ms = 60000;

}

QString session::find_tool(const QString &name)
{
	QStringList places;
	const QString from_environment = qEnvironmentVariable("SITU_BIN");
	if (!from_environment.isEmpty())
		places << from_environment;
	places << QCoreApplication::applicationDirPath();
	if (QString(SITU_TREE_BIN).size() > 0)
		places << QString(SITU_TREE_BIN);
	for (const QString &place : places) {
		const QFileInfo candidate(QDir(place).filePath(name));
		if (candidate.isFile() && candidate.isExecutable())
			return candidate.absoluteFilePath();
	}
	return QStandardPaths::findExecutable(name);
}

bool session::open(const open_options &options)
{
	options_  = options;
	modified_ = std::make_shared<bool>(false);
	notes_.clear();
	scratch_ = std::make_shared<QTemporaryDir>();
	if (!scratch_->isValid()) {
		error_ = QStringLiteral("cannot make a scratch directory: %1")
		         .arg(scratch_->errorString());
		return false;
	}
	work_ = scratch_->filePath(QStringLiteral("message.bin"));

	// THE IMAGE, packed once. A `.situ` goes through `situc pack
	// --metadata`, as `editor.text.image_bytes` does it; anything else is
	// taken to be an image already, as there.
	if (options.schema.endsWith(QStringLiteral(".situ"))) {
		const QString situc = find_tool(QStringLiteral("situc"));
		if (situc.isEmpty()) {
			error_ = QStringLiteral("cannot find `situc` to pack %1")
			         .arg(options.schema);
			return false;
		}
		image_ = scratch_->filePath(QStringLiteral("schema.img"));
		QProcess pack;
		pack.start(situc, {QStringLiteral("pack"),
		                   QStringLiteral("--metadata"),
		                   QStringLiteral("-o"), image_, options.schema});
		if (!pack.waitForFinished(run_limit_ms) ||
		    pack.exitStatus() != QProcess::NormalExit ||
		    pack.exitCode() != 0) {
			error_ = QStringLiteral("`situc pack` refused %1:\n%2")
			         .arg(options.schema,
			              QString::fromUtf8(pack.readAllStandardError()).trimmed());
			return false;
		}
	} else {
		image_ = options.schema;
	}

	// THE WORKING BYTES, as raw bytes whatever the input was. A hex message
	// is turned into bytes by the CLI itself -- `--hex` with `--out` and
	// no edit writes the buffer it read -- so the GUI has no second hex
	// parser to disagree with the first.
	if (options.hex) {
		QByteArray ignored;
		QStringList once = common_arguments();
		once << QStringLiteral("--hex") << QStringLiteral("--out") << work_
		     << image_ << options.message;
		if (!run(once, &ignored))
			return false;
	} else if (!QFile::copy(options.message, work_)) {
		error_ = QStringLiteral("cannot read %1").arg(options.message);
		return false;
	}
	QFile::setPermissions(work_, QFile::ReadOwner | QFile::WriteOwner);
	return refresh();
}

QStringList session::common_arguments() const
{
	QStringList arguments;
	if (!options_.structure.isEmpty())
		arguments << QStringLiteral("--struct") << options_.structure;
	if (options_.offset != 0)
		arguments << QStringLiteral("--offset")
		          << QString::number(options_.offset);
	if (options_.length >= 0)
		arguments << QStringLiteral("--length")
		          << QString::number(options_.length);
	for (const QString &argument : options_.arguments)
		arguments << QStringLiteral("--arg") << argument;
	for (const QString &window : options_.within)
		arguments << QStringLiteral("--within") << window;
	return arguments;
}

// After a write, where every window's frame is now: a run grown in this
// one moved its own length and each enclosing window's by as much, and
// the next call must name the frames as they are (26.1141). A window
// framed by the rest of the file keeps saying so.
void session::follow_frames(const QJsonObject &after)
{
	if (options_.length >= 0 && after.contains(QStringLiteral("bytes")))
		options_.length = after.value(QStringLiteral("bytes")).toInteger();
	const QJsonArray outer = after.value(QStringLiteral("within")).toArray();
	for (qsizetype i = 0; i < options_.within.size() && i < outer.size(); i++) {
		QStringList parts = options_.within[i].split(QLatin1Char(':'));
		if (parts.size() != 3 || parts[2].isEmpty())
			continue;
		parts[2] = QString::number(outer[i].toObject()
		                           .value(QStringLiteral("bytes")).toInteger());
		options_.within[i] = parts.join(QLatin1Char(':'));
	}
}

bool session::run(const QStringList &extra, QByteArray *out)
{
	const QString cli = find_tool(QStringLiteral("situ-edit"));
	if (cli.isEmpty()) {
		error_ = QStringLiteral("cannot find `situ-edit`; set SITU_BIN to "
		                        "the directory holding it");
		return false;
	}
	QProcess child;
	child.start(cli, extra);
	if (!child.waitForFinished(run_limit_ms)) {
		child.kill();
		child.waitForFinished();
		error_ = QStringLiteral("`situ-edit` did not answer");
		return false;
	}
	const QByteArray said = child.readAllStandardError();
	*out = child.readAllStandardOutput();
	notes_.clear();
	for (const QString &line : QString::fromUtf8(said).split('\n')) {
		QString note = line.trimmed();
		if (note.startsWith(QStringLiteral("situ-edit: ")))
			note = note.mid(11);
		if (!note.isEmpty())
			notes_ << note;
	}
	if (child.exitStatus() != QProcess::NormalExit || child.exitCode() != 0) {
		error_ = notes_.isEmpty() ? QStringLiteral("`situ-edit` failed")
		                          : notes_.join('\n');
		return false;
	}
	return true;
}

bool session::refresh()
{
	QByteArray printed;
	QStringList arguments = common_arguments();
	arguments << QStringLiteral("--format") << QStringLiteral("json")
	          << image_ << work_;
	const QStringList kept = notes_;
	if (!run(arguments, &printed))
		return false;
	notes_ = kept;
	QJsonParseError why;
	const QJsonDocument parsed = QJsonDocument::fromJson(printed, &why);
	if (!parsed.isObject()) {
		error_ = QStringLiteral("`situ-edit --format json` printed something "
		                        "that is not a document: %1")
		         .arg(why.errorString());
		return false;
	}
	document_ = parsed.object();
	return true;
}

bool session::set(const QString &field, const QString &text)
{
	// Into a second file, renamed over the first only once the CLI has
	// accepted the write: a refused edit leaves the working bytes exactly
	// as they were, which is what the CLI's own all-or-nothing promises.
	const QString next = scratch_->filePath(QStringLiteral("next.bin"));
	QFile::remove(next);
	QStringList arguments = common_arguments();
	// `--put`: the CLI reads the text the way the field shows it, hex or a
	// number, so the window does not decide which (26.1113).
	arguments << QStringLiteral("--put")
	          << field + QLatin1Char('=') + text.trimmed()
	          << QStringLiteral("--format") << QStringLiteral("json")
	          << QStringLiteral("--out") << next << image_ << work_;
	QByteArray printed;
	if (!run(arguments, &printed))
		return false;
	// The CLI prints "not written" only without --out; anything else it
	// said is what the write cost.
	const QStringList cost = notes_;
	QFile::remove(work_);
	if (!QFile::rename(next, work_)) {
		error_ = QStringLiteral("cannot replace the working copy");
		return false;
	}
	*modified_ = true;
	const QJsonDocument after = QJsonDocument::fromJson(printed);
	if (after.isObject())
		follow_frames(after.object());
	if (!refresh())
		return false;
	notes_ = cost;
	return true;
}

bool session::save(const QString &path)
{
	QFile::remove(path + QStringLiteral(".situ-edit-new"));
	if (!QFile::copy(work_, path + QStringLiteral(".situ-edit-new"))) {
		error_ = QStringLiteral("cannot write %1").arg(path);
		return false;
	}
	QFile::remove(path);
	if (!QFile::rename(path + QStringLiteral(".situ-edit-new"), path)) {
		error_ = QStringLiteral("cannot write %1").arg(path);
		return false;
	}
	*modified_ = false;
	return true;
}

bool session::open_nested(const session &parent, const QString &structure,
                          qint64 offset, qint64 length)
{
	// The parent's own window joins the chain this one was opened down,
	// so a write here can find what frames it (26.1141).
	const open_options &above = parent.options_;
	QStringList within = above.within;
	within << above.structure + QLatin1Char(':')
	          + QString::number(above.offset) + QLatin1Char(':')
	          + (above.length >= 0 ? QString::number(above.length) : QString());
	options_           = parent.options_;
	options_.within    = within;
	options_.structure = structure;
	options_.offset    = offset;
	options_.length    = length;
	// Raw bytes already: the parent turned any hex into the working file.
	options_.hex       = false;
	// A nested struct's own arguments are the ones its parent hands it,
	// which the CLI computes; none are passed down from the command line.
	options_.arguments.clear();
	scratch_  = parent.scratch_;
	image_    = parent.image_;
	work_     = parent.work_;
	modified_ = parent.modified_;
	notes_.clear();
	return refresh();
}
