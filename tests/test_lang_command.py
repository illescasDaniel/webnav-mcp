"""Tests for TypeScript 7 `tsc` LSP and Windows-aware npm `.bin` resolution."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from webnav_mcp.lang_command import resolve_css_command, resolve_html_command, resolve_ts_command


def _touch(path: Path) -> Path:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text("", encoding="utf-8")
	return path


def _write_typescript_pkg(root: Path, version: str) -> Path:
	"""Install a fake `typescript` package with a `.bin/tsc` shim under `root`."""
	pkg_dir = root / "node_modules" / "typescript"
	pkg_dir.mkdir(parents=True, exist_ok=True)
	(pkg_dir / "package.json").write_text(
		json.dumps({"name": "typescript", "version": version, "bin": {"tsc": "./bin/tsc"}}),
		encoding="utf-8",
	)
	bin_src = _touch(pkg_dir / "bin" / "tsc")
	shim = root / "node_modules" / ".bin" / "tsc"
	shim.parent.mkdir(parents=True, exist_ok=True)
	if shim.exists() or shim.is_symlink():
		shim.unlink()
	shim.symlink_to(bin_src)
	return shim


def test_prefers_cmd_shim_on_windows_for_html(tmp_path: Path) -> None:
	shim = _touch(tmp_path / "node_modules" / ".bin" / "vscode-html-language-server")
	cmd = _touch(tmp_path / "node_modules" / ".bin" / "vscode-html-language-server.cmd")
	with patch("webnav_mcp.lang_command.sys.platform", "win32"):
		resolved = resolve_html_command(tmp_path)
	assert resolved == [str(cmd), "--stdio"]
	assert resolved[0] != str(shim)


def test_uses_extensionless_shim_on_posix_for_html(tmp_path: Path) -> None:
	shim = _touch(tmp_path / "node_modules" / ".bin" / "vscode-html-language-server")
	_touch(tmp_path / "node_modules" / ".bin" / "vscode-html-language-server.cmd")
	with patch("webnav_mcp.lang_command.sys.platform", "linux"):
		resolved = resolve_html_command(tmp_path)
	assert resolved == [str(shim), "--stdio"]


def test_resolve_ts_prefers_workspace_typescript7(tmp_path: Path) -> None:
	shim = _write_typescript_pkg(tmp_path, "7.0.2")
	with patch("webnav_mcp.lang_command.sys.platform", "linux"):
		resolved = resolve_ts_command(tmp_path)
	assert resolved == [str(shim), "--lsp", "--stdio"]


def test_resolve_ts_skips_typescript5_workspace(tmp_path: Path) -> None:
	_write_typescript_pkg(tmp_path, "5.9.3")
	with (
		patch("webnav_mcp.lang_command.sys.platform", "linux"),
		patch("webnav_mcp.lang_command._WEBNAV_PKG_ROOT", tmp_path / "empty-webnav"),
		patch(
			"webnav_mcp.lang_command.shutil.which",
			side_effect=lambda name: {
				"tsc": None,
				"npx": "/usr/bin/npx",
			}.get(name),
		),
	):
		resolved = resolve_ts_command(tmp_path)
	assert resolved == [
		"/usr/bin/npx",
		"--yes",
		"-p",
		"typescript@7",
		"tsc",
		"--lsp",
		"--stdio",
	]


def test_resolve_ts_falls_back_to_webnav_package_install(tmp_path: Path) -> None:
	webnav_root = tmp_path / "webnav"
	shim = _write_typescript_pkg(webnav_root, "7.0.2")
	workspace = tmp_path / "project"
	workspace.mkdir()
	with (
		patch("webnav_mcp.lang_command.sys.platform", "linux"),
		patch("webnav_mcp.lang_command._WEBNAV_PKG_ROOT", webnav_root),
	):
		resolved = resolve_ts_command(workspace)
	assert resolved == [str(shim), "--lsp", "--stdio"]


def test_resolve_ts_falls_back_to_npx(tmp_path: Path) -> None:
	with (
		patch("webnav_mcp.lang_command.sys.platform", "win32"),
		patch("webnav_mcp.lang_command._WEBNAV_PKG_ROOT", tmp_path / "empty-webnav"),
		patch(
			"webnav_mcp.lang_command.shutil.which",
			side_effect=lambda name: {
				"tsc": None,
				"npx": r"C:\Program Files\nodejs\npx.cmd",
			}.get(name),
		),
	):
		resolved = resolve_ts_command(tmp_path)
	assert resolved == [
		r"C:\Program Files\nodejs\npx.cmd",
		"--yes",
		"-p",
		"typescript@7",
		"tsc",
		"--lsp",
		"--stdio",
	]


@pytest.mark.parametrize(
	("resolver", "bin_name"),
	[
		(resolve_html_command, "vscode-html-language-server"),
		(resolve_css_command, "vscode-css-language-server"),
	],
)
def test_html_css_also_prefer_cmd(tmp_path: Path, resolver, bin_name: str) -> None:
	cmd = _touch(tmp_path / "node_modules" / ".bin" / f"{bin_name}.cmd")
	_touch(tmp_path / "node_modules" / ".bin" / bin_name)
	with patch("webnav_mcp.lang_command.sys.platform", "win32"):
		assert resolver(tmp_path) == [str(cmd), "--stdio"]
