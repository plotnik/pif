# pif — pi files

FastAPI REST access to an Obsidian vault on a Raspberry Pi. Python 3.11+ on Linux (also works on macOS). No database or Obsidian plugin required.

## Install and run

From this directory on your Pi:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
export GEMINI_API_KEY='your-key'
uvicorn pif.app:app --host 0.0.0.0 --port 8000 --workers 1
```

Use a single worker and one service instance: the current directory is shared by all clients, and settings updates are serialized within that process. This service intentionally has no authentication; clients that can reach port 8000 can read/write the vault and invoke Gemini. CORS is not enabled.

Interactive API documentation: `http://raspberrypi.local:8000/docs`; schema: `/openapi.json`.

On startup pif creates `~/obsidian`, `~/obsidian/prompts`, and `~/.pif/settings.json` if missing. Here `~` is the home of the service user. Existing content is retained. Settings default to:

```json
{"current_folder": ".", "gemini_model": "gemini-3.8-flash"}
```

Set the model through REST; changes survive restarts. `GEMINI_API_KEY` is read from the environment and never stored in settings. File operations work without it. Invalid settings stop startup; a missing saved directory resets to the vault root with a warning.

## Paths and files

The vault boundary is fixed at `~/obsidian`. Paths are relative to the shared current folder; `/` starts at the vault root, not the system root. `..` is allowed within the vault. Symlinks and special files are inaccessible and omitted from listings. Directory-relative operations with no-follow flags prevent symlink escapes. The underlying vault directories should not be moved out of the vault while requests are active.

Markdown is UTF-8, including frontmatter. PUT creates or replaces a file atomically; its parent must exist. Updates preserve file permissions. Settings and file writes are last-writer-wins; there is no edit history or merge support. Obsidian edits become visible on the next request. No general delete, folder creation, or image upload endpoint is provided.

Images support PNG, JPEG, GIF, WebP, BMP, TIFF, ICO, and SVG, identified by extension. SVG responses download as attachments. Images are returned as bytes; the current implementation reads each image into memory.

## API examples

Set a shell variable for the service:

```sh
base=http://raspberrypi.local:8000/api
```

Read settings, navigate (target must exist), select a model, and list the current directory:

```sh
curl "$base/settings"
curl -X PATCH "$base/settings" -H 'Content-Type: application/json' -d '{"current_folder":"/"}'
curl -X PATCH "$base/settings" -H 'Content-Type: application/json' -d '{"gemini_model":"gemini-3.8-flash"}'
curl "$base/files"
```

Listings return `{current_folder, entries}`. Each entry contains `name`, vault-relative `path`, `type` (`directory` or `file`), and `size` (null for directories). Folders sort first, then names.

Read/write markdown and download an existing image:

```sh
curl -X PUT "$base/markdown?path=example.md" -H 'Content-Type: application/json' -d '{"content":"# Example\nHello"}'
curl --get "$base/markdown" --data-urlencode 'path=example.md'
curl --get "$base/images" --data-urlencode 'path=attachments/photo.jpg' -o photo.jpg
```

Markdown responses are `{path, content}`. Creation returns 201 and replacement returns 200.

Create, list, filter, read, update, and delete prompts:

```sh
curl -X POST "$base/prompts" -H 'Content-Type: application/json' -d '{"name":"summary","content":"Summarize this page.","tags":["writing","summary"]}'
curl "$base/prompts"
curl --get "$base/prompts" --data-urlencode 'tag=writing'
curl "$base/prompts/summary"
curl -X PUT "$base/prompts/summary" -H 'Content-Type: application/json' -d '{"content":"Summarize in three bullets.","tags":["summary"]}'
curl "$base/tags"
```

Prompts always reside directly in `~/obsidian/prompts`, regardless of current folder. Names exclude `.md` and cannot contain path separators. Each file uses YAML frontmatter:

```markdown
---
tags:
  - writing
  - summary
---
Summarize this page.
```

No frontmatter means no tags. Tags must be nonempty strings; whitespace is trimmed, duplicates removed, and matching is case-sensitive. PUT replaces body/tags and preserves other frontmatter fields (YAML formatting/comments may change). GET returns `{name, content, tags, metadata}`. Lists return `{prompts, errors}`; tags return `{tags, errors}`. Malformed prompt files appear in `errors` without hiding valid prompts; reading/applying them returns 422.

Apply a prompt, then delete it:

```sh
curl -X POST "$base/apply-prompt" -H 'Content-Type: application/json' -d '{"prompt":"summary","page":"example.md"}'
curl -X DELETE "$base/prompts/summary"
```

Gemini receives the prompt body as its system instruction and the entire markdown page as input. Frontmatter tags are not part of the prompt instruction. Attachments and linked notes are not expanded; template variables are not substituted. Response: `{model, prompt, page, result}`. Nothing is saved; use the markdown PUT endpoint to save a result explicitly. Requests time out after 120 seconds. Calls use your Gemini account and may incur charges.

Errors use `{"error":{"code":"...","message":"..."}}`: 403 forbidden paths, 404 missing files/parents, 409 duplicate prompt, 422 invalid inputs/frontmatter, 500 storage failure, 502 upstream/empty generation, 503 missing API key, 504 Gemini timeout.

## Run with systemd

Copy this project to `/home/pi/pif` and install its virtualenv as above. Edit `deployment/pif.service` to match your username and installation path. The account's home determines the vault and settings location.

```sh
mkdir -p ~/.pif
chmod 700 ~/.pif
printf 'GEMINI_API_KEY=your-key\n' > ~/.pif/environment
chmod 600 ~/.pif/environment
sudo cp deployment/pif.service /etc/systemd/system/pif.service
sudo systemctl daemon-reload
sudo systemctl enable --now pif
sudo systemctl status pif
journalctl -u pif
```

After changing the environment file, restart the service. Model and folder changes through REST do not need a restart.

## Tests

```sh
.venv/bin/python -m pytest
```

Tests use isolated temporary vaults and mock Gemini. They never make paid API requests. They cover persistence, traversal and symlinks, atomic write failures, Unicode, prompt metadata/tags, image reads, and Gemini failures.
