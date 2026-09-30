"""webnav: MCP server exposing JS/TS/HTML/CSS language-server features (hover,
definition, references, workspace symbol search, diagnostics) plus a
workspace-wide CSS custom-property/selector index (css_var, selector) as
MCP tools.

Multiplexes three Node-based language servers behind one MCP tool set,
routed by file extension: TypeScript 7's native `tsc --lsp --stdio` for
`.js`/`.mjs`/`.cjs` (via `allowJs`) and `.ts`/`.mts`/`.cts` (sent with the
`typescript` languageId), and `vscode-html-language-server` /
`vscode-css-language-server` (from `vscode-langservers-extracted`) for
`.html`/`.css`. Mirrors codenav_mcp's shape and its shared
`mcp_nav_shared.lsp_client.LspClient`; see docs/agent-tooling.md for details.

Run standalone for manual testing:
    uv run python -m webnav_mcp.server
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

import mcp_nav_shared
from mcp.server.mcpserver import Context, MCPServer
from mcp_nav_shared.errors import TOOL_ERRORS, ToolInputError, format_tool_error
from mcp_nav_shared.exclude import is_excluded
from mcp_nav_shared.format import (
	LOCALS_HOLDER_KINDS,
	filter_symbols_by_kind_and_path,
	format_diagnostics,
	format_location,
	format_outline,
	format_references,
	format_references_grouped,
	format_workspace_symbols,
	parse_kind_filter,
	symbol_kind_label,
	uri_to_relative,
)
from mcp_nav_shared.lsp_client import LspClient
from mcp_nav_shared.notices import NoticeBoard, package_source_dirs
from mcp_nav_shared.params import resolve_name_query
from mcp_nav_shared.resolve import resolve_symbol
from mcp_nav_shared.workspace import WorkspaceSelector

import webnav_mcp
from webnav_mcp import web_index
from webnav_mcp.lang_command import resolve_css_command, resolve_html_command, resolve_ts_command
from webnav_mcp.outline_imports import drop_import_symbols


# Which checkout/worktree to navigate is decided per request (see
# `WorkspaceSelector`): the host starts this process once, usually from the
# main checkout, even when the session works in a linked worktree.
_selector = WorkspaceSelector("WEBNAV_MCP_WORKSPACE")
WORKSPACE_ROOT = _selector.base
_workspace_source = _selector.base_source

# One or more `label=relative/path` roots to index separately (see
# web_index.build_workspace_index); e.g. splitting production assets from
# design wireframes. Unset means "index the whole workspace as one root" —
# most projects have no such split and don't need to set this.
_raw_web_roots = os.environ.get("WEBNAV_MCP_ROOTS")
# Comma-separated workspace-relative files/directories of *generated* script
# output (e.g. the JS a TypeScript build emits). They are never eagerly opened,
# are dropped from `search_symbol`, and position tools reject them with a
# pointer to the source — the TS sources are the code the project maintains.
# Unset means nothing is treated as generated. This is navigation-only: the
# CSS/selector index (`WEBNAV_MCP_ROOTS`) still reads such files, since
# emitted JS is where a root's runtime class/id usages live.
_raw_exclude = os.environ.get("WEBNAV_MCP_EXCLUDE", "")

# Derived from the workspace root by `_derive_config` (called below and again
# whenever the workspace switches).
# A malformed WEBNAV_MCP_ROOTS must not crash the server at import (the host
# would only show "server failed to start"): remember the problem and report
# it as tool text from every index-backed tool instead (see `_indexes`).
_WEB_ROOTS_ERROR: str | None = None
WEB_ROOTS: list[tuple[str, Path]] | None = None
GENERATED_PATHS: list[Path] = []
# The same paths, workspace-relative, for labeling index hits as generated.
_GENERATED_RELATIVE: tuple[str, ...] = ()


def _derive_config(root: Path) -> None:
	global WEB_ROOTS, _WEB_ROOTS_ERROR, GENERATED_PATHS, _GENERATED_RELATIVE
	_WEB_ROOTS_ERROR = None
	try:
		WEB_ROOTS = web_index.parse_roots_env(_raw_web_roots, root) if _raw_web_roots else None
	except ValueError as exc:
		WEB_ROOTS = None
		_WEB_ROOTS_ERROR = str(exc)
	GENERATED_PATHS = [(root / part.strip()).resolve() for part in _raw_exclude.split(",") if part.strip()]
	_GENERATED_RELATIVE = tuple(
		str(path.relative_to(root.resolve())).replace("\\", "/")
		for path in GENERATED_PATHS
		if path.is_relative_to(root.resolve())
	)


_derive_config(WORKSPACE_ROOT)

_POSITION_NOTE = (
	"Positions are 1-indexed. `column` is a UTF-16 character offset on the "
	"line (not a visual/display column): a leading tab counts as one "
	"character, so after a single tab the next character starts at column 2."
)

mcp = MCPServer(
	name="webnav",
	instructions=(
		"Code navigation for this project's JS/TS/HTML/CSS, backed by "
		"TypeScript 7 native tsc LSP (JS/TS) and vscode-langservers-extracted "
		"(HTML/CSS). Prefer this over grepping for symbol definitions/usages. "
		"Start with symbol_info (what is X) or outline (what's in this file) for "
		"JS/TS; search_symbol is JS/TS-only (the HTML/CSS language servers don't "
		"implement useful workspace-wide symbol search). The language servers only "
		"see one file at a time, so `--custom-properties` and `#id`/`.class` "
		"selectors can't be cross-referenced across files that way; use "
		"css_var/selector for those instead of hover/definition/references — "
		"references and definition also answer from that same cross-file "
		"index automatically when the position is on one of those tokens in "
		"a .css/.html file. " + _POSITION_NOTE
	),
)

logger = logging.getLogger(__name__)

_JS_EXTENSIONS = {".js", ".mjs", ".cjs"}
_TS_EXTENSIONS = {".ts", ".mts", ".cts"}
# Everything the one TypeScript language-server instance serves.
_SCRIPT_EXTENSIONS = _JS_EXTENSIONS | _TS_EXTENSIONS | {".jsx", ".tsx"}
_SCRIPT_LANGUAGE_IDS = {
	**dict.fromkeys(_JS_EXTENSIONS, "javascript"),
	**dict.fromkeys(_TS_EXTENSIONS, "typescript"),
	".jsx": "javascriptreact",
	".tsx": "typescriptreact",
}
# The TS language server reads these once at startup; a change restarts it.
_TS_CONFIG_NAMES = frozenset({"tsconfig.json", "jsconfig.json", "package.json"})

_notices = NoticeBoard("webnav", package_source_dirs(mcp_nav_shared, webnav_mcp))
_SUPPORTED_EXTENSIONS_TEXT = "/".join([*sorted(_SCRIPT_EXTENSIONS), ".html", ".css"])

_clients: dict[str, LspClient] = {}
# One lock per language server: the TS server's first start opens the whole JS
# project, which must not hold up HTML/CSS calls.
_client_locks: dict[str, asyncio.Lock] = {key: asyncio.Lock() for key in ("ts", "html", "css")}


_workspace_lock = asyncio.Lock()


async def _configure_workspace(root: Path, source: str) -> None:
	"""Re-target the server at `root`: every language server was started for
	the previous tree, and the configured roots/generated paths are relative
	to it."""
	global WORKSPACE_ROOT, _workspace_source
	stale = list(_clients.values())
	_clients.clear()
	for client in stale:
		await _discard_client(client)
	WORKSPACE_ROOT = root
	_workspace_source = source
	_derive_config(root)
	logger.info("workspace: %s (%s)", root, source)


async def _use_workspace(ctx: Context | None) -> None:
	"""Called first by every tool. `ctx` is None only when a tool is invoked
	directly (tests), in which case the current workspace stays as is."""
	if ctx is None:
		return
	async with _workspace_lock:
		selection = await _selector.select(ctx.session)
		if selection.root != WORKSPACE_ROOT:
			await _configure_workspace(selection.root, selection.source)


async def _discard_client(client: LspClient) -> None:
	"""Reap a dead (or replaced) language server so it can't linger as a zombie."""
	try:
		await client.stop()
	except Exception:
		logger.debug("failed to stop stale language server", exc_info=True)


async def _get_client(
	key: str,
	make: Callable[[], LspClient],
	after_start: Callable[[LspClient], Awaitable[None]] | None = None,
) -> LspClient:
	async with _client_locks[key]:
		client = _clients.get(key)
		if client is None or not client.is_alive:
			if client is not None:
				await _discard_client(client)
			client = make()
			await client.start()
			_clients[key] = client
			if after_start is not None:
				await after_start(client)
		# Tell the server about anything created/edited/deleted on disk since the last call.
		await client.refresh()
		return client


def _indexes() -> list[web_index.RootIndex]:
	if _WEB_ROOTS_ERROR is not None:
		raise ToolInputError(f"WEBNAV_MCP_ROOTS is misconfigured: {_WEB_ROOTS_ERROR}")
	return web_index.build_workspace_index(WORKSPACE_ROOT, WEB_ROOTS)


def _js_include_globs(workspace_root: Path) -> list[str]:
	"""Read the `include` globs from jsconfig.json, so webnav's eager-open list
	stays in sync with what the ts-server itself treats as the JS project."""
	try:
		config = json.loads((workspace_root / "jsconfig.json").read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError):
		return []
	return config.get("include", [])


def _js_files_fallback(workspace_root: Path) -> list[Path]:
	"""Scan `WEB_ROOTS` (or the whole workspace) for JS files when there's no
	jsconfig.json to read `include` globs from — otherwise no file ever gets
	eagerly opened and tsserver's `workspace/symbol` reports "No Project" on
	every `search_symbol` call until some other tool happens to open a JS
	file first (see docs/agent-tooling.md)."""
	roots = [root for _, root in WEB_ROOTS] if WEB_ROOTS else [workspace_root]
	files: list[Path] = []
	for root in roots:
		if not root.is_dir():
			continue
		files.extend(
			p
			for p in root.rglob("*")
			if p.is_file() and p.suffix.lower() in _SCRIPT_EXTENSIONS and not is_excluded(p, root)
		)
	return files


async def _open_project_files(client: LspClient) -> None:
	# workspace/symbol is most reliable when project files have been opened;
	# eagerly open the JS/TS project here rather than leaving the first
	# search_symbol call (agents' typical first lookup) to miss files that
	# haven't happened to hover/define/reference first.
	include_globs = _js_include_globs(WORKSPACE_ROOT)
	if include_globs:
		open_paths = [path for glob in include_globs for path in WORKSPACE_ROOT.glob(glob)]
	else:
		# No jsconfig.json (or no `include` key): fall back to scanning
		# for JS files directly rather than opening nothing.
		open_paths = _js_files_fallback(WORKSPACE_ROOT)
	for path in open_paths:
		if _is_generated(path):
			continue
		try:
			await client.ensure_open(str(path))
		except TOOL_ERRORS:
			logger.debug("skipping unreadable project file %s", path, exc_info=True)  # e.g. non-UTF-8


async def _get_ts_client() -> LspClient:
	return await _get_client(
		"ts",
		lambda: LspClient(
			workspace_root=WORKSPACE_ROOT,
			command=resolve_ts_command(WORKSPACE_ROOT),
			language_id="javascript",
			language_ids=_SCRIPT_LANGUAGE_IDS,
			watch_suffixes=frozenset(_SCRIPT_EXTENSIONS),
			watch_ignore=_is_generated,
			open_watched_changes=True,
			config_names=_TS_CONFIG_NAMES,
			on_restart=_open_project_files,
			on_notice=_notices.post,
		),
		_open_project_files,
	)


async def _get_html_client() -> LspClient:
	return await _get_client(
		"html",
		lambda: LspClient(
			workspace_root=WORKSPACE_ROOT, command=resolve_html_command(WORKSPACE_ROOT), language_id="html"
		),
	)


async def _get_css_client() -> LspClient:
	return await _get_client(
		"css",
		lambda: LspClient(
			workspace_root=WORKSPACE_ROOT, command=resolve_css_command(WORKSPACE_ROOT), language_id="css"
		),
	)


async def _client_for(file_path: str) -> LspClient:
	suffix = Path(file_path).suffix.lower()
	if suffix in _SCRIPT_EXTENSIONS:
		if _is_generated(_resolve_path(file_path)):
			raise ToolInputError(
				f"{file_path!r} is generated output (WEBNAV_MCP_EXCLUDE); navigate the source it was built from instead"
			)
		return await _get_ts_client()
	if suffix == ".html":
		return await _get_html_client()
	if suffix == ".css":
		return await _get_css_client()
	raise ToolInputError(f"webnav has no language server for {file_path!r} (supported: {_SUPPORTED_EXTENSIONS_TEXT})")


def _is_generated(path: Path) -> bool:
	resolved = path.resolve()
	return any(resolved == root or root in resolved.parents for root in GENERATED_PATHS)


def _resolve_path(file_path: str) -> Path:
	p = Path(file_path)
	return p if p.is_absolute() else WORKSPACE_ROOT / p


def _index_token_at(file_path: str, line: int, column: int) -> str | None:
	"""The `--var`/`#id`/`.class` token at a position in a `.css`/`.html`
	file, so `references`/`definition` can answer from the cross-file index
	instead of the single-file language server (see module docstring)."""
	if Path(file_path).suffix.lower() not in (".css", ".html"):
		return None
	try:
		text_line = _resolve_path(file_path).read_text(encoding="utf-8").splitlines()[line - 1]
	except (OSError, IndexError, UnicodeDecodeError):
		return None
	token = web_index.token_at_position(text_line, column)
	if token is None or token.startswith("--"):
		return token
	# A `#fff` colour or a `.5em`-like value looks like a selector token but
	# isn't one; only defer to the index for tokens it actually knows.
	return token if web_index.knows_selector(_indexes(), token) else None


def _index_answer(token: str, file_path: str | None = None, *, definitions_only: bool = False) -> str:
	"""Answer from the cross-file index. With `file_path` (a position query), only
	the root that file lives in is reported — each root defines its own values and
	markup, so mixing in the other roots (wireframes vs. production) answers a
	different question — unless that root has nothing for the token."""
	indexes = _indexes()
	scoped = ""
	if file_path is not None:
		own = web_index.root_index_for_file(indexes, _resolve_path(file_path))
		if own is not None:
			own_index = own[0]
			answer = _format_index([own_index], token, definitions_only=definitions_only)
			if "was not found" not in answer and "not defined or used" not in answer:
				return answer
			scoped = f"(nothing in {own_index.name}; showing all roots)\n"
	return scoped + _format_index(indexes, token, definitions_only=definitions_only)


def _format_index(indexes: list[web_index.RootIndex], token: str, *, definitions_only: bool) -> str:
	if token.startswith("--"):
		return web_index.format_css_var(
			indexes, token, generated=_GENERATED_RELATIVE, definitions_only=definitions_only
		)
	return web_index.format_selector(indexes, token, generated=_GENERATED_RELATIVE, definitions_only=definitions_only)


def _format_hover_contents(contents: object) -> str:
	if not contents:
		return ""
	if isinstance(contents, dict):
		return str(contents.get("value", contents)).strip()
	if isinstance(contents, list):
		return "\n".join(c.get("value", str(c)) if isinstance(c, dict) else str(c) for c in contents).strip()
	return str(contents).strip()


def _check_script_file(file_path: str) -> None:
	suffix = Path(file_path).suffix.lower()
	if suffix not in _SCRIPT_EXTENSIONS:
		raise ToolInputError(
			f"webnav outline/symbol_info only support JS/TS files "
			f"({'/'.join(sorted(_SCRIPT_EXTENSIONS))}), got {file_path!r}; "
			"use css_var/selector for CSS/HTML"
		)
	if _is_generated(_resolve_path(file_path)):
		raise ToolInputError(
			f"{file_path!r} is generated output (WEBNAV_MCP_EXCLUDE); navigate the source it was built from instead"
		)


@mcp.tool()
@_notices.tool
async def hover(file_path: str, line: int, column: int, ctx: Context | None = None) -> str:
	"""Get type/documentation info for the symbol at a position.

	`line` and `column` are 1-indexed. `column` is a UTF-16 character offset
	on the line (not a visual/display column): a leading tab counts as one
	character.
	"""
	await _use_workspace(ctx)
	try:
		client = await _client_for(file_path)
		result = await client.hover(file_path, line, column)
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)
	return _format_hover_contents(result.get("contents")) or "No hover information at that position."


@mcp.tool()
@_notices.tool
async def workspace(ctx: Context | None = None) -> str:
	"""Which directory is webnav navigating, and why? Use when results look like they come from the wrong checkout/worktree."""
	await _use_workspace(ctx)
	return f"{WORKSPACE_ROOT}\nchosen because: {_selector.explain(_workspace_source)}"


@mcp.tool()
@_notices.tool
async def definition(file_path: str, line: int, column: int, ctx: Context | None = None) -> str:
	"""Go to the definition of the symbol at a position.

	`line` and `column` are 1-indexed. `column` is a UTF-16 character offset
	on the line (not a visual/display column): a leading tab counts as one
	character. On a `--custom-property`/`#id`/`.class` token in a `.css`/
	`.html` file (or a name inside an HTML `id="..."`/`class="..."` value),
	answers from the cross-file index (see css_var/selector)
	instead of the single-file language server: for `definition`, just the
	definition(s) in the file's own root (each `WEBNAV_MCP_ROOTS` root
	is separate); for `references`, that root's definitions and usages.
	"""
	await _use_workspace(ctx)
	try:
		token = _index_token_at(file_path, line, column)
		if token is not None:
			return _index_answer(token, file_path, definitions_only=True)
		client = await _client_for(file_path)
		locations = await client.definition(file_path, line, column)
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)
	if not locations:
		return "No definition found at that position."
	return "\n\n".join(format_location(loc, WORKSPACE_ROOT) for loc in locations)


@mcp.tool()
@_notices.tool
async def references(
	file_path: str, line: int, column: int, include_declaration: bool = True, ctx: Context | None = None
) -> str:
	"""Find all usages of the symbol at a position across the workspace.

	`line` and `column` are 1-indexed. `column` is a UTF-16 character offset
	on the line (not a visual/display column): a leading tab counts as one
	character. On a `--custom-property`/`#id`/`.class` token in a `.css`/
	`.html` file (or a name inside an HTML `id="..."`/`class="..."` value),
	answers from the cross-file index (see css_var/selector)
	instead of the single-file language server, limited to the file's own root
	(each `WEBNAV_MCP_ROOTS` root is separate) unless it has no hits there.
	"""
	await _use_workspace(ctx)
	try:
		token = _index_token_at(file_path, line, column)
		if token is not None:
			return _index_answer(token, file_path)
		client = await _client_for(file_path)
		locations = await client.references(file_path, line, column, include_declaration=include_declaration)
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)
	return format_references(locations, WORKSPACE_ROOT)


@mcp.tool()
@_notices.tool
async def search_symbol(
	query: str | None = None,
	name: str | None = None,
	kind: str | None = None,
	path: str | None = None,
	fuzzy: bool = False,
	ctx: Context | None = None,
) -> str:
	"""Search JS/TS files for a symbol by name (function, class, const, etc.).

	JS/TS-only: the HTML/CSS language servers don't implement useful
	workspace-wide symbol search (webnav does not reimplement it). Prefer
	`symbol_info` for a one-call summary. Returned positions point at the
	identifier name and use the same character-offset column convention as
	the other tools. Results include a SymbolKind label and are capped.
	`name` is accepted as an alias for `query`. Narrow broad queries with
	`kind` (SymbolKind labels, comma-separated: `class`, `function,method`,
	`interface`, ...) and `path` (workspace-relative prefix such as `src/`,
	or a glob such as `src/**/*.ts`). Production code ranks before tests.
	Loose fuzzy hits whose names don't contain the query are summarised as a
	count when real matches exist; pass `fuzzy=true` to list them too.
	"""
	await _use_workspace(ctx)
	try:
		kinds = parse_kind_filter(kind)
		query = resolve_name_query(preferred="query", example="renderSidebar", query=query, name=name)
		client = await _get_ts_client()
		symbols = await client.workspace_symbol(query)
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)
	symbols = [
		sym
		for sym in symbols
		if not _is_generated(_resolve_path(uri_to_relative(sym.get("location", {}).get("uri", ""), WORKSPACE_ROOT)))
	]
	if not symbols:
		return f"No symbols matching {query!r}."
	matching = filter_symbols_by_kind_and_path(symbols, WORKSPACE_ROOT, kinds=kinds, path=path)
	if not matching:
		filters = ", ".join(f"{k}={v!r}" for k, v in (("kind", kind), ("path", path)) if v)
		return f"No symbols matching {query!r} with {filters} ({len(symbols)} without the filters)."
	return format_workspace_symbols(matching, WORKSPACE_ROOT, query=query, fuzzy=fuzzy)


@mcp.tool()
@_notices.tool
async def symbol_info(
	name: str | None = None,
	query: str | None = None,
	file_path: str | None = None,
	include_references: bool = True,
	ctx: Context | None = None,
) -> str:
	"""What is X and where is it used? Example: `symbol_info(name="renderSidebar")`.

	One-call summary for a JS/TS name: header, hover text, definition, and
	references grouped by file — the usual first lookup instead of chaining
	search_symbol → hover → definition → references by hand. Pass `file_path`
	to disambiguate; `query` is accepted as an alias for `name`. For CSS/HTML
	cross-file lookups use `css_var` / `selector` instead.
	"""
	await _use_workspace(ctx)
	try:
		name = resolve_name_query(preferred="name", example="renderSidebar", name=name, query=query)
		client = await _get_ts_client()
		resolved = await resolve_symbol(client, WORKSPACE_ROOT, name, file_path=file_path)
		rel_path = uri_to_relative(resolved.uri, WORKSPACE_ROOT)
		if _is_generated(_resolve_path(rel_path)):
			raise ToolInputError(
				f"{rel_path!r} is generated output (WEBNAV_MCP_EXCLUDE); navigate the source it was built from instead"
			)
		line, column = resolved.line + 1, resolved.column + 1
		hover_result = await client.hover(rel_path, line, column)
		definition_locations = await client.definition(rel_path, line, column)
		reference_locations = await client.references(rel_path, line, column) if include_references else []
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)
	header = f"{resolved.name}  [{symbol_kind_label(resolved.kind)}]  ({rel_path}:{line}:{column})"
	hover_text = _format_hover_contents(hover_result.get("contents")) or "No hover information."
	definition_text = (
		"\n\n".join(format_location(loc, WORKSPACE_ROOT) for loc in definition_locations)
		if definition_locations
		else "No definition found."
	)
	parts = [header, "", hover_text, "", "Definition:", definition_text]
	if include_references:
		parts += ["", "References:", format_references_grouped(reference_locations, WORKSPACE_ROOT)]
	return "\n".join(parts)


@mcp.tool()
@_notices.tool
async def outline(file_path: str, detailed: bool = False, ctx: Context | None = None) -> str:
	"""What's in this file? Example: `outline(file_path="src/app.ts")`.

	Indented outline (functions, classes, interfaces, with `:start-end` line
	spans) of a JS/TS file in source order, so you can navigate without reading
	it in full. Locals, callbacks and object-literal keys inside functions and
	variables are left out; pass `detailed=true` to include them. Follow up
	with hover/definition/references at a listed line, or symbol_info by name.
	"""
	await _use_workspace(ctx)
	try:
		_check_script_file(file_path)
		client = await _get_ts_client()
		symbols = await client.document_symbol(file_path)
		if not detailed:
			symbols = drop_import_symbols(symbols, _resolve_path(file_path).read_text(encoding="utf-8"))
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)
	return format_outline(symbols, collapse_kinds=frozenset() if detailed else LOCALS_HOLDER_KINDS)


@mcp.tool()
@_notices.tool
async def diagnostics(file_path: str, ctx: Context | None = None) -> str:
	"""Get the relevant language server's diagnostics (errors/warnings) for a single file.

	For `.css`/`.html` files, this also includes index-derived warnings the
	single-file language server can't see: `var(--x)` used with no matching
	declaration anywhere in the same indexed root, and CSS selectors
	(`#id`/`.class`) with no HTML/JS reference in that root.
	"""
	await _use_workspace(ctx)
	try:
		client = await _client_for(file_path)
		items = await client.diagnostics(file_path)
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)
	lines = [format_diagnostics(items)]
	if Path(file_path).suffix.lower() in (".css", ".html"):
		try:
			located = web_index.root_index_for_file(_indexes(), _resolve_path(file_path))
		except ToolInputError as exc:
			lines.append(str(exc))
			located = None
		if located is not None:
			idx, file_rel = located
			extra = web_index.diagnostics_for_file(idx, file_rel)
			if extra:
				lines.append("\n".join(extra))
	combined = [text for text in lines if text and text != "No diagnostics."]
	return "\n".join(combined) if combined else "No diagnostics."


@mcp.tool()
@_notices.tool
async def css_var(name: str | None = None, query: str | None = None, ctx: Context | None = None) -> str:
	"""Where is this `--custom-property` defined and used? Example: `css_var(name="--bg")`.

	The CSS/HTML language servers only see one file at a time, so `var(--x)`
	usages can't be cross-referenced across files that way — this scans
	`.css` files and HTML `<style>`/`style="…"` blocks/attributes instead.
	`name` may be given with or without the leading `--`. Definitions (value
	+ enclosing context, e.g. `@media (prefers-color-scheme: dark) › :root`)
	and usages (grouped by file with line numbers) are reported separately per
	configured root (see `WEBNAV_MCP_ROOTS`; a single unnamed root by default),
	since each may define its own values. `query` is accepted as an alias for `name`.
	"""
	await _use_workspace(ctx)
	try:
		name = resolve_name_query(preferred="name", example="--bg", name=name, query=query)
		indexes = _indexes()
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)
	return web_index.format_css_var(indexes, name, generated=_GENERATED_RELATIVE)


@mcp.tool()
@_notices.tool
async def selector(name: str | None = None, query: str | None = None, ctx: Context | None = None) -> str:
	"""Who uses this `#id` or `.class`? Example: `selector(name=".card-title")`.

	Looks up the selector across the whole workspace.

	Cross-references CSS rule definitions, HTML `id=`/`class=` attributes,
	and JS usages (`getElementById`, `classList.add/remove/toggle/contains`,
	`querySelector`/`querySelectorAll`, `className` assignment, and any JS
	string literal exactly equal to the bare name — e.g. an id passed to a
	project's own helper like `onClick("btn-save", …)`, labeled "string
	literal"). Hits in generated output (`WEBNAV_MCP_EXCLUDE`) are labeled
	`[generated]`; edit their source instead. This is something
	the single-file CSS/HTML language servers can't do. `name` must include
	the leading `#` or `.`. Grouped by file with line numbers, separately per
	configured root (see `WEBNAV_MCP_ROOTS`). A JS hit built from string
	concatenation (e.g. `getElementById("view-" + x)`) is reported against
	only its static prefix and labeled "dynamic partial match"; a query whose
	name starts with such a prefix (e.g. `#view-components` against a stored
	`#view-`) also surfaces that hit, labeled "dynamic partial match via
	'<prefix>'", instead of being silently dropped or guessed. `query` is
	accepted as an alias for `name`.
	"""
	await _use_workspace(ctx)
	try:
		name = resolve_name_query(preferred="name", example=".card-title", name=name, query=query)
		indexes = _indexes()
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)
	return web_index.format_selector(indexes, name, generated=_GENERATED_RELATIVE)


def main() -> None:
	"""Console entry point: serve over stdio."""
	mcp.run(transport="stdio")


if __name__ == "__main__":
	main()
