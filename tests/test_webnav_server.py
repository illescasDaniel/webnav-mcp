"""Fast unit tests for webnav_mcp.server helpers (no live language servers)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from mcp_nav_shared.errors import ToolInputError

from webnav_mcp import server


def _write(path: Path, text: str = "") -> Path:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")
	return path


def test_given_no_jsconfig_when_js_files_fallback_then_finds_js_files(tmp_path, monkeypatch):
	# given — no jsconfig.json, so `_js_include_globs` returns nothing; the
	# fallback scan should still find the project's own JS files.
	_write(tmp_path / "app.js")
	_write(tmp_path / "lib" / "util.mjs")
	monkeypatch.setattr(server, "WEB_ROOTS", None)
	# when
	files = server._js_files_fallback(tmp_path)
	# then
	assert {p.name for p in files} == {"app.js", "util.mjs"}


def test_given_venv_and_node_modules_when_js_files_fallback_then_excludes_them(tmp_path, monkeypatch):
	# given
	_write(tmp_path / "app.js")
	_write(tmp_path / "node_modules" / "pkg" / "index.js")
	_write(tmp_path / ".venv" / "lib" / "vendored.js")
	monkeypatch.setattr(server, "WEB_ROOTS", None)
	# when
	files = server._js_files_fallback(tmp_path)
	# then
	assert {p.name for p in files} == {"app.js"}


def test_given_configured_web_roots_when_js_files_fallback_then_scans_only_those_roots(tmp_path, monkeypatch):
	# given
	web_root = tmp_path / "web"
	other_root = tmp_path / "other"
	_write(web_root / "app.js")
	_write(other_root / "ignored.js")
	monkeypatch.setattr(server, "WEB_ROOTS", [("web", web_root)])
	# when
	files = server._js_files_fallback(tmp_path)
	# then
	assert {p.name for p in files} == {"app.js"}


@pytest.mark.parametrize("name", ["a.ts", "a.mts", "a.cts", "a.js", "a.tsx", "a.jsx"])
def test_given_script_file_when_client_for_then_routes_to_ts_server(monkeypatch, name):
	# given
	sentinel = object()

	async def _fake_ts_client() -> object:
		return sentinel

	monkeypatch.setattr(server, "_get_ts_client", _fake_ts_client)
	# when
	client = asyncio.run(server._client_for(name))
	# then
	assert client is sentinel


def test_given_ts_and_js_when_language_ids_then_ts_is_typescript():
	# then
	assert server._SCRIPT_LANGUAGE_IDS[".ts"] == "typescript"
	assert server._SCRIPT_LANGUAGE_IDS[".js"] == "javascript"


def test_given_ts_file_when_js_files_fallback_then_includes_ts(tmp_path, monkeypatch):
	# given
	_write(tmp_path / "app.ts")
	monkeypatch.setattr(server, "WEB_ROOTS", [])
	# when
	files = server._js_files_fallback(tmp_path)
	# then
	assert {p.name for p in files} == {"app.ts"}


def test_given_generated_dir_when_client_for_script_then_rejected_with_source_hint(tmp_path, monkeypatch):
	# given
	out = tmp_path / "static" / "js"
	monkeypatch.setattr(server, "WORKSPACE_ROOT", tmp_path)
	monkeypatch.setattr(server, "GENERATED_PATHS", [out.resolve()])
	# when / then
	with pytest.raises(ToolInputError, match="generated output"):
		asyncio.run(server._client_for(str(out / "dom.js")))


def test_given_generated_dir_when_is_generated_then_only_paths_under_it_match(tmp_path, monkeypatch):
	# given
	out = tmp_path / "static" / "js"
	monkeypatch.setattr(server, "GENERATED_PATHS", [out.resolve()])
	# when / then
	assert server._is_generated(out / "dom.js")
	assert server._is_generated(out)
	assert not server._is_generated(tmp_path / "static" / "other.js")
	assert not server._is_generated(tmp_path / "web" / "dom.ts")


def test_given_symbols_in_generated_and_source_when_search_symbol_then_only_source_listed(tmp_path, monkeypatch):
	# given
	out = tmp_path / "static" / "js"
	monkeypatch.setattr(server, "WORKSPACE_ROOT", tmp_path)
	monkeypatch.setattr(server, "GENERATED_PATHS", [out.resolve()])

	def _sym(path: Path) -> dict:
		pos = {"line": 0, "character": 0}
		return {
			"name": "isDesktopShell",
			"kind": 12,
			"location": {"uri": path.as_uri(), "range": {"start": pos, "end": pos}},
		}

	class _FakeClient:
		async def workspace_symbol(self, _query: str) -> list[dict]:
			return [_sym(tmp_path / "web" / "dom.ts"), _sym(out / "dom.js")]

	async def _fake_ts_client() -> _FakeClient:
		return _FakeClient()

	monkeypatch.setattr(server, "_get_ts_client", _fake_ts_client)
	# when
	result = asyncio.run(server.search_symbol("isDesktopShell"))
	# then
	assert "dom.ts" in result
	assert "dom.js" not in result


def test_given_name_alias_when_search_symbol_called_then_behaves_like_query(tmp_path, monkeypatch):
	# given
	seen: list[str] = []

	class _FakeClient:
		async def workspace_symbol(self, query: str) -> list[dict]:
			seen.append(query)
			return []

	async def _fake_ts_client() -> _FakeClient:
		return _FakeClient()

	monkeypatch.setattr(server, "_get_ts_client", _fake_ts_client)
	# when
	result = asyncio.run(server.search_symbol(name="renderGalleryItemStage"))
	# then
	assert seen == ["renderGalleryItemStage"]
	assert "No symbols matching" in result


def test_given_neither_query_nor_name_when_search_symbol_then_returns_actionable_error():
	# when
	result = asyncio.run(server.search_symbol())
	# then
	assert "query" in result
	assert "alias" in result


def test_given_gallery_property_flood_when_search_symbol_then_declarations_visible(tmp_path, monkeypatch):
	# given — tsserver-style flood: many Property assignments + export Variable
	# dupes; after filter, GalleryItem / galleryDateParts must stay in the page
	def _sym(name: str, kind: int, path: Path, line: int = 0) -> dict:
		pos = {"line": line, "character": 0}
		return {
			"name": name,
			"kind": kind,
			"location": {"uri": path.as_uri(), "range": {"start": pos, "end": pos}},
		}

	src = tmp_path / "web" / "src"
	src.mkdir(parents=True)
	state = src / "state.ts"
	timeline = src / "gallery-timeline.ts"
	types = src / "types.ts"
	state.write_text("x\n", encoding="utf-8")
	timeline.write_text("x\n", encoding="utf-8")
	types.write_text("x\n", encoding="utf-8")
	symbols = [_sym("GalleryItem", 11, types)]
	symbols.append(_sym("galleryDateParts", 12, timeline))
	symbols.append(_sym("galleryDateParts", 13, timeline))  # export list Variable
	for line in range(5):
		symbols.append(_sym("galleryHasMore", 7, state, line))
		symbols.append(_sym("galleryHasMore", 7, timeline, line))

	class _FakeClient:
		async def workspace_symbol(self, _query: str) -> list[dict]:
			return symbols

	async def _fake_ts_client() -> _FakeClient:
		return _FakeClient()

	monkeypatch.setattr(server, "WORKSPACE_ROOT", tmp_path)
	monkeypatch.setattr(server, "GENERATED_PATHS", [])
	monkeypatch.setattr(server, "_get_ts_client", _fake_ts_client)
	# when
	result = asyncio.run(server.search_symbol("gallery"))
	# then
	assert "GalleryItem" in result
	assert "galleryDateParts" in result
	assert result.count("galleryHasMore") == 2  # one per file, not five each
	assert result.count("[Variable]") == 0


def test_given_query_alias_when_symbol_info_called_then_resolves_name(monkeypatch):
	# given
	seen: list[str] = []

	class _Resolved:
		name = "renderGalleryItemStage"
		kind = 12
		uri = "file:///web/src/gallery-item.ts"
		line = 134
		column = 9

	async def _fake_resolve(client, workspace, name, file_path=None):
		seen.append(name)
		return _Resolved()

	class _FakeClient:
		async def hover(self, *_a, **_k):
			return {"contents": {"value": "fn"}}

		async def definition(self, *_a, **_k):
			return []

		async def references(self, *_a, **_k):
			return []

	async def _fake_ts_client() -> _FakeClient:
		return _FakeClient()

	monkeypatch.setattr(server, "_get_ts_client", _fake_ts_client)
	monkeypatch.setattr(server, "resolve_symbol", _fake_resolve)
	monkeypatch.setattr(server, "uri_to_relative", lambda *_: "web/src/gallery-item.ts")
	monkeypatch.setattr(server, "_is_generated", lambda *_: False)
	# when
	result = asyncio.run(server.symbol_info(query="renderGalleryItemStage", include_references=False))
	# then
	assert seen == ["renderGalleryItemStage"]
	assert "renderGalleryItemStage" in result
	assert "Definition:" in result


def test_given_css_file_when_outline_then_rejects_with_hint():
	# when
	result = asyncio.run(server.outline("theme.css"))
	# then
	assert "JS/TS" in result
	assert "css_var" in result or "selector" in result


def test_given_query_alias_when_selector_called_then_behaves_like_name(monkeypatch):
	# given
	seen: list[str] = []
	monkeypatch.setattr(server.web_index, "build_workspace_index", lambda *_: [])
	monkeypatch.setattr(server.web_index, "format_selector", lambda _idx, name, **_kw: seen.append(name) or "ok")
	# when
	result = asyncio.run(server.selector(query=".thumb-removing"))
	# then
	assert result == "ok"
	assert seen == [".thumb-removing"]


def test_given_query_alias_when_css_var_called_then_behaves_like_name(monkeypatch):
	# given
	seen: list[str] = []
	monkeypatch.setattr(server.web_index, "build_workspace_index", lambda *_: [])
	monkeypatch.setattr(server.web_index, "format_css_var", lambda _idx, name, **_kw: seen.append(name) or "ok")
	# when
	result = asyncio.run(server.css_var(query="--bg"))
	# then
	assert result == "ok"
	assert seen == ["--bg"]


def test_given_neither_name_nor_query_when_selector_called_then_returns_actionable_error():
	# when
	result = asyncio.run(server.selector())
	# then
	assert "name" in result


class _FakeClient:
	def __init__(self, alive: bool = True, items: list | None = None) -> None:
		self._alive = alive
		self.stopped = False
		self._items = items or []

	@property
	def is_alive(self) -> bool:
		return self._alive

	async def start(self) -> None:
		pass

	async def stop(self) -> None:
		self.stopped = True

	async def refresh(self) -> None:
		pass

	async def diagnostics(self, _file_path: str) -> list:
		return self._items


def test_given_hex_colour_in_css_when_index_token_at_then_not_treated_as_selector(tmp_path, monkeypatch):
	# given
	_write(tmp_path / "a.css", ".box { color: #fff; }\n")
	monkeypatch.setattr(server, "WORKSPACE_ROOT", tmp_path)
	monkeypatch.setattr(server, "WEB_ROOTS", None)
	# when
	token = server._index_token_at(str(tmp_path / "a.css"), 1, 16)
	# then
	assert token is None


def test_given_known_class_in_css_when_index_token_at_then_returns_token(tmp_path, monkeypatch):
	# given
	_write(tmp_path / "a.css", ".box { color: red; }\n")
	monkeypatch.setattr(server, "WORKSPACE_ROOT", tmp_path)
	monkeypatch.setattr(server, "WEB_ROOTS", None)
	# when
	found = server._index_token_at(str(tmp_path / "a.css"), 1, 3)
	# then
	assert found == ".box"


def test_given_non_utf8_file_when_index_token_at_then_none(tmp_path, monkeypatch):
	# given
	(tmp_path / "a.css").write_bytes(b".box { content: '\xff\xfe'; }\n")
	monkeypatch.setattr(server, "WORKSPACE_ROOT", tmp_path)
	# when
	token = server._index_token_at(str(tmp_path / "a.css"), 1, 3)
	# then
	assert token is None


def test_given_malformed_roots_when_selector_called_then_returns_tool_text(monkeypatch):
	# given
	monkeypatch.setattr(server, "_WEB_ROOTS_ERROR", "invalid WEBNAV_MCP_ROOTS entry 'x'")
	# when
	result = asyncio.run(server.selector(name=".a"))
	# then
	assert "WEBNAV_MCP_ROOTS" in result


def test_given_multi_root_when_diagnostics_then_index_warnings_appear(tmp_path, monkeypatch):
	# given
	_write(tmp_path / "wireframes" / "a.css", ".x { color: var(--nope); }\n")
	monkeypatch.setattr(server, "WORKSPACE_ROOT", tmp_path)
	monkeypatch.setattr(server, "WEB_ROOTS", [("wf", tmp_path / "wireframes")])

	async def _fake_client_for(_path: str) -> _FakeClient:
		return _FakeClient()

	monkeypatch.setattr(server, "_client_for", _fake_client_for)
	# when
	result = asyncio.run(server.diagnostics("wireframes/a.css"))
	# then
	assert "--nope" in result


def test_given_dead_client_when_get_client_then_old_one_stopped_and_replaced(monkeypatch):
	# given
	dead = _FakeClient(alive=False)
	fresh = _FakeClient()
	monkeypatch.setattr(server, "_clients", {"ts": dead})
	# when
	got = asyncio.run(server._get_client("ts", lambda: fresh))  # type: ignore[arg-type,return-value]
	# then
	assert got is fresh
	assert dead.stopped


@pytest.fixture
def two_roots(tmp_path, monkeypatch):
	_write(
		tmp_path / "static" / "theme.css",
		":root { --bg: #fff; }\n.card { color: red; }\nbody { background: var(--bg); }\n",
	)
	_write(tmp_path / "static" / "page.html", '<div id="hero" class="card"></div>\n')
	_write(
		tmp_path / "wire" / "app.html",
		'<style>:root { --bg: #000; }\n.card { color: blue; }</style>\n<div class="card"></div>\n',
	)
	monkeypatch.setattr(server, "WORKSPACE_ROOT", tmp_path)
	monkeypatch.setattr(server, "WEB_ROOTS", [("static", tmp_path / "static"), ("wire", tmp_path / "wire")])
	monkeypatch.setattr(server, "GENERATED_PATHS", [])
	monkeypatch.setattr(server, "_GENERATED_RELATIVE", ())
	return tmp_path


def test_given_var_in_production_css_when_definition_then_only_own_root_definitions(two_roots):
	# when — cursor on `--bg` in `var(--bg)` (line 3, col 24)
	text = asyncio.run(server.definition(str(two_roots / "static" / "theme.css"), 3, 24))
	# then
	assert "== static ==" in text and "#fff" in text
	assert "wire" not in text and "#000" not in text
	assert "Usages" not in text


def test_given_var_in_production_css_when_references_then_own_root_defs_and_usages_only(two_roots):
	# when
	text = asyncio.run(server.references(str(two_roots / "static" / "theme.css"), 3, 24))
	# then
	assert "== static ==" in text and "Usages" in text
	assert "wire" not in text


def test_given_class_in_wireframe_when_definition_then_wireframe_rule_only(two_roots):
	# when — cursor on `.card` in the wireframe's <style> (line 2)
	text = asyncio.run(server.definition(str(two_roots / "wire" / "app.html"), 2, 3))
	# then
	assert "== wire ==" in text and "== static ==" not in text
	assert "CSS" in text and "HTML" not in text


def test_given_id_in_html_when_definition_then_markup_attribute_reported(two_roots):
	# when — `#hero` only exists as an id attribute in static/page.html; query from a CSS file that mentions it
	_write(two_roots / "static" / "extra.css", "#hero { margin: 0; }\n")
	text = asyncio.run(server.definition(str(two_roots / "static" / "extra.css"), 1, 3))
	# then
	assert "HTML (1)" in text and "page.html: L1" in text


def test_given_token_absent_from_own_root_when_definition_then_falls_back_to_all_roots_with_note(two_roots):
	# given — `.only-wire` exists in the wireframe root only, but is queried from production CSS
	_write(two_roots / "wire" / "extra.html", '<p class="only-wire"></p>\n<style>.only-wire { top: 0; }</style>\n')
	_write(two_roots / "static" / "uses.css", "/* .only-wire */ .x { top: 0; }\n")
	# when — cursor on `.only-wire` inside the comment (col 5)
	text = asyncio.run(server.definition(str(two_roots / "static" / "uses.css"), 1, 5))
	# then
	assert "nothing in static; showing all roots" in text and "== wire ==" in text


def test_given_tools_when_listed_then_ctx_is_not_a_parameter():
	# given / when: the notice decorator must not hide the `Context` parameter from the framework
	tools = asyncio.run(server.mcp.list_tools())
	# then
	assert {t.name for t in tools} >= {"hover", "symbol_info", "outline", "selector", "css_var"}
	for tool in tools:
		assert "ctx" not in tool.input_schema.get("properties", {}), tool.name


def test_given_ts_client_when_built_then_config_change_restarts_and_reopens_project(monkeypatch):
	# given
	made: list = []

	async def _capture(_key, make, after_start=None):
		made.append(make())
		return made[-1]

	monkeypatch.setattr(server, "_get_client", _capture)
	# when
	client = asyncio.run(server._get_ts_client())
	# then
	assert client.config_names == frozenset({"tsconfig.json", "jsconfig.json", "package.json"})
	assert client.on_restart is server._open_project_files
	assert client.on_notice == server._notices.post
