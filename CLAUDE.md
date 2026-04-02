# Project: Superna Eyeglass DR MCP Server

## GitHub

- **Username:** Andrew-MacKay-CA
- **Email:** andrew.mackay@superna.net
- **Profile:** https://github.com/Andrew-MacKay-CA
- **Repository:** https://github.com/Superna-io/DR-MCP
- **Token:** (set in local git credential store — do not commit)

## Eyeglass Appliance

- **Host:** igls
- **API Token:** igls-1pk0l7fjigubbb3vhg283cujsi48lqmbjip3fguuigimfriff8ne
- **Docs:** https://igls/sera/docs/
- **SSL:** self-signed (verify disabled)

## MCP Server

- **Entry point:** server.py
- **Default transport:** SSE HTTP on port 8000
- **Config file:** superna_mcp.json (read on startup, falls back to env vars)
- **Start command:** `python3 server.py` (SSE default) or `python3 server.py --stdio`
- **SSE endpoint:** http://127.0.0.1:8000/sse

## Project Files

| File | Purpose |
|------|---------|
| `server.py` | MCP server — 34 tools exposing the Eyeglass /sera/ REST API |
| `gui.py` | Windows GUI frontend |
| `superna_mcp.json` | Runtime config (host, token, port) + Claude Desktop MCP entry |
| `requirements-server.txt` | Python deps for server.py |
| `requirements-gui.txt` | Python deps for gui.py |
| `build.bat` | Windows build script |
| `.github/workflows/build-gui.yml` | GitHub Actions — builds SupernaMCP-GUI.exe on tag push |
| `INSTALL.md` | Installation and configuration guide |

## Nodes (live)

| Name | Role | ID |
|------|------|----|
| production | Primary (source) | production_005056b4aa4369988a664106c21f470db369 |
| DR | Target | DR_005056b4793eee9a8a66b60de6bdf9f5d245 |

## SyncIQ Policies

| Policy | Source → Target | Readiness |
|--------|----------------|-----------|
| dfsprod | production → DR | WARNING (SyncIQ policy needs attention) |
| marketing-data | production → DR | WARNING |
| marketing-data_mirror | DR → production | FAILED_OVER |

## Python Dependencies (tested versions)

```
mcp==1.25.0
requests==2.32.5
cryptography==46.0.3
urllib3==2.6.2
```
