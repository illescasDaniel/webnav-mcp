"""Live smoke: TypeScript 7 native LSP (`tsc --lsp --stdio`) serving `.ts` files.

Skipped when no TypeScript 7 `tsc` is resolvable (`npm ci` in the repo root). Marked integration like codenav's ty smoke so fast
unit runs can exclude it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from mcp_nav_shared.lsp_client import LspClient

from webnav_mcp.lang_command import _is_typescript7_tsc, resolve_ts_command
from webnav_mcp.server import _SCRIPT_LANGUAGE_IDS


pytestmark = pytest.mark.integration

# tests/test_ts_smoke.py -> repo root (owns node_modules).
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _ts7_available() -> bool:
	cmd = resolve_ts_command(_REPO_ROOT)
	# npx fallback always counts as available; local/PATH only when it's TS7.
	base = Path(cmd[0]).name.lower()
	if base in {"npx", "npx.cmd"}:
		return True
	bin_path = Path(cmd[0])
	return bin_path.is_file() and _is_typescript7_tsc(bin_path) and "--lsp" in cmd


_TS_SERVER_AVAILABLE = _ts7_available()


def _client(workspace: Path) -> LspClient:
	return LspClient(
		workspace_root=workspace,
		command=resolve_ts_command(_REPO_ROOT),
		language_id="javascript",
		language_ids=_SCRIPT_LANGUAGE_IDS,
	)


@pytest.mark.skipif(not _TS_SERVER_AVAILABLE, reason="TypeScript 7 tsc not installed (npm ci)")
def test_given_ts_type_error_when_diagnostics_then_first_call_reports_it(tmp_path):
	# given — native LSP supports pull diagnostics; LspClient uses that path
	bad = tmp_path / "bad.ts"
	bad.write_text('export const n: number = "str";\n', encoding="utf-8")
	good = tmp_path / "good.ts"
	good.write_text("export const ok: number = 1;\n", encoding="utf-8")
	client = _client(tmp_path)

	async def _run() -> None:
		await client.start()
		try:
			# when
			bad_items = await client.diagnostics(str(bad))
			good_items = await client.diagnostics(str(good))
			# then
			assert any(item.get("code") == 2322 for item in bad_items), bad_items
			assert good_items == []
		finally:
			await client.stop()

	asyncio.run(_run())


@pytest.mark.skipif(not _TS_SERVER_AVAILABLE, reason="TypeScript 7 tsc not installed (npm ci)")
def test_given_edited_ts_file_when_diagnostics_then_reflects_new_content(tmp_path):
	# given
	src = tmp_path / "edit.ts"
	src.write_text("export const n: number = 1;\n", encoding="utf-8")
	client = _client(tmp_path)

	async def _run() -> None:
		await client.start()
		try:
			assert await client.diagnostics(str(src)) == []
			# when — introduce an error (different size, so ensure_open re-syncs)
			src.write_text('export const n: number = "now a string";\n', encoding="utf-8")
			items = await client.diagnostics(str(src))
			# then
			assert any(item.get("code") == 2322 for item in items), items
		finally:
			await client.stop()

	asyncio.run(_run())


@pytest.mark.skipif(not _TS_SERVER_AVAILABLE, reason="TypeScript 7 tsc not installed (npm ci)")
def test_given_ts_files_when_hover_and_references_then_typed_and_cross_file(tmp_path):
	# given
	lib = tmp_path / "lib.ts"
	lib.write_text("export function greet(name: string): string {\n\treturn name;\n}\n", encoding="utf-8")
	use = tmp_path / "use.ts"
	use.write_text('import { greet } from "./lib.ts";\n\ngreet("x");\n', encoding="utf-8")
	client = _client(tmp_path)

	async def _run() -> None:
		await client.start()
		try:
			await client.ensure_open(str(use))
			# when
			hover = await client.hover(str(lib), 1, 18)
			refs = await client.references(str(lib), 1, 18)
			# then
			assert "greet(name: string): string" in str(hover)
			assert any(str(r["uri"]).endswith("use.ts") for r in refs), refs
		finally:
			await client.stop()

	asyncio.run(_run())


@pytest.mark.skipif(not _TS_SERVER_AVAILABLE, reason="TypeScript 7 tsc not installed (npm ci)")
def test_given_tsconfig_edited_when_refresh_then_server_restarts_and_uses_new_options(tmp_path):
	# given — `null` is assignable to `string` until `strict` is switched on
	tsconfig = tmp_path / "tsconfig.json"
	tsconfig.write_text('{"compilerOptions": {"strict": false}, "include": ["*.ts"]}\n', encoding="utf-8")
	src = tmp_path / "s.ts"
	src.write_text("export const s: string = null;\n", encoding="utf-8")
	notices: list[str] = []
	reopened: list[LspClient] = []

	async def _on_restart(c: LspClient) -> None:
		reopened.append(c)
		await c.ensure_open(str(src))

	client = LspClient(
		workspace_root=tmp_path,
		command=resolve_ts_command(_REPO_ROOT),
		language_id="javascript",
		language_ids=_SCRIPT_LANGUAGE_IDS,
		watch_suffixes=frozenset({".ts"}),
		open_watched_changes=True,
		config_names=frozenset({"tsconfig.json"}),
		on_restart=_on_restart,
		on_notice=notices.append,
	)

	async def _run() -> None:
		await client.start()
		try:
			await client.refresh()
			assert await client.diagnostics(str(src)) == []
			# when
			tsconfig.write_text('{"compilerOptions": {"strict": true}, "include": ["*.ts"]}\n', encoding="utf-8")
			await client.refresh()
			items = await client.diagnostics(str(src))
			# then
			assert any(item.get("code") == 2322 for item in items), items
			assert notices == ["restarted the language server because tsconfig.json changed"]
			assert reopened == [client]
		finally:
			await client.stop()

	asyncio.run(_run())
