pif: a REST interface to an Obsidian vault
==========================================

The application joins three concerns: a persistent current directory, markdown
files that double as reusable prompts, and optional Gemini text generation.
``Vault`` owns filesystem access; this module defines request contracts and
connects those operations to HTTP endpoints.

The vault root is fixed at ``~/obsidian``. Navigation changes one shared,
persistent current directory inside that root. Prompt names always resolve
under the root's ``prompts`` directory, regardless of navigation. Settings live
separately in ``~/.pif/settings.json`` and cannot be reached through file routes.

This service intentionally has no authentication. Run one application instance
with one worker: settings and the write lock are process-local, and all clients
share them. External Obsidian edits are read directly from disk rather than
from an application cache.

We begin with the HTTP, validation, filesystem, and asynchronous building
blocks. Gemini is imported only when generation is requested::

  import asyncio
  import json
  import logging
  import os
  import threading
  from contextlib import asynccontextmanager
  from pathlib import Path
  from typing import Annotated

  import yaml
  from fastapi import FastAPI, Query, Request, Response
  from fastapi.exceptions import RequestValidationError
  from fastapi.responses import JSONResponse
  from pydantic import BaseModel, ConfigDict, Field, StrictStr
  from starlette.exceptions import HTTPException

  from pif.storage import Problem, Vault

Defaults and request contracts
------------------------------

The initial model can be replaced through the settings endpoint. Image media
types come from the filename extension; this table is an allowlist, not an
image decoder or a content-signature check::

  DEFAULT_MODEL = "gemini-3.8-flash"
  IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
                 ".tif": "image/tiff", ".tiff": "image/tiff", ".ico": "image/vnd.microsoft.icon",
                 ".svg": "image/svg+xml"}


Request models reject unknown fields and use strict strings to avoid silent
coercion of file contents or names. A settings patch distinguishes omitted
fields from explicit nulls in the endpoint; null does not clear a setting.
Prompt bodies carry their tags separately from the markdown instruction,
while an apply request identifies an existing prompt and page::

  class Input(BaseModel):
      model_config = ConfigDict(extra="forbid")


  class Settings(Input):
      current_folder: StrictStr = "."
      gemini_model: Annotated[StrictStr, Field(min_length=1)] = DEFAULT_MODEL


  class SettingsPatch(Input):
      current_folder: StrictStr | None = None
      gemini_model: StrictStr | None = None


  class Markdown(Input):
      content: StrictStr


  class PromptBody(Markdown):
      tags: list[StrictStr] = Field(default_factory=list)


  class PromptCreate(PromptBody):
      name: StrictStr


  class Apply(Input):
      prompt: StrictStr
      page: StrictStr


Interpreting prompt files
-------------------------

Prompts can be edited through REST or directly in Obsidian, so tag validation
also runs on parsed YAML. Normalization trims surrounding whitespace, removes
duplicates, and sorts the result. Case is preserved for exact tag matching::

  def tags_clean(tags):
      if not isinstance(tags, list) or any(not isinstance(tag, str) or not tag.strip() for tag in tags):
          raise Problem(422, "invalid_tags", "Tags must be a list of nonempty strings.")
      return sorted(set(tag.strip() for tag in tags))


A prompt identifier is a single filename stem, not a navigable path. Appending
``.md`` beneath ``prompts/`` keeps prompt operations independent of the shared
current directory. Actual filesystem access still passes through ``Vault``::

  def prompt_path(name):
      if not name or name in (".", "..") or any(char in name for char in "/\\\x00") or name.endswith(".md"):
          raise Problem(422, "invalid_prompt_name", "Use a filename without .md or path separators.")
      return "prompts/" + name + ".md"


Frontmatter is recognized only when the first line, after trimming, is
``---``; the next such delimiter ends it. Safe YAML loading accepts ordinary
data rather than constructing arbitrary Python objects. Metadata must be a
mapping with string keys, and absent tags default to an empty list.

Keeping line endings during the split preserves the instruction body as read.
Returning body, metadata, and normalized tags separately lets generation omit
metadata while updates retain unrelated frontmatter fields::

  def parse_prompt(raw):
      lines = raw.splitlines(keepends=True)
      metadata = {}
      body = raw
      if lines and lines[0].strip() == "---":
          end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
          if end is None:
              raise Problem(422, "invalid_frontmatter", "Unterminated YAML frontmatter.")
          try:
              metadata = yaml.safe_load("".join(lines[1:end]))
          except yaml.YAMLError as exc:
              raise Problem(422, "invalid_frontmatter", "Invalid YAML frontmatter.") from exc
          if metadata is None:
              metadata = {}
          if not isinstance(metadata, dict) or any(not isinstance(k, str) for k in metadata):
              raise Problem(422, "invalid_frontmatter", "Frontmatter must be a mapping with string keys.")
          body = "".join(lines[end + 1:])
      tags = tags_clean(metadata.get("tags", []))
      return body, metadata, tags


Calling Gemini through a replaceable adapter
--------------------------------------------

Filesystem endpoints do not require an API key. This adapter checks the key
only when called and gives its asynchronous SDK client a request-scoped
lifetime. The prompt body becomes the system instruction; the complete page,
including its own frontmatter, becomes the input content. No attachments,
linked pages, or template variables are expanded.

An empty response and SDK request failures become sanitized public errors.
The HTTP operation below owns the overall generation timeout. Tests can replace
this adapter with an asynchronous function accepting the same three arguments::

  async def generate(model, prompt, page):
      key = os.environ.get("GEMINI_API_KEY")
      if not key:
          raise Problem(503, "gemini_unconfigured", "GEMINI_API_KEY is not configured.")
      from google import genai
      from google.genai import types
      try:
          async with genai.Client(api_key=key).aio as client:
              result = await client.models.generate_content(
                  model=model, contents=page,
                  config=types.GenerateContentConfig(system_instruction=prompt))
              if not result.text:
                  raise Problem(502, "empty_generation", "Gemini returned no text or blocked the request.")
              return result.text
      except Problem:
          raise
      except Exception as exc:
          raise Problem(502, "gemini_error", "Gemini request failed.") from exc


Constructing an isolated application
------------------------------------

The factory accepts a home directory and a generation adapter so tests can
use temporary vaults without touching real notes or making paid requests.
Creating the app registers routes; filesystem initialization waits for lifespan
startup. A reentrant lock permits a settings update to call ``snapshot()``
while already holding the same lock::

  def create_app(home: Path | None = None, generator=None):
      home = Path(home) if home is not None else Path.home()
      lock = threading.RLock()

Startup creates missing directories, opens the vault and settings roots, and
verifies that ``prompts`` is a real directory. Descriptor ownership is paired
with cleanup even if later initialization fails.

Missing settings receive defaults, while malformed settings stop startup.
A saved folder that has disappeared resets to the root; forbidden paths still
fail. The validated settings are persisted before becoming active in memory.
The lifespan yield marks the interval during which requests may use the open
roots, and shutdown closes both descriptors::

      @asynccontextmanager
      async def lifespan(app):
          for directory in (home / ".pif", home / "obsidian"):
              directory.mkdir(mode=0o700, exist_ok=True)
          vault = Vault(home / "obsidian")
          try:
              config = Vault(home / ".pif")
          except Exception:
              vault.close()
              raise
          app.state.vault, app.state.config = vault, config
          try:
              with vault.directory(".") as fd:
                  try:
                      os.mkdir("prompts", mode=0o700, dir_fd=fd)
                  except FileExistsError:
                      pass
              with vault.directory("prompts"):
                  pass
              try:
                  settings = Settings.model_validate_json(config.text("settings.json"))
                  if not settings.gemini_model.strip():
                      raise ValueError("Model must be nonempty")
              except Problem as exc:
                  if exc.status != 404:
                      raise
                  settings = Settings()
              except ValueError as exc:
                  raise RuntimeError("Invalid ~/.pif/settings.json") from exc
              settings.current_folder = vault.path(settings.current_folder, ".")
              try:
                  with vault.directory(settings.current_folder):
                      pass
              except Problem as exc:
                  if exc.status != 404:
                      raise
                  logging.getLogger("pif").warning("Saved current folder is missing; resetting to vault root")
                  settings.current_folder = "."
              config.write("settings.json", settings.model_dump_json(indent=2).encode())
              app.state.settings = settings
              yield
          finally:
              vault.close()
              config.close()

Responses and shared request helpers
------------------------------------

The application exposes the injected generator through its state. Storage
problems, request validation failures, and framework HTTP errors share the
same ``error`` envelope. Validation messages deliberately omit submitted values,
which can include note contents::

      app = FastAPI(title="pif", description="Pi files — Obsidian vault REST API", lifespan=lifespan)
      app.state.generator = generator or generate

      @app.exception_handler(Problem)
      async def problem_handler(request: Request, exc: Problem):
          return JSONResponse(status_code=exc.status, content={"error": {"code": exc.code, "message": exc.message}})

      @app.exception_handler(RequestValidationError)
      async def validation_handler(request: Request, exc: RequestValidationError):
          return JSONResponse(status_code=422, content={"error": {"code": "invalid_input", "message": "Invalid request fields or types."}})

      @app.exception_handler(HTTPException)
      async def http_handler(request: Request, exc: HTTPException):
          return JSONResponse(status_code=exc.status_code, headers=exc.headers,
                              content={"error": {"code": "http_error", "message": str(exc.detail)}})

A request copies settings under the lock before resolving paths or invoking
Gemini. A later navigation or model update cannot change that request's chosen
folder or model. This copies configuration only; it does not freeze file
contents. Markdown eligibility is checked after vault-relative normalization::

      def snapshot():
          with lock:
              return app.state.settings.model_copy()

      def page_path(path, settings):
          resolved = app.state.vault.path(path, settings.current_folder)
          if Path(resolved).suffix.lower() != ".md":
              raise Problem(422, "unsupported_type", "A .md file is required.")
          return resolved

Prompt responses include the parsed body, normalized tags, and metadata.
Safe YAML scalars such as dates are converted to JSON-compatible representations;
cyclic or otherwise unserializable structures become validation errors::

      def read_prompt(name):
          content, metadata, tags = parse_prompt(app.state.vault.text(prompt_path(name)))
          # YAML dates and other safe scalar values must remain JSON serializable.
          try:
              metadata_json = json.loads(json.dumps(metadata, default=str))
          except (ValueError, TypeError, RecursionError) as exc:
              raise Problem(422, "invalid_frontmatter", "Frontmatter must contain serializable values.") from exc
          return {"name": name, "content": content, "tags": tags, "metadata": metadata_json}

Prompt discovery scans the fixed prompt directory afresh. One malformed file
must not hide usable prompts: successful records and per-file errors are
returned separately. Tag filtering is exact and applies only to valid records;
errors remain visible even when a filter is supplied::

      def prompts(tag=None):
          items, errors = [], []
          for entry in app.state.vault.listing("prompts"):
              if entry["type"] != "file" or not entry["name"].endswith(".md"):
                  continue
              name = entry["name"][:-3]
              try:
                  item = read_prompt(name)
                  if tag is None or tag in item["tags"]:
                      items.append(item)
              except Problem as exc:
                  errors.append({"name": name, "code": exc.code, "message": exc.message})
          return {"prompts": items, "errors": errors}

Persisting navigation and model selection
-----------------------------------------

GET returns a settings snapshot. PATCH applies only supplied fields to a copy,
verifies directory access and a nonblank model name, then writes that copy
before publishing it in memory. If the write fails before publication, the
active settings stay unchanged. The model identifier is stored without an
upstream availability check; Gemini validates it when used::

      @app.get("/api/settings", response_model=Settings)
      def get_settings():
          return snapshot()

      @app.patch("/api/settings", response_model=Settings)
      def patch_settings(body: SettingsPatch):
          with lock:
              settings = snapshot()
              if "current_folder" in body.model_fields_set:
                  if body.current_folder is None:
                      raise Problem(422, "invalid_path", "Folder cannot be null.")
                  settings.current_folder = app.state.vault.path(body.current_folder, settings.current_folder)
                  with app.state.vault.directory(settings.current_folder):
                      pass
              if "gemini_model" in body.model_fields_set:
                  if body.gemini_model is None or not body.gemini_model.strip():
                      raise Problem(422, "invalid_model", "Model must be nonempty.")
                  settings.gemini_model = body.gemini_model.strip()
              app.state.config.write("settings.json", settings.model_dump_json(indent=2).encode())
              app.state.settings = settings
              return settings

Browsing and editing the current directory
------------------------------------------

The directory listing uses the captured current folder. Markdown reads and
writes resolve a client path against the same navigation convention; responses
identify the resulting vault-relative path. PUT accepts the complete document,
creates a file only in an existing parent, and distinguishes creation (201)
from replacement (200). It does not merge concurrent edits::

      @app.get("/api/files")
      def files():
          settings = snapshot()
          return {"current_folder": settings.current_folder, "entries": app.state.vault.listing(settings.current_folder)}

      @app.get("/api/markdown")
      def get_markdown(path: str = Query(...)):
          resolved = page_path(path, snapshot())
          return {"path": resolved, "content": app.state.vault.text(resolved)}

      @app.put("/api/markdown")
      def put_markdown(body: Markdown, response: Response, path: str = Query(...)):
          resolved = page_path(path, snapshot())
          with lock:
              created = app.state.vault.write(resolved, body.content.encode("utf-8"))
          response.status_code = 201 if created else 200
          return {"path": resolved, "content": body.content}

Images use the same path boundary but return raw bytes rather than JSON.
The extension selects the response media type, and ``nosniff`` discourages
browser reinterpretation. SVG is served as an attachment. There is no upload
route, and the storage read loads the entire image into memory::

      @app.get("/api/images")
      def get_image(path: str = Query(...)):
          resolved = app.state.vault.path(path, snapshot().current_folder)
          media_type = IMAGE_TYPES.get(Path(resolved).suffix.lower())
          if media_type is None:
              raise Problem(422, "unsupported_type", "Unsupported image format.")
          headers = {"X-Content-Type-Options": "nosniff"}
          if media_type == "image/svg+xml":
              headers["Content-Disposition"] = "attachment"
          return Response(app.state.vault.read(resolved), media_type=media_type, headers=headers)

Managing the prompt collection
------------------------------

Listing delegates to the tolerant scanner. Creation serializes tags into YAML
frontmatter and appends the instruction body. A create-only storage write
rejects an existing name atomically; the returned record is read back through
the same parser used for externally edited prompts::

      @app.get("/api/prompts")
      def list_prompts(tag: str | None = None):
          return prompts(tag)

      @app.post("/api/prompts", status_code=201)
      def create_prompt(body: PromptCreate):
          path = prompt_path(body.name)
          tags = tags_clean(body.tags)
          raw = "---\n" + yaml.safe_dump({"tags": tags}, allow_unicode=True) + "---\n" + body.content
          with lock:
              app.state.vault.write(path, raw.encode(), create_only=True)
              return read_prompt(body.name)

Individual reads report malformed files directly. Updating an existing prompt
replaces its body and tags while preserving other parsed metadata values.
YAML is serialized anew, so comments and original frontmatter formatting are
not preserved. The lock covers the application's read-modify-write sequence,
but external editors remain independent writers::

      @app.get("/api/prompts/{name}")
      def get_prompt(name: str):
          return read_prompt(name)

      @app.put("/api/prompts/{name}")
      def update_prompt(name: str, body: PromptBody):
          path = prompt_path(name)
          with lock:
              _, metadata, _ = parse_prompt(app.state.vault.text(path))
              metadata["tags"] = tags_clean(body.tags)
              raw = "---\n" + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False) + "---\n" + body.content
              app.state.vault.write(path, raw.encode(), existing_only=True)
              return read_prompt(name)

Deletion is intentionally limited to named prompts and returns an empty 204
response. The tag index is derived from valid prompt files on each request,
so it requires no database or synchronization job; it carries the scanner's
errors alongside the sorted, unique tags::

      @app.delete("/api/prompts/{name}", status_code=204)
      def delete_prompt(name: str):
          with lock:
              app.state.vault.delete(prompt_path(name))
          return Response(status_code=204)

      @app.get("/api/tags")
      def get_tags():
          result = prompts()
          return {"tags": sorted({tag for item in result["prompts"] for tag in item["tags"]}), "errors": result["errors"]}

Applying a prompt without changing the page
-------------------------------------------

The operation snapshots settings, reads the chosen prompt from its fixed
location, and resolves the page from the captured current directory. File reads
run in worker threads so they do not block the event loop. Prompt and page
reads are independent, not a joint filesystem snapshot.

Only the generation call is inside the 120-second timeout. Timeout, empty text,
and upstream failures have distinct public outcomes. The response includes the
captured model and resolved page path for traceability. No result is written:
a client that wants to save it must explicitly call the markdown PUT route::

      @app.post("/api/apply-prompt")
      async def apply_prompt(body: Apply):
          settings = snapshot()
          path = page_path(body.page, settings)
          prompt = await asyncio.to_thread(read_prompt, body.prompt)
          page = await asyncio.to_thread(app.state.vault.text, path)
          try:
              async with asyncio.timeout(120):
                  result = await app.state.generator(settings.gemini_model, prompt["content"], page)
              if not isinstance(result, str) or not result.strip():
                  raise Problem(502, "empty_generation", "Gemini returned no text or blocked the request.")
          except TimeoutError as exc:
              raise Problem(504, "gemini_timeout", "Gemini request timed out.") from exc
          except Problem:
              raise
          except Exception as exc:
              raise Problem(502, "gemini_error", "Gemini request failed.") from exc
          return {"model": settings.gemini_model, "prompt": body.prompt, "page": path, "result": result}

      return app


The ASGI entry point
--------------------

Uvicorn imports ``pif.app:app``. This default instance uses the service user's
home directory and the real Gemini adapter, with resource setup deferred until
application startup::

  app = create_app()
