import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pif.app import create_app


@pytest.fixture
def env(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        yield client, tmp_path / "obsidian", app


def test_settings_navigation_and_restart(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        vault = tmp_path / "obsidian"
        (vault / "notes").mkdir()
        assert client.get("/api/settings").json()["current_folder"] == "."
        assert client.patch("/api/settings", json={"current_folder": "notes", "gemini_model": "test-model"}).status_code == 200
        assert client.put("/api/markdown", params={"path": "héllo.md"}, json={"content": "你好"}).status_code == 201
        assert (vault / "notes/héllo.md").read_text() == "你好"
        assert client.patch("/api/settings", json={"current_folder": "missing"}).status_code == 404
        assert client.get("/api/settings").json()["current_folder"] == "notes"
    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/api/settings").json() == {"current_folder": "notes", "gemini_model": "test-model"}
        assert client.patch("/api/settings", json={"current_folder": ".."}).json()["current_folder"] == "."
        assert client.patch("/api/settings", json={"current_folder": "/notes"}).status_code == 200
    (tmp_path / "obsidian/notes/héllo.md").unlink()
    (tmp_path / "obsidian/notes").rmdir()
    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/api/settings").json()["current_folder"] == "."


def test_invalid_settings(tmp_path):
    (tmp_path / ".pif").mkdir()
    (tmp_path / ".pif/settings.json").write_text("broken")
    with pytest.raises(RuntimeError, match="Invalid"):
        with TestClient(create_app(tmp_path)):
            pass


def test_markdown_and_listing(env):
    client, vault, _ = env
    (vault / "folder").mkdir()
    for content, status in [("first", 201), ("second", 200)]:
        assert client.put("/api/markdown?path=a.md", json={"content": content}).status_code == status
    assert client.get("/api/markdown?path=a.md").json()["content"] == "second"
    entries = client.get("/api/files").json()["entries"]
    assert [e["name"] for e in entries] == ["folder", "prompts", "a.md"]
    assert entries[-1]["size"] == 6
    assert client.put("/api/markdown?path=missing/a.md", json={"content": "x"}).status_code == 404
    assert client.get("/api/markdown?path=a.txt").status_code == 422
    assert client.get("/api/markdown?path=missing.md").status_code == 404
    assert client.put("/api/markdown?path=a.md", json={"content": 42}).status_code == 422
    (vault / "bad.md").write_bytes(b"\xff")
    assert client.get("/api/markdown?path=bad.md").status_code == 422


def test_boundary_and_symlinks(env, tmp_path):
    client, vault, _ = env
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("secret")
    (vault / "escape").symlink_to(outside, target_is_directory=True)
    (vault / "link.md").symlink_to(outside / "secret.md")
    for path in ["../outside/secret.md", "escape/secret.md", "link.md", "/../outside/secret.md"]:
        assert client.get("/api/markdown", params={"path": path}).status_code == 403
        assert client.put("/api/markdown", params={"path": path}, json={"content": "overwrite"}).status_code == 403
    assert (outside / "secret.md").read_text() == "secret"
    assert client.patch("/api/settings", json={"current_folder": "escape"}).status_code == 403
    assert not any(e["name"] in ("escape", "link.md") for e in client.get("/api/files").json()["entries"])
    assert client.put("/api/markdown?path=escape/new.md", json={"content": "x"}).status_code == 403
    os.mkfifo(vault / "pipe.md")
    assert client.get("/api/markdown?path=pipe.md").status_code == 403


def test_images(env):
    client, vault, _ = env
    (vault / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    response = client.get("/api/images?path=image.png")
    assert response.content == (vault / "image.png").read_bytes()
    assert response.headers["content-type"] == "image/png"
    (vault / "image.svg").write_text("<svg/>")
    assert client.get("/api/images?path=image.svg").headers["content-disposition"] == "attachment"
    assert client.get("/api/images?path=notes.md").status_code == 422


def test_prompt_crud_and_external_edits(env):
    client, vault, _ = env
    body = {"name": "summarize", "content": "Summarize this.", "tags": [" writing ", "summary", "writing"]}
    assert client.post("/api/prompts", json=body).status_code == 201
    assert client.post("/api/prompts", json=body).status_code == 409
    assert client.get("/api/tags").json()["tags"] == ["summary", "writing"]
    assert len(client.get("/api/prompts?tag=summary").json()["prompts"]) == 1
    assert client.get("/api/prompts?tag=Summary").json()["prompts"] == []
    file = vault / "prompts/summarize.md"
    file.write_text("---\ntitle: Keep me\ntags: [old]\n---\nOld")
    assert client.put("/api/prompts/summarize", json={"content": "New", "tags": ["new"]}).status_code == 200
    result = client.get("/api/prompts/summarize").json()
    assert result["metadata"]["title"] == "Keep me"
    assert result["content"] == "New"
    (vault / "sub").mkdir()
    client.patch("/api/settings", json={"current_folder": "sub"})
    assert client.get("/api/prompts/summarize").status_code == 200
    assert client.delete("/api/prompts/summarize").status_code == 204
    assert not file.exists()
    assert client.get("/api/prompts/summarize").status_code == 404
    assert client.put("/api/prompts/missing", json={"content": "x"}).status_code == 404
    assert client.post("/api/prompts", json={**body, "name": "../bad"}).status_code == 422


def test_malformed_prompts(env):
    client, vault, _ = env
    (vault / "prompts/good.md").write_text("Plain prompt")
    for name, content in {"bad": "---\ntags: [\n---\nBad", "tags": "---\ntags: string\n---\nBad", "end": "---\ntags: []"}.items():
        (vault / f"prompts/{name}.md").write_text(content)
        assert client.get(f"/api/prompts/{name}").status_code == 422
    result = client.get("/api/prompts").json()
    assert len(result["prompts"]) == 1
    assert len(result["errors"]) == 3
    assert len(client.get("/api/tags").json()["errors"]) == 3


def test_gemini(env, monkeypatch):
    client, vault, app = env
    client.post("/api/prompts", json={"name": "p", "content": "Instruction", "tags": ["hidden"]})
    (vault / "page.md").write_text("---\ntitle: Page\n---\nText")
    client.patch("/api/settings", json={"gemini_model": "chosen"})
    seen = []
    async def mock(model, prompt, page):
        seen.append((model, prompt, page))
        return "Generated"
    app.state.generator = mock
    payload = {"prompt": "p", "page": "page.md"}
    result = client.post("/api/apply-prompt", json=payload)
    assert result.status_code == 200
    assert result.json()["result"] == "Generated"
    assert seen == [("chosen", "Instruction", "---\ntitle: Page\n---\nText")]
    assert (vault / "page.md").read_text() == seen[0][2]
    for exception, status in [(TimeoutError(), 504), (RuntimeError("secret key"), 502)]:
        async def fail(*args):
            raise exception
        app.state.generator = fail
        response = client.post("/api/apply-prompt", json=payload)
        assert response.status_code == status
        assert "secret key" not in response.text
    async def empty(*args):
        return ""
    app.state.generator = empty
    assert client.post("/api/apply-prompt", json=payload).status_code == 502
    from pif.app import generate
    app.state.generator = generate
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert client.post("/api/apply-prompt", json=payload).status_code == 503
    assert client.get("/api/files").status_code == 200


def test_failed_atomic_write_preserves_original(env, monkeypatch):
    client, vault, _ = env
    (vault / "page.md").write_text("original")
    def fail(*args, **kwargs):
        raise OSError("simulated disk failure")
    monkeypatch.setattr(os, "replace", fail)
    assert client.put("/api/markdown?path=page.md", json={"content": "replacement"}).status_code == 500
    assert (vault / "page.md").read_text() == "original"
    assert not list(vault.glob(".pif-*"))


def test_failed_settings_write_keeps_memory(env, monkeypatch):
    client, _, app = env
    from pif.storage import Problem
    def fail(*args, **kwargs):
        raise Problem(500, "storage_error", "Failed")
    monkeypatch.setattr(app.state.config, "write", fail)
    before = client.get("/api/settings").json()
    assert client.patch("/api/settings", json={"gemini_model": "different"}).status_code == 500
    assert client.get("/api/settings").json() == before
