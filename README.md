# Superna Eyeglass MCP Server & GUI

Natural-language control of Superna Eyeglass DR failover operations via the Model Context Protocol (MCP).

---

## What's included

| File | Purpose |
|------|---------|
| `server.py` | MCP server — exposes 34 Eyeglass API tools |
| `gui.py` | Windows GUI — chat interface with OpenAI or Anthropic LLM |
| `superna_mcp.json` | Shared config file for both server and GUI |
| `requirements-server.txt` | Server dependencies |
| `requirements-gui.txt` | GUI dependencies |
| `build.bat` | Build `SupernaMCP-GUI.exe` locally on Windows |

---

## Download 1 — MCP Server (all platforms)

Requires Python 3.10+.

```bash
pip install -r requirements-server.txt
```

Edit `superna_mcp.json` and set your Eyeglass host and API token:

```json
{
  "eyeglass_host": "your-eyeglass-ip",
  "eyeglass_api_token": "igls-your-token-here",
  "eyeglass_verify_ssl": false,
  "mcp_port": 8000
}
```

Start the server:

```bash
python server.py --sse --port 8000
```

### Use with Claude Desktop

Add to `%APPDATA%\Claude\claude_desktop_config.json` (Windows) or `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

```json
{
  "mcpServers": {
    "eyeglass-failover": {
      "url": "http://127.0.0.1:8000/sse"
    }
  }
}
```

---

## Download 2 — Windows GUI (exe)

👉 **[Download SupernaMCP-GUI.exe from Releases](../../releases/latest)**

1. Download `SupernaMCP-GUI.exe`
2. Place `superna_mcp.json` in the same folder
3. Double-click the exe — no Python install needed
4. Fill in your Eyeglass host, API token, and OpenAI or Anthropic key
5. Click **Start Server**, then type your question

The GUI auto-starts the MCP server, connects to your Eyeglass appliance, and runs a full agentic tool-call loop with your chosen LLM.

---

## Available MCP Tools (34)

- Health check & alarms
- Node / cluster management
- SyncIQ policies, access zones, IP pools
- Failover jobs (v1 + v2)
- DR test mode & rehearsal jobs
- Readiness assessments
- Configuration replication

---

## Configuration (`superna_mcp.json`)

| Field | Description |
|-------|-------------|
| `eyeglass_host` | Hostname or IP of your Eyeglass appliance |
| `eyeglass_api_token` | API token (`igls-...`) from Eyeglass Admin → API Tokens |
| `eyeglass_verify_ssl` | `false` for self-signed certs (default) |
| `mcp_port` | Port for the MCP SSE server (default: 8000) |
| `server_py_path` | Path to `server.py` (GUI uses this to auto-start the server) |
| `openai_api_key` | Your OpenAI API key |
| `anthropic_api_key` | Your Anthropic API key |

---

## Building the exe yourself

On a Windows machine:

```bat
build.bat
```

Output: `dist\SupernaMCP-GUI.exe`

---

## License

Apache 2.0 — see [LICENSE](LICENSE)
