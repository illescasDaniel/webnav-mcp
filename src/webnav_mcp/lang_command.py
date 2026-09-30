"""Resolves how to launch the Node-based language servers webnav multiplexes to.

Same fallback chain as codenav's `resolve_ty_command`: a locally installed
binary first (here, npm's `node_modules/.bin/`), then PATH, then a
package-manager-mediated run as a last resort.

JS/TS uses TypeScript 7's native LSP (`tsc --lsp --stdio`). webnav owns a
package-local `typescript@^7` install under this package's root so standalone
launch works without the host project's `node_modules`.
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
from pathlib import Path


logger = logging.getLogger(__name__)

# lang_command.py -> webnav_mcp (src) -> src -> package root (repo checkout).
_WEBNAV_PKG_ROOT = Path(__file__).resolve().parents[2]

_TS7_NPX_ARGS = ["-p", "typescript@7", "tsc", "--lsp"]


def _local_bin_candidates(workspace_root: Path, bin_name: str) -> list[Path]:
	"""Ordered candidates under `node_modules/.bin` for this platform.

	On Windows, npm writes an extensionless POSIX shim *and* a `.cmd` launcher.
	`CreateProcess` cannot run the shim (WinError 193), so prefer `.cmd`.
	"""
	bin_dir = workspace_root / "node_modules" / ".bin"
	if sys.platform == "win32":
		return [bin_dir / f"{bin_name}.cmd", bin_dir / bin_name]
	return [bin_dir / bin_name]


def _typescript_package_json(tsc_bin: Path) -> Path | None:
	"""Locate the main `typescript` package.json for a `tsc` binary, if any."""
	candidates: list[Path] = []
	# npm shim: <root>/node_modules/.bin/tsc → sibling typescript/
	if tsc_bin.parent.name == ".bin":
		candidates.append(tsc_bin.parent.parent / "typescript" / "package.json")
	try:
		resolved = tsc_bin.resolve()
	except OSError:
		resolved = tsc_bin
	# .../node_modules/typescript/bin/tsc or .../@typescript/typescript-*/lib/tsc
	for parent in resolved.parents:
		pkg = parent / "package.json"
		if pkg.is_file():
			candidates.append(pkg)
		# platform package lives under node_modules/@typescript/...; main package is adjacent
		if parent.name.startswith("typescript-") and parent.parent.name == "@typescript":
			candidates.append(parent.parent.parent / "typescript" / "package.json")
	seen: set[Path] = set()
	for pkg in candidates:
		if pkg in seen or not pkg.is_file():
			continue
		seen.add(pkg)
		try:
			data = json.loads(pkg.read_text(encoding="utf-8"))
		except (OSError, json.JSONDecodeError):
			continue
		if data.get("name") == "typescript":
			return pkg
	return None


def _is_typescript7_tsc(tsc_bin: Path) -> bool:
	"""True when this `tsc` comes from a TypeScript 7+ install (native LSP)."""
	pkg_json = _typescript_package_json(tsc_bin)
	if pkg_json is None:
		return False
	try:
		data = json.loads(pkg_json.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError):
		return False
	version = str(data.get("version") or "")
	major_s = version.split(".", 1)[0]
	return major_s.isdigit() and int(major_s) >= 7


def _resolve_bin(workspace_root: Path, bin_name: str, npx_args: list[str]) -> list[str]:
	for candidate in _local_bin_candidates(workspace_root, bin_name):
		if candidate.is_file():
			return [str(candidate), "--stdio"]
	on_path = shutil.which(bin_name)
	if on_path:
		return [on_path, "--stdio"]
	logger.warning(
		"%s not found locally (node_modules/.bin) or on PATH; falling back to "
		"'npx --yes %s', which downloads it on first use.",
		bin_name,
		" ".join(npx_args),
	)
	# Bare "npx" fails under CreateProcess on Windows (npx is npx.cmd / npx.ps1).
	npx = shutil.which("npx") or "npx"
	return [npx, "--yes", *npx_args, "--stdio"]


def _tsc_search_roots(workspace_root: Path) -> list[Path]:
	"""Where to look for a TypeScript 7 `tsc` binary.

	1. Navigated workspace (host project after `npm ci`)
	2. webnav's own package-local install (standalone launch)
	"""
	roots = [workspace_root]
	if _WEBNAV_PKG_ROOT.resolve() != workspace_root.resolve():
		roots.append(_WEBNAV_PKG_ROOT)
	return roots


def resolve_ts_command(workspace_root: Path) -> list[str]:
	"""Launch TypeScript 7's native language server: `tsc --lsp --stdio`."""
	for root in _tsc_search_roots(workspace_root):
		for candidate in _local_bin_candidates(root, "tsc"):
			if candidate.is_file() and _is_typescript7_tsc(candidate):
				return [str(candidate), "--lsp", "--stdio"]
	on_path = shutil.which("tsc")
	if on_path:
		path = Path(on_path)
		if _is_typescript7_tsc(path):
			return [str(path), "--lsp", "--stdio"]
	logger.warning(
		"TypeScript 7 tsc not found in workspace, webnav package, or PATH; "
		"falling back to 'npx --yes -p typescript@7 tsc --lsp --stdio'.",
	)
	npx = shutil.which("npx") or "npx"
	return [npx, "--yes", *_TS7_NPX_ARGS, "--stdio"]


def resolve_html_command(workspace_root: Path) -> list[str]:
	# Prefer workspace, then webnav's own tooling install (standalone).
	for root in _tsc_search_roots(workspace_root):
		for candidate in _local_bin_candidates(root, "vscode-html-language-server"):
			if candidate.is_file():
				return [str(candidate), "--stdio"]
	return _resolve_bin(
		workspace_root,
		"vscode-html-language-server",
		["--package=vscode-langservers-extracted", "vscode-html-language-server"],
	)


def resolve_css_command(workspace_root: Path) -> list[str]:
	for root in _tsc_search_roots(workspace_root):
		for candidate in _local_bin_candidates(root, "vscode-css-language-server"):
			if candidate.is_file():
				return [str(candidate), "--stdio"]
	return _resolve_bin(
		workspace_root,
		"vscode-css-language-server",
		["--package=vscode-langservers-extracted", "vscode-css-language-server"],
	)
