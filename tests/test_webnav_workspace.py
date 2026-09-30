"""Workspace (checkout/worktree) selection for the webnav server."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
from mcp import Client, types
from mcp_nav_shared.workspace import WorkspaceSelector

from webnav_mcp import server


def _repo_with_worktree(tmp_path: Path) -> tuple[Path, Path]:
	main = tmp_path / "main"
	main.mkdir()
	linked = tmp_path / "linked"
	for args in (
		["init", "-q", "-b", "trunk"],
		["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "i"],
		["worktree", "add", "-q", "-b", "feature", str(linked)],
	):
		subprocess.run(["git", *args], cwd=main, check=True, capture_output=True)  # noqa: S603, S607
	return main.resolve(), linked.resolve()


@pytest.fixture(autouse=True)
def _restore_derived_config(monkeypatch):
	# `_configure_workspace` rewrites these; register them so teardown restores the originals.
	for name in ("WEB_ROOTS", "GENERATED_PATHS", "_GENERATED_RELATIVE", "_WEB_ROOTS_ERROR", "_workspace_source"):
		monkeypatch.setattr(server, name, getattr(server, name))


class _Running:
	def __init__(self) -> None:
		self.stopped = False

	async def stop(self) -> None:
		self.stopped = True


def test_given_client_reports_worktree_root_when_tool_called_then_server_switches_workspace(tmp_path, monkeypatch):
	# given — the host started the server in the main checkout, the session works in a worktree
	main, linked = _repo_with_worktree(tmp_path)
	(linked / "web").mkdir()
	monkeypatch.delenv("WEBNAV_MCP_WORKSPACE", raising=False)
	monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(main))
	monkeypatch.setattr(server, "_selector", WorkspaceSelector("WEBNAV_MCP_WORKSPACE"))
	monkeypatch.setattr(server, "WORKSPACE_ROOT", main)
	monkeypatch.setattr(server, "_raw_web_roots", "web=web")
	monkeypatch.setattr(server, "_clients", {})

	async def _roots(_context: object) -> types.ListRootsResult:
		return types.ListRootsResult(roots=[types.Root(uri=linked.as_uri())])

	async def _ask() -> str:
		async with Client(server.mcp, list_roots_callback=_roots, mode="legacy") as client:
			result = await client.call_tool("workspace", {})
		return str(result.content[0].model_dump()["text"])

	# when
	text = asyncio.run(_ask())
	# then
	assert str(linked) in text
	assert "client roots" in text
	assert linked == server.WORKSPACE_ROOT
	assert server.WEB_ROOTS == [("web", linked / "web")]


def test_given_workspace_switch_when_configure_then_language_servers_stopped_and_config_rederived(
	tmp_path, monkeypatch
):
	# given
	old, new = tmp_path / "old", tmp_path / "new"
	running = _Running()
	monkeypatch.setattr(server, "_clients", {"ts": running})
	monkeypatch.setattr(server, "WORKSPACE_ROOT", old)
	monkeypatch.setattr(server, "_raw_web_roots", "web=src")
	monkeypatch.setattr(server, "_raw_exclude", "src/out")
	# when
	asyncio.run(server._configure_workspace(new, "client roots"))
	# then
	assert running.stopped
	assert server._clients == {}
	assert new == server.WORKSPACE_ROOT
	assert server.WEB_ROOTS == [("web", new / "src")]
	assert server.GENERATED_PATHS == [(new / "src" / "out").resolve()]
	assert server._GENERATED_RELATIVE == ("src/out",)
