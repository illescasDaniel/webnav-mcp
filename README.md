# webnav-mcp

> **⚠️ Deprecated — do not use this package.** Use
> [`webnav-ts-mcp`](https://www.npmjs.com/package/webnav-ts-mcp) from the npm
> registry instead (`claude mcp add webnav -- npx webnav-ts-mcp`). It is a
> TypeScript port with the same tools and environment variables, needs only
> Node.js (no Python or `uv`), and is the only version that will receive updates.

An MCP server that gives AI agents JS/TS/HTML/CSS navigation, plus a
cross-file index of CSS custom properties and `#id`/`.class` selectors that
single-file language servers can't provide. It's the front-end counterpart to
[`codenav-mcp`](https://pypi.org/project/codenav-mcp/).

## Quick start

You need [uv](https://docs.astral.sh/uv/) (or `pipx`) and Node.js ≥ 18:

```bash
uvx webnav-mcp
```

Register it with your MCP host. For Claude Code, from the project root:

```bash
claude mcp add webnav -- uvx webnav-mcp
```

Or add it to a project `.mcp.json` (Claude Code) or `.cursor/mcp.json` (Cursor):

```json
{
	"mcpServers": {
		"webnav": {
			"command": "uvx",
			"args": ["webnav-mcp"]
		}
	}
}
```

In Cursor, also set `"env": {"WEBNAV_MCP_WORKSPACE": "${workspaceFolder}"}`,
because Cursor may start MCP servers with your home directory as the working
directory.

## Language servers

Requests are routed to three Node language servers by file extension:

| Extension | Backend |
|-----------|---------|
| `.js` / `.mjs` / `.cjs` / `.jsx` / `.ts` / `.mts` / `.cts` / `.tsx` | TypeScript 7 native LSP: `tsc --lsp --stdio` (JS via `allowJs` / `jsconfig.json`) |
| `.html` | `vscode-html-language-server` |
| `.css` | `vscode-css-language-server` |

**Resolution order** for `tsc` / HTML / CSS binaries: navigated project's
`node_modules/.bin/` → webnav's own package-local install (see Development) →
`PATH` → `npx --yes` (JS/TS: `npx -p typescript@7 tsc --lsp --stdio`). webnav
does **not** use `typescript-language-server` — TypeScript 7 no longer ships
classic `tsserver.js`.

To skip downloads, install in the project (or under this package for
standalone):

```bash
npm install --save-dev typescript@^7 vscode-langservers-extracted
```

## Tools

**For JS/TS, start with the name-based tools:**

| Tool | Answers |
|------|---------|
| `symbol_info` | What is this? Header, hover, definition and references in one call |
| `outline` | What's in this file? (source order; locals left out unless `detailed=true`) |
| `search_symbol` | JS/TS workspace symbol search (ranked, capped; optional `kind=` / `path=` filters; fuzzy-only hits summarised unless `fuzzy=true`) |
| `workspace` | Which directory is being navigated, and why |

Then use the position tools once you have a `path:line:col`:

| Tool | Answers |
|------|---------|
| `hover` | Type and docs at a position |
| `definition` | Go to definition (CSS/HTML tokens answer from the index below) |
| `references` | All usages (CSS/HTML tokens answer from the index below) |
| `diagnostics` | Language-server diagnostics; CSS/HTML also get unreferenced-selector and undefined-variable warnings |

**Cross-file CSS/HTML index** (a Python scanner, not the language servers):

| Tool | Answers |
|------|---------|
| `css_var` | Where is `--name` defined and used? |
| `selector` | Where is `#id` or `.class` defined and used (CSS, HTML, JS)? |

`search_symbol`, `symbol_info` and `outline` are **JS/TS only**. Use
`css_var` and `selector` for markup and stylesheets.

`name` and `query` are accepted as aliases of each other on the name-based
tools. A missing parameter gets a short hint back instead of a validation
error.

Positions are **1-indexed**. `column` is a UTF-16 character offset (a leading
tab counts as one character).

## Environment

| Variable | Default | Purpose |
|----------|---------|---------|
| `WEBNAV_MCP_WORKSPACE` | unset: follows the client's MCP roots when they name a worktree of the same git repository, else `CLAUDE_PROJECT_DIR`, else the working directory | Pins the project root (never overridden). See the `workspace` tool |
| `WEBNAV_MCP_ROOTS` | the whole workspace as one root, labelled `web` | Comma-separated `label=relative/path` pairs to index separately, e.g. `app=src,prototypes=design` when two trees define their own values |
| `WEBNAV_MCP_EXCLUDE` | nothing | Comma-separated workspace-relative paths of generated script output (e.g. the JS a TS build emits). These aren't opened, are hidden from `search_symbol`, and are rejected by the position tools. The CSS/selector index still reads them |
| `WEBNAV_MCP_PUBLIC` | nothing | Comma-separated workspace-relative stylesheets (files or directories) that are a public API, e.g. a design-token file consumed by other projects. `diagnostics` stops reporting their custom properties as "declared but never used" and their selectors as "never referenced"; undefined `var()` usages are still reported |

## Requirements

- Python ≥ 3.11
- Node.js ≥ 18 (for TypeScript 7's `tsc` shim and the HTML/CSS servers)
- Installed automatically: [`mcp`](https://pypi.org/project/mcp/),
  [`mcp-nav-shared`](https://pypi.org/project/mcp-nav-shared/)

## Related

- Design notes (index heuristics, dynamic selectors, positioning):
  [docs/agent-tooling.md](https://github.com/illescasDaniel/SpaceMaker/blob/main/docs/agent-tooling.md).

## Development

Source: [github.com/illescasDaniel/webnav-mcp](https://github.com/illescasDaniel/webnav-mcp). From a
checkout: `uv sync --group dev`, then `npm ci` in this directory (owns
`typescript@^7` + `vscode-langservers-extracted` for standalone launch), then
`uv run webnav-mcp`.

## License

MIT. See [LICENSE](https://github.com/illescasDaniel/webnav-mcp/blob/main/LICENSE).
