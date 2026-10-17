Storage: keeping file operations inside a vault
===============================================

The HTTP layer decides which kinds of files a client may use. This module
provides the lower-level mechanism: a directory descriptor anchors every
operation, and descendant directories are opened without following symlinks.
The same abstraction serves the Obsidian vault and the private settings folder.

There are two stages to access. ``path()`` interprets a client path relative to
an application-level current directory; the remaining methods consume that
normalized, root-relative path. They must not receive unvalidated client paths.
No operation changes the process working directory.

Descriptor-relative access avoids checking a pathname and then reopening it
through a potentially substituted symlink. It is not an isolation boundary
against another local process moving an already-open directory out of the
vault; vault directories should remain in place while requests are active.

The implementation uses POSIX directory descriptors and no-follow flags, as
available on the Raspberry Pi's Linux system and on macOS::

  """Descriptor-relative vault access; no path component may be a symlink."""
  import errno
  import os
  import stat
  import uuid
  from contextlib import contextmanager


Reporting failures without exposing filesystem details
------------------------------------------------------

Storage failures carry an HTTP status, a stable machine-readable code, and a
public message. Keeping this exception independent of FastAPI lets storage be
used and tested without an HTTP request. The original exception is retained as
its cause where useful, but its raw message is not sent to clients::

  class Problem(Exception):
      def __init__(self, status: int, code: str, message: str):
          self.status, self.code, self.message = status, code, message


Several operating-system errors describe the same public outcome. In
particular, a symlink encountered during directory traversal can appear as
``ELOOP`` or ``ENOTDIR``. Missing resources and name conflicts have distinct
responses; unexpected failures become a generic storage error::

  def translate(exc):
      if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EACCES, errno.EPERM):
          return Problem(403, "forbidden_path", "Path is forbidden or contains a symlink/non-directory.")
      if exc.errno == errno.ENOENT:
          return Problem(404, "not_found", "Resource or parent directory does not exist.")
      if exc.errno == errno.EEXIST:
          return Problem(409, "already_exists", "Resource already exists.")
      return Problem(500, "storage_error", "Filesystem operation failed.")


Owning the root and interpreting paths
--------------------------------------

A vault owns one open root descriptor for its lifetime. The application closes
it during shutdown. Opening the root itself with ``O_NOFOLLOW`` also rejects a
vault or settings directory that is a symlink::

  class Vault:
      def __init__(self, root):
          self.fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

      def close(self):
          os.close(self.fd)

A leading slash means the vault root, not the host filesystem root. Otherwise,
resolution starts from the saved current directory. Empty components and dots
are discarded; each parent component removes one accumulated component, and
attempting to remove beyond the root is forbidden. The root is represented by
``"."``. This is lexical normalization, not a filesystem existence check::

      def path(self, value, cwd="."):
          if not value or "\x00" in value or "\\" in value:
              raise Problem(422, "invalid_path", "A nonempty POSIX path is required.")
          parts = [] if value.startswith("/") else ([] if cwd == "." else cwd.split("/"))
          for part in value.split("/"):
              if part in ("", "."):
                  continue
              if part == "..":
                  if not parts:
                      raise Problem(403, "forbidden_path", "Path escapes the vault.")
                  parts.pop()
              else:
                  parts.append(part)
          return "/".join(parts) or "."

Walking directories without following links
-------------------------------------------

Each traversal starts with a duplicate of the root descriptor so its cleanup
cannot close the vault itself. Opening one component at a time with
``O_DIRECTORY | O_NOFOLLOW`` prevents an intermediate symlink from redirecting
access. A caller may use the yielded descriptor only inside the context.

The ``try`` surrounds the yield as well as the traversal. Consequently,
operating-system errors raised by a caller's file operation are translated
here too, and the temporary directory descriptor is always closed::

      @contextmanager
      def directory(self, path):
          fd = os.dup(self.fd)
          try:
              for part in ([] if path == "." else path.split("/")):
                  nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                  os.close(fd)
                  fd = nxt
              yield fd
          except OSError as exc:
              raise translate(exc) from exc
          finally:
              os.close(fd)

File operations need both a trusted parent descriptor and a single final name.
Splitting a normalized path supplies that pair without resolving the final
component prematurely. The root itself is not a valid file target::

      @contextmanager
      def parent(self, path):
          if path == ".":
              raise Problem(422, "invalid_path", "A file path is required.")
          parent, _, name = path.rpartition("/")
          with self.directory(parent or ".") as fd:
              yield fd, name

Reading regular files
---------------------

The final component also uses ``O_NOFOLLOW``. Inspecting the opened descriptor
checks the actual object being read rather than a prior pathname lookup.
``O_NONBLOCK`` prevents opening a FIFO from hanging before it can be rejected.
Only regular files are accepted, and their contents are read fully into memory.
The text adapter adds UTF-8 decoding and a client-facing encoding error::

      def read(self, path):
          with self.parent(path) as (parent, name):
              fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
              with os.fdopen(fd, "rb") as stream:
                  if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                      raise Problem(403, "forbidden_path", "Only regular files are accessible.")
                  return stream.read()

      def text(self, path):
          try:
              return self.read(path).decode("utf-8")
          except UnicodeDecodeError as exc:
              raise Problem(422, "invalid_encoding", "Markdown must be UTF-8.") from exc

Publishing complete files
-------------------------

Writes first inspect an existing destination without following links. They
reject non-regular objects, and optionally require an existing file. A new
file starts with private permissions; replacing a file preserves its mode.

Bytes are written and flushed to a unique temporary file in the same directory.
For create-only requests, linking that file to the destination makes the
no-overwrite decision atomic. Other writes use atomic replacement, so readers
see either the old file or the complete new file. The parent directory is then
synced and any remaining temporary name is removed, including on failure.

``existing_only`` is a precondition check, not a transaction with external
editors: another process can remove the destination after that check. Likewise,
the returned creation flag reflects the initial lookup. The application lock
serializes its own writers; it does not coordinate Obsidian or other processes.
An error after publication, such as a directory-sync failure, does not roll
back the already-published contents::

      def write(self, path, data, create_only=False, existing_only=False):
          with self.parent(path) as (parent, name):
              try:
                  info = os.stat(name, dir_fd=parent, follow_symlinks=False)
              except FileNotFoundError:
                  info = None
              if info and not stat.S_ISREG(info.st_mode):
                  raise Problem(403, "forbidden_path", "Only regular files can be replaced.")
              if existing_only and info is None:
                  raise Problem(404, "not_found", "File does not exist.")
              temp = ".pif-" + uuid.uuid4().hex
              try:
                  fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
                  with os.fdopen(fd, "wb") as stream:
                      if info:
                          os.fchmod(stream.fileno(), stat.S_IMODE(info.st_mode))
                      stream.write(data)
                      stream.flush()
                      os.fsync(stream.fileno())
                  if create_only:
                      os.link(temp, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                      os.unlink(temp, dir_fd=parent)
                  else:
                      os.replace(temp, name, src_dir_fd=parent, dst_dir_fd=parent)
                  os.fsync(parent)
              finally:
                  try:
                      os.unlink(temp, dir_fd=parent)
                  except FileNotFoundError:
                      pass
              return info is None

Deleting and listing entries
----------------------------

Deletion removes a regular file's directory entry. It does not follow the
entry if another process replaces it with a symlink between the check and the
unlink. The API uses this primitive only for prompt deletion::

      def delete(self, path):
          with self.parent(path) as (parent, name):
              info = os.stat(name, dir_fd=parent, follow_symlinks=False)
              if not stat.S_ISREG(info.st_mode):
                  raise Problem(403, "forbidden_path", "Only regular files can be deleted.")
              os.unlink(name, dir_fd=parent)

A listing is a best-effort view of one directory, not a filesystem snapshot.
An entry disappearing during inspection is skipped. Symlinks and special files
are omitted, and regular files expose their byte size. Paths remain relative
to the vault root, with directories sorted before files for predictable output::

      def listing(self, path):
          entries = []
          with self.directory(path) as fd:
              for name in os.listdir(fd):
                  try:
                      info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                  except FileNotFoundError:
                      continue
                  if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                      continue
                  folder = stat.S_ISDIR(info.st_mode)
                  entries.append(dict(name=name, path=name if path == "." else path + "/" + name,
                                      type="directory" if folder else "file", size=None if folder else info.st_size))
          return sorted(entries, key=lambda entry: (entry["type"] != "directory", entry["name"]))
