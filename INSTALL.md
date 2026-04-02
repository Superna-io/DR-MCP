# Superna Eyeglass Failover MCP Server — Installation & Configuration Guide

## Overview

This MCP (Model Context Protocol) server exposes the Superna Eyeglass REST API
as a set of tools that an AI assistant (Claude) can call. It enables natural-language
control of DR failover operations — listing nodes, checking readiness, launching
failover jobs, DR tests, rehearsals, and more.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Your Machine                                  │
│                                                                      │
│  ┌──────────────────┐   MCP Protocol    ┌────────────────────────┐  │
│  │                  │  (JSON-RPC 2.0)   │                        │  │
│  │  Claude Desktop  │◄─────────────────►│  Eyeglass Failover     │  │
│  │  (MCP Client)    │   SSE transport   │  MCP Server            │  │
│  │                  │   port 8000       │  server.py             │  │
│  └──────────────────┘                   └───────────┬────────────┘  │
│                                                     │               │
└─────────────────────────────────────────────────────┼───────────────┘
                                                      │ HTTPS
                                                      │ port 443
                                                      │ api_key header
                                                      ▼
                                         ┌────────────────────────┐
                                         │   Superna Eyeglass     │
                                         │   Appliance            │
                                         │   https://igls         │
                                         │                        │
                                         │  ┌──────────────────┐  │
                                         │  │  REST API        │  │
                                         │  │  /sera/v1/...    │  │
                                         │  │  /sera/v2/...    │  │
                                         │  └──────────────────┘  │
                                         │                        │
                                         │  ┌──────────────────┐  │
                                         │  │ PowerScale /     │  │
                                         │  │ Isilon Clusters  │  │
                                         │  │  (production)    │  │
                                         │  │  (DR)            │  │
                                         │  └──────────────────┘  │
                                         └────────────────────────┘
```

### Communication Flow

```
Claude Desktop                MCP Server (server.py)         Eyeglass REST API
      │                               │                              │
      │  1. list_tools()              │                              │
      │──────────────────────────────►│                              │
      │◄──────────────────────────────│                              │
      │  [34 tools returned]          │                              │
      │                               │                              │
      │  2. call_tool("list_nodes")   │                              │
      │──────────────────────────────►│                              │
      │                               │  GET /sera/v1/nodes          │
      │                               │  api_key: igls-...           │
      │                               │─────────────────────────────►│
      │                               │◄─────────────────────────────│
      │                               │  200 OK [{id, name, ip}]     │
      │◄──────────────────────────────│                              │
      │  [nodes as TextContent]       │                              │
```

### Transport Modes

| Mode | Usage | Port |
|------|-------|------|
| **SSE** (Server-Sent Events) | Standalone persistent service, Claude Desktop via URL | `8000` (configurable) |
| **stdio** | Claude Desktop spawns the process directly | n/a |

---

## Prerequisites

### Python

- **Minimum:** Python 3.10
- **Recommended:** Python 3.11 or newer
- Tested on: Python 3.14.2

Verify your version:
```bash
python3 --version
```

### Network Access

| From | To | Port | Protocol | Purpose |
|------|----|------|----------|---------|
| MCP Server host | Eyeglass appliance (`igls`) | 443 | HTTPS | REST API calls |
| MCP Client (Claude Desktop) | MCP Server | 8000 | HTTP SSE | MCP protocol (SSE mode) |
| Claude Desktop | MCP Server | stdio | stdin/stdout | MCP protocol (stdio mode) |

> **Note:** The Eyeglass appliance uses a self-signed TLS certificate by default.
> The server runs with SSL verification disabled (`EYEGLASS_VERIFY_SSL=false`)
> unless you install the appliance CA cert and set the variable to `true`.

### Eyeglass API Token

You need an API token from the Eyeglass appliance. To create or retrieve one:

1. Log in to `https://<eyeglass-host>/eyeglass/`
2. Navigate to **Admin → API Tokens**
3. Copy the token — it will be in the format `igls-<alphanumeric string>`

---

## Installation

### 1. Clone / copy the server file

```bash
mkdir -p ~/mcp-servers/eyeglass-failover
cp server.py ~/mcp-servers/eyeglass-failover/
cd ~/mcp-servers/eyeglass-failover
```

### 2. (Recommended) Create a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate        # macOS / Linux
# .venv\Scripts\activate         # Windows
```

### 3. Install dependencies

```bash
pip install mcp>=1.25.0 requests>=2.32.0 cryptography>=42.0.0 urllib3>=2.0.0
```

Or pin exact tested versions:

```bash
pip install \
  "mcp==1.25.0" \
  "requests==2.32.5" \
  "cryptography==46.0.3" \
  "urllib3==2.6.2"
```

To save a `requirements.txt` for repeatability:

```bash
pip freeze > requirements.txt
# Later: pip install -r requirements.txt
```

---

## Configuration

The server is configured entirely through environment variables — no config file needed.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `EYEGLASS_HOST` | Yes | `igls` | Hostname or IP of the Eyeglass appliance |
| `EYEGLASS_API_TOKEN` | Yes | _(empty)_ | API token (`igls-...`) |
| `EYEGLASS_VERIFY_SSL` | No | `false` | Set to `true` to verify the TLS certificate |

---

## Running the Server

### Option A — SSE mode (recommended for persistent use)

SSE mode starts an HTTP server that MCP clients connect to over the network.

```bash
export EYEGLASS_HOST=igls
export EYEGLASS_API_TOKEN=igls-<your-token-here>
export EYEGLASS_VERIFY_SSL=false

python3 server.py --sse --port 8000
```

Expected output:
```
INFO:     Started server process [12345]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
```

The SSE endpoint is available at: `http://127.0.0.1:8000/sse`

To expose it beyond localhost, change the server's `host` binding in `server.py`:
```python
mcp.settings.host = "0.0.0.0"   # listen on all interfaces
```

### Option B — stdio mode (Claude Desktop spawns it)

In stdio mode, Claude Desktop manages the process lifecycle.
Do **not** start the server manually in this case — Claude Desktop handles it.

---

## Claude Desktop Integration

### Option A — SSE (connect to running server)

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "eyeglass-failover": {
      "url": "http://127.0.0.1:8000/sse"
    }
  }
}
```

### Option B — stdio (Claude Desktop manages the process)

```json
{
  "mcpServers": {
    "eyeglass-failover": {
      "command": "python3",
      "args": ["/full/path/to/server.py"],
      "env": {
        "EYEGLASS_HOST": "igls",
        "EYEGLASS_API_TOKEN": "igls-<your-token-here>",
        "EYEGLASS_VERIFY_SSL": "false"
      }
    }
  }
}
```

> If using a virtual environment, replace `python3` with the full path to the
> venv interpreter, e.g. `/full/path/to/.venv/bin/python3`.

Restart Claude Desktop after editing the config. You should see
**"eyeglass-failover"** appear in the MCP tools panel.

---

## Verifying the Installation

### Test the API token directly

```bash
curl -sk -H "api_key: igls-<your-token>" https://igls/sera/v1/nodes
```

Expected: a JSON array of managed cluster nodes.

### Test the MCP server via protocol

```python
import asyncio, json
from mcp.client.sse import sse_client
from mcp import ClientSession

async def test():
    async with sse_client("http://127.0.0.1:8000/sse") as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            tools = await session.list_tools()
            print(f"Connected — {len(tools.tools)} tools available")
            nodes = await session.call_tool("list_nodes", {})
            for c in nodes.content:
                print(json.loads(c.text))

asyncio.run(test())
```

Expected output:
```
Connected — 34 tools available
{'id': 'production_...', 'ip': '...', 'name': 'production'}
{'id': 'DR_...',         'ip': '...', 'name': 'DR'}
```

---

## Available Tools (34)

### Health & Alarms
| Tool | Description |
|------|-------------|
| `health_check` | Check Eyeglass appliance health |
| `list_active_alarms` | List all active alarms |
| `list_historical_alarms` | List resolved/historical alarms |

### Nodes (v1)
| Tool | Description |
|------|-------------|
| `list_nodes` | List all managed PowerScale/Isilon clusters |
| `get_node` | Get details for a specific node |
| `list_node_policies` | List SyncIQ policies (optionally with readiness) |
| `get_node_policy` | Get a specific SyncIQ policy by name |
| `list_node_zones` | List access zones (optionally with readiness) |
| `get_node_zone` | Get a specific access zone |
| `list_node_pools` | List IP pools (optionally with readiness) |
| `get_node_pool` | Get a specific IP pool |

### Failover Jobs (v1 — legacy)
| Tool | Description |
|------|-------------|
| `list_failover_jobs_v1` | List failover jobs |
| `create_failover_job_v1` | Launch a failover job |
| `get_failover_job_v1` | Get job status by ID |
| `cancel_failover_job_v1` | Cancel a running job |
| `get_failover_job_log_v1` | Retrieve job log |
| `dr_test_mode_v1` | Enter / exit DR test mode |
| `create_rehearsal_job_v1` | Start / end a rehearsal |

### Failover Jobs (v2 — recommended)
| Tool | Description |
|------|-------------|
| `list_failover_jobs_v2` | List failover jobs |
| `create_failover_job_v2` | Launch a failover job |
| `get_failover_job_v2` | Get job status by ID |
| `cancel_failover_job_v2` | Cancel a running job |
| `get_failover_job_log_v2` | Retrieve job log |
| `dr_test_mode_v2` | Enter / exit DR test mode |
| `create_rehearsal_job_v2` | Start / end a rehearsal |

### Readiness (v2)
| Tool | Description |
|------|-------------|
| `list_readiness_jobs` | List recent readiness assessments |
| `run_readiness_job` | Run an immediate readiness check |
| `get_readiness_job` | Get readiness job results |

### Replication (v2)
| Tool | Description |
|------|-------------|
| `list_replication_jobs` | List config replication jobs |
| `run_replication_job` | Run a config replication job |
| `get_replication_job` | Get replication job results |

### Config Replication per Node (v2)
| Tool | Description |
|------|-------------|
| `list_node_configrep_jobs` | List config rep jobs for a node |
| `get_node_configrep_job` | Get a specific config rep job |
| `update_node_configrep_job` | Enable/disable or change job type |

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| `403 Forbidden` on API calls | Invalid or missing API token | Check `EYEGLASS_API_TOKEN` |
| `SSL: CERTIFICATE_VERIFY_FAILED` | Self-signed cert | Set `EYEGLASS_VERIFY_SSL=false` |
| `Connection refused` on port 8000 | Server not started | Run `python3 server.py --sse --port 8000` |
| Tools show in list but calls fail | Wrong node ID format | Use IDs from `list_nodes`, not names |
| Failover blocked | `blockonwarnings=True` + warnings exist | Fix underlying SyncIQ issues first |
| `ModuleNotFoundError: mcp` | Dependency not installed | Run `pip install mcp requests cryptography` |
