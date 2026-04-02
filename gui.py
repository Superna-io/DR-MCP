"""
Superna Eyeglass MCP GUI
Launches the MCP server and provides a chat interface using OpenAI or Anthropic LLMs.
"""

import os
import sys
import json
import time
import threading
import subprocess
import asyncio
import textwrap
from pathlib import Path
from datetime import datetime

import customtkinter as ctk
import requests

# ─── Optional LLM SDKs (imported lazily to give clear error messages) ─────────
try:
    import openai
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False

try:
    import anthropic as anthropic_sdk
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

try:
    from mcp.client.sse import sse_client
    from mcp import ClientSession
    HAS_MCP = True
except ImportError:
    HAS_MCP = False

# ─── Config ───────────────────────────────────────────────────────────────────

CONFIG_FILE = Path(__file__).parent / "superna_mcp.json"

DEFAULT_CONFIG = {
    "mcpServers": {
        "eyeglass-failover": {
            "url": "http://127.0.0.1:8000/sse"
        }
    },
    "eyeglass_host": "igls",
    "eyeglass_api_token": "",
    "eyeglass_verify_ssl": False,
    "mcp_port": 8000,
    "server_py_path": "server.py",
    "openai_api_key": "",
    "anthropic_api_key": ""
}


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                data = json.load(f)
            # merge with defaults for any missing keys
            for k, v in DEFAULT_CONFIG.items():
                if k not in data:
                    data[k] = v
            return data
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


# ─── Theme ────────────────────────────────────────────────────────────────────

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

DARK_BG      = "#0d1117"
PANEL_BG     = "#161b22"
BORDER       = "#30363d"
ACCENT       = "#00d4aa"        # Superna teal
ACCENT2      = "#58a6ff"        # blue highlight
TEXT_PRIMARY = "#e6edf3"
TEXT_MUTED   = "#8b949e"
SUCCESS      = "#3fb950"
WARNING      = "#d29922"
ERROR        = "#f85149"
USER_BUBBLE  = "#1c2128"
AI_BUBBLE    = "#161b22"

FONT_MONO    = ("Consolas", 12)
FONT_UI      = ("Segoe UI", 12)
FONT_UI_SM   = ("Segoe UI", 10)
FONT_TITLE   = ("Segoe UI Semibold", 13)
FONT_HEADING = ("Segoe UI Semibold", 11)


# ─── MCP Tool Discovery & Agentic Loop ────────────────────────────────────────

async def get_mcp_tools(sse_url: str) -> list:
    """Fetch tool definitions from the running MCP server."""
    async with sse_client(sse_url) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            result = await session.list_tools()
            return result.tools


async def call_mcp_tool(sse_url: str, tool_name: str, arguments: dict) -> str:
    """Call a single MCP tool and return the text result."""
    async with sse_client(sse_url) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments)
            parts = []
            for c in result.content:
                if hasattr(c, "text"):
                    parts.append(c.text)
            return "\n".join(parts)


def mcp_tools_to_openai_schema(tools) -> list:
    schemas = []
    for t in tools:
        schema = {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description or "",
                "parameters": t.inputSchema if t.inputSchema else {"type": "object", "properties": {}}
            }
        }
        schemas.append(schema)
    return schemas


def mcp_tools_to_anthropic_schema(tools) -> list:
    schemas = []
    for t in tools:
        schema = {
            "name": t.name,
            "description": t.description or "",
            "input_schema": t.inputSchema if t.inputSchema else {"type": "object", "properties": {}}
        }
        schemas.append(schema)
    return schemas


# ─── Main Application ─────────────────────────────────────────────────────────

class SupernaMCPApp(ctk.CTk):

    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.server_process = None
        self.server_running = False
        self.mcp_tools = []
        self._loop = None

        self.title("Superna Eyeglass MCP Console")
        self.geometry("1180x820")
        self.minsize(900, 650)
        self.configure(fg_color=DARK_BG)

        self._build_ui()
        self._load_config_into_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI Construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Top bar ──
        topbar = ctk.CTkFrame(self, fg_color=PANEL_BG, height=52, corner_radius=0)
        topbar.pack(fill="x", side="top")
        topbar.pack_propagate(False)

        logo_lbl = ctk.CTkLabel(
            topbar, text="⬡  SUPERNA EYEGLASS  ·  MCP CONSOLE",
            font=("Consolas", 13, "bold"), text_color=ACCENT
        )
        logo_lbl.pack(side="left", padx=20)

        self.status_dot = ctk.CTkLabel(topbar, text="●", font=("Segoe UI", 16), text_color=ERROR)
        self.status_dot.pack(side="right", padx=(0, 8))
        self.status_lbl = ctk.CTkLabel(topbar, text="Server stopped", font=FONT_UI_SM, text_color=TEXT_MUTED)
        self.status_lbl.pack(side="right", padx=(0, 4))

        # ── Main split ──
        main = ctk.CTkFrame(self, fg_color=DARK_BG)
        main.pack(fill="both", expand=True, padx=0, pady=0)

        # Left sidebar
        sidebar = ctk.CTkFrame(main, fg_color=PANEL_BG, width=300, corner_radius=0)
        sidebar.pack(fill="y", side="left")
        sidebar.pack_propagate(False)
        self._build_sidebar(sidebar)

        # Divider
        div = ctk.CTkFrame(main, fg_color=BORDER, width=1, corner_radius=0)
        div.pack(fill="y", side="left")

        # Right chat area
        chat_area = ctk.CTkFrame(main, fg_color=DARK_BG, corner_radius=0)
        chat_area.pack(fill="both", expand=True, side="left")
        self._build_chat(chat_area)

    def _build_sidebar(self, parent):
        pad = {"padx": 14, "pady": 4}

        # ── Server section ──
        self._section_header(parent, "MCP SERVER")

        self._field_label(parent, "server.py Path")
        path_row = ctk.CTkFrame(parent, fg_color="transparent")
        path_row.pack(fill="x", **pad)
        self.server_path_var = ctk.StringVar()
        self.server_path_entry = ctk.CTkEntry(
            path_row, textvariable=self.server_path_var,
            fg_color=DARK_BG, border_color=BORDER, text_color=TEXT_PRIMARY,
            font=FONT_UI_SM
        )
        self.server_path_entry.pack(side="left", fill="x", expand=True)
        browse_btn = ctk.CTkButton(
            path_row, text="…", width=32, fg_color=BORDER,
            hover_color=ACCENT, text_color=TEXT_PRIMARY, font=FONT_UI_SM,
            command=self._browse_server
        )
        browse_btn.pack(side="left", padx=(4, 0))

        self._field_label(parent, "Eyeglass Host / IP")
        self.host_var = ctk.StringVar()
        ctk.CTkEntry(parent, textvariable=self.host_var,
                     fg_color=DARK_BG, border_color=BORDER, text_color=TEXT_PRIMARY,
                     font=FONT_UI).pack(fill="x", **pad)

        self._field_label(parent, "Eyeglass API Token")
        self.token_var = ctk.StringVar()
        ctk.CTkEntry(parent, textvariable=self.token_var, show="•",
                     fg_color=DARK_BG, border_color=BORDER, text_color=TEXT_PRIMARY,
                     font=FONT_UI).pack(fill="x", **pad)

        ssl_row = ctk.CTkFrame(parent, fg_color="transparent")
        ssl_row.pack(fill="x", padx=14, pady=(6, 2))
        self.ssl_var = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(
            ssl_row, text="Verify SSL", variable=self.ssl_var,
            font=FONT_UI_SM, text_color=TEXT_MUTED,
            progress_color=ACCENT, button_color=TEXT_PRIMARY
        ).pack(side="left")

        self._field_label(parent, "MCP Port")
        self.port_var = ctk.StringVar(value="8000")
        ctk.CTkEntry(parent, textvariable=self.port_var, width=80,
                     fg_color=DARK_BG, border_color=BORDER, text_color=TEXT_PRIMARY,
                     font=FONT_UI).pack(anchor="w", **pad)

        # Start / Stop button
        btn_row = ctk.CTkFrame(parent, fg_color="transparent")
        btn_row.pack(fill="x", padx=14, pady=(10, 4))
        self.start_btn = ctk.CTkButton(
            btn_row, text="▶  Start Server", fg_color=SUCCESS, hover_color="#2ea043",
            text_color=DARK_BG, font=("Segoe UI Semibold", 12),
            command=self._toggle_server
        )
        self.start_btn.pack(fill="x")

        # ── Separator ──
        ctk.CTkFrame(parent, fg_color=BORDER, height=1).pack(fill="x", padx=14, pady=12)

        # ── LLM section ──
        self._section_header(parent, "LLM SETTINGS")

        self._field_label(parent, "Provider")
        self.llm_var = ctk.StringVar(value="anthropic")
        seg = ctk.CTkSegmentedButton(
            parent, values=["anthropic", "openai"],
            variable=self.llm_var,
            fg_color=DARK_BG, selected_color=ACCENT, selected_hover_color=ACCENT,
            unselected_color=DARK_BG, unselected_hover_color=BORDER,
            text_color=TEXT_PRIMARY, font=FONT_UI_SM,
            command=self._on_llm_change
        )
        seg.pack(fill="x", **pad)

        self._field_label(parent, "Anthropic API Key")
        self.anthropic_key_var = ctk.StringVar()
        self.anthropic_key_entry = ctk.CTkEntry(
            parent, textvariable=self.anthropic_key_var, show="•",
            fg_color=DARK_BG, border_color=BORDER, text_color=TEXT_PRIMARY,
            font=FONT_UI
        )
        self.anthropic_key_entry.pack(fill="x", **pad)

        self._field_label(parent, "OpenAI API Key")
        self.openai_key_var = ctk.StringVar()
        self.openai_key_entry = ctk.CTkEntry(
            parent, textvariable=self.openai_key_var, show="•",
            fg_color=DARK_BG, border_color=BORDER, text_color=TEXT_PRIMARY,
            font=FONT_UI
        )
        self.openai_key_entry.pack(fill="x", **pad)

        self._field_label(parent, "Model")
        self.model_var = ctk.StringVar(value="claude-sonnet-4-20250514")
        self.model_entry = ctk.CTkEntry(
            parent, textvariable=self.model_var,
            fg_color=DARK_BG, border_color=BORDER, text_color=TEXT_PRIMARY,
            font=FONT_UI_SM
        )
        self.model_entry.pack(fill="x", **pad)

        ctk.CTkFrame(parent, fg_color=BORDER, height=1).pack(fill="x", padx=14, pady=12)

        # Save config button
        ctk.CTkButton(
            parent, text="💾  Save Config", fg_color=BORDER, hover_color=ACCENT2,
            text_color=TEXT_PRIMARY, font=FONT_UI_SM,
            command=self._save_config
        ).pack(fill="x", padx=14, pady=(0, 4))

        # Tools count label
        self.tools_lbl = ctk.CTkLabel(
            parent, text="No tools loaded", font=FONT_UI_SM, text_color=TEXT_MUTED
        )
        self.tools_lbl.pack(padx=14, pady=4, anchor="w")

    def _build_chat(self, parent):
        # Chat display
        self.chat_box = ctk.CTkTextbox(
            parent, fg_color=DARK_BG, text_color=TEXT_PRIMARY,
            font=FONT_MONO, wrap="word", state="disabled",
            border_width=0, corner_radius=0
        )
        self.chat_box.pack(fill="both", expand=True, padx=0, pady=0)

        # Configure text tags
        self.chat_box.tag_config("user_label",   foreground=ACCENT2,  font=("Consolas", 11, "bold"))
        self.chat_box.tag_config("ai_label",     foreground=ACCENT,   font=("Consolas", 11, "bold"))
        self.chat_box.tag_config("tool_label",   foreground=WARNING,  font=("Consolas", 11, "bold"))
        self.chat_box.tag_config("error_label",  foreground=ERROR,    font=("Consolas", 11, "bold"))
        self.chat_box.tag_config("user_text",    foreground=TEXT_PRIMARY)
        self.chat_box.tag_config("ai_text",      foreground=TEXT_PRIMARY)
        self.chat_box.tag_config("tool_text",    foreground=TEXT_MUTED, font=("Consolas", 11))
        self.chat_box.tag_config("muted",        foreground=TEXT_MUTED, font=("Consolas", 10))
        self.chat_box.tag_config("error_text",   foreground=ERROR)

        # Input area
        input_frame = ctk.CTkFrame(parent, fg_color=PANEL_BG, height=90, corner_radius=0)
        input_frame.pack(fill="x", side="bottom")
        input_frame.pack_propagate(False)

        inner = ctk.CTkFrame(input_frame, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=12, pady=10)

        self.prompt_box = ctk.CTkTextbox(
            inner, fg_color=DARK_BG, text_color=TEXT_PRIMARY,
            font=FONT_UI, border_color=BORDER, border_width=1,
            height=60, wrap="word", corner_radius=6
        )
        self.prompt_box.pack(side="left", fill="both", expand=True)
        self.prompt_box.bind("<Return>", self._on_enter)
        self.prompt_box.bind("<Shift-Return>", lambda e: None)

        btn_col = ctk.CTkFrame(inner, fg_color="transparent", width=90)
        btn_col.pack(side="left", fill="y", padx=(8, 0))
        btn_col.pack_propagate(False)

        self.send_btn = ctk.CTkButton(
            btn_col, text="Send", fg_color=ACCENT, hover_color="#00b894",
            text_color=DARK_BG, font=("Segoe UI Semibold", 12),
            command=self._send_prompt, corner_radius=6
        )
        self.send_btn.pack(fill="x", pady=(0, 4))

        ctk.CTkButton(
            btn_col, text="Clear", fg_color=BORDER, hover_color="#444c56",
            text_color=TEXT_MUTED, font=FONT_UI_SM,
            command=self._clear_chat, corner_radius=6
        ).pack(fill="x")

        self._append_chat("muted", "── Superna Eyeglass MCP Console ──\n")
        self._append_chat("muted", "Configure settings on the left, start the server, then ask questions.\n\n")

    def _section_header(self, parent, text):
        ctk.CTkLabel(
            parent, text=text, font=("Consolas", 10, "bold"),
            text_color=TEXT_MUTED
        ).pack(anchor="w", padx=14, pady=(12, 2))

    def _field_label(self, parent, text):
        ctk.CTkLabel(
            parent, text=text, font=FONT_UI_SM, text_color=TEXT_MUTED
        ).pack(anchor="w", padx=14, pady=(6, 1))

    # ── Config ────────────────────────────────────────────────────────────────

    def _load_config_into_ui(self):
        c = self.cfg
        self.server_path_var.set(c.get("server_py_path", "server.py"))
        self.host_var.set(c.get("eyeglass_host", "igls"))
        self.token_var.set(c.get("eyeglass_api_token", ""))
        self.ssl_var.set(bool(c.get("eyeglass_verify_ssl", False)))
        self.port_var.set(str(c.get("mcp_port", 8000)))
        self.openai_key_var.set(c.get("openai_api_key", ""))
        self.anthropic_key_var.set(c.get("anthropic_api_key", ""))

    def _save_config(self):
        self.cfg["server_py_path"]    = self.server_path_var.get()
        self.cfg["eyeglass_host"]     = self.host_var.get()
        self.cfg["eyeglass_api_token"] = self.token_var.get()
        self.cfg["eyeglass_verify_ssl"] = self.ssl_var.get()
        self.cfg["mcp_port"]          = int(self.port_var.get() or 8000)
        self.cfg["openai_api_key"]    = self.openai_key_var.get()
        self.cfg["anthropic_api_key"] = self.anthropic_key_var.get()
        self.cfg["mcpServers"] = {
            "eyeglass-failover": {
                "url": f"http://127.0.0.1:{self.cfg['mcp_port']}/sse"
            }
        }
        save_config(self.cfg)
        self._append_chat("muted", "✓ Config saved to superna_mcp.json\n")

    def _browse_server(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            title="Select server.py",
            filetypes=[("Python files", "*.py"), ("All files", "*.*")]
        )
        if path:
            self.server_path_var.set(path)

    def _on_llm_change(self, value):
        model_defaults = {
            "anthropic": "claude-sonnet-4-20250514",
            "openai": "gpt-4o"
        }
        self.model_var.set(model_defaults.get(value, ""))

    # ── Server management ─────────────────────────────────────────────────────

    def _toggle_server(self):
        if self.server_running:
            self._stop_server()
        else:
            self._save_config()
            self._start_server()

    def _start_server(self):
        server_path = self.server_path_var.get().strip()
        if not server_path:
            self._append_chat("error_text", "✗ No server.py path set.\n")
            return

        server_path = Path(server_path)
        if not server_path.exists():
            self._append_chat("error_text", f"✗ server.py not found at: {server_path}\n")
            return

        port = int(self.port_var.get() or 8000)
        self._append_chat("muted", f"⟳ Starting MCP server on port {port}...\n")

        try:
            self.server_process = subprocess.Popen(
                [sys.executable, str(server_path), "--sse", "--port", str(port)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=str(server_path.parent)
            )
        except Exception as e:
            self._append_chat("error_text", f"✗ Failed to start server: {e}\n")
            return

        threading.Thread(target=self._wait_for_server, args=(port,), daemon=True).start()

    def _wait_for_server(self, port: int):
        sse_url = f"http://127.0.0.1:{port}/sse"
        for _ in range(30):
            time.sleep(0.5)
            try:
                r = requests.get(f"http://127.0.0.1:{port}/sse", timeout=1, stream=True)
                r.close()
                break
            except Exception:
                pass
        else:
            self.after(0, lambda: self._append_chat("error_text", "✗ Server did not start in time.\n"))
            return

        self.server_running = True
        self.after(0, self._on_server_started)
        # Load tools
        threading.Thread(target=self._load_tools, args=(sse_url,), daemon=True).start()

    def _on_server_started(self):
        self.status_dot.configure(text_color=SUCCESS)
        self.status_lbl.configure(text="Server running")
        self.start_btn.configure(text="■  Stop Server", fg_color=ERROR, hover_color="#b91c1c")
        self._append_chat("muted", "✓ MCP server ready\n")

    def _stop_server(self):
        if self.server_process:
            self.server_process.terminate()
            self.server_process = None
        self.server_running = False
        self.mcp_tools = []
        self.status_dot.configure(text_color=ERROR)
        self.status_lbl.configure(text="Server stopped")
        self.start_btn.configure(text="▶  Start Server", fg_color=SUCCESS, hover_color="#2ea043")
        self.tools_lbl.configure(text="No tools loaded")
        self._append_chat("muted", "■ Server stopped\n")

    def _load_tools(self, sse_url: str):
        try:
            loop = asyncio.new_event_loop()
            tools = loop.run_until_complete(get_mcp_tools(sse_url))
            loop.close()
            self.mcp_tools = tools
            self.after(0, lambda: self.tools_lbl.configure(
                text=f"✓ {len(tools)} tools loaded", text_color=SUCCESS
            ))
            self.after(0, lambda: self._append_chat(
                "muted", f"✓ {len(tools)} MCP tools available\n\n"
            ))
        except Exception as e:
            self.after(0, lambda: self._append_chat("error_text", f"✗ Tool load error: {e}\n"))

    # ── Chat ──────────────────────────────────────────────────────────────────

    def _append_chat(self, tag: str, text: str):
        self.chat_box.configure(state="normal")
        self.chat_box.insert("end", text, tag)
        self.chat_box.configure(state="disabled")
        self.chat_box.see("end")

    def _clear_chat(self):
        self.chat_box.configure(state="normal")
        self.chat_box.delete("1.0", "end")
        self.chat_box.configure(state="disabled")

    def _on_enter(self, event):
        if not event.state & 0x1:  # Shift not held
            self._send_prompt()
            return "break"

    def _send_prompt(self):
        prompt = self.prompt_box.get("1.0", "end").strip()
        if not prompt:
            return
        self.prompt_box.delete("1.0", "end")

        if not self.server_running:
            self._append_chat("error_text", "✗ Start the MCP server first.\n\n")
            return

        provider = self.llm_var.get()
        if provider == "openai" and not self.openai_key_var.get().strip():
            self._append_chat("error_text", "✗ Enter your OpenAI API key.\n\n")
            return
        if provider == "anthropic" and not self.anthropic_key_var.get().strip():
            self._append_chat("error_text", "✗ Enter your Anthropic API key.\n\n")
            return

        ts = datetime.now().strftime("%H:%M:%S")
        self._append_chat("user_label", f"\n[{ts}] YOU\n")
        self._append_chat("user_text", f"{prompt}\n")

        self.send_btn.configure(state="disabled", text="…")
        threading.Thread(target=self._run_agentic_loop, args=(prompt,), daemon=True).start()

    def _run_agentic_loop(self, prompt: str):
        provider = self.llm_var.get()
        try:
            if provider == "openai":
                self._openai_loop(prompt)
            else:
                self._anthropic_loop(prompt)
        except Exception as e:
            self.after(0, lambda: self._append_chat("error_text", f"\n✗ Error: {e}\n\n"))
        finally:
            self.after(0, lambda: self.send_btn.configure(state="normal", text="Send"))

    # ── OpenAI agentic loop ───────────────────────────────────────────────────

    def _openai_loop(self, prompt: str):
        if not HAS_OPENAI:
            self.after(0, lambda: self._append_chat("error_text", "✗ openai package not installed.\n\n"))
            return

        port = int(self.port_var.get() or 8000)
        sse_url = f"http://127.0.0.1:{port}/sse"
        client = openai.OpenAI(api_key=self.openai_key_var.get().strip())
        model = self.model_var.get().strip() or "gpt-4o"

        loop = asyncio.new_event_loop()
        tools_schema = mcp_tools_to_openai_schema(self.mcp_tools)

        messages = [
            {
                "role": "system",
                "content": (
                    "You are an AI assistant with access to the Superna Eyeglass DR failover API. "
                    "Use the available tools to answer questions about DR readiness, nodes, jobs, and alarms. "
                    "Always call relevant tools to get real data rather than guessing."
                )
            },
            {"role": "user", "content": prompt}
        ]

        while True:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools_schema if tools_schema else openai.NOT_GIVEN,
                tool_choice="auto" if tools_schema else openai.NOT_GIVEN,
            )
            msg = response.choices[0].message

            if msg.tool_calls:
                messages.append(msg)
                for tc in msg.tool_calls:
                    fn = tc.function.name
                    args = json.loads(tc.function.arguments or "{}")
                    self.after(0, lambda f=fn, a=args: (
                        self._append_chat("tool_label", f"\n⚙  TOOL: {f}\n"),
                        self._append_chat("tool_text", f"   args: {json.dumps(a)}\n")
                    ))
                    try:
                        result = loop.run_until_complete(call_mcp_tool(sse_url, fn, args))
                    except Exception as e:
                        result = f"Error: {e}"
                    self.after(0, lambda r=result: self._append_chat("tool_text", f"   → {r[:300]}\n"))
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result
                    })
            else:
                final = msg.content or ""
                ts = datetime.now().strftime("%H:%M:%S")
                self.after(0, lambda t=ts, f=final: (
                    self._append_chat("ai_label", f"\n[{t}] EYEGLASS AI\n"),
                    self._append_chat("ai_text", f"{f}\n\n")
                ))
                break

        loop.close()

    # ── Anthropic agentic loop ────────────────────────────────────────────────

    def _anthropic_loop(self, prompt: str):
        if not HAS_ANTHROPIC:
            self.after(0, lambda: self._append_chat("error_text", "✗ anthropic package not installed.\n\n"))
            return

        port = int(self.port_var.get() or 8000)
        sse_url = f"http://127.0.0.1:{port}/sse"
        client = anthropic_sdk.Anthropic(api_key=self.anthropic_key_var.get().strip())
        model = self.model_var.get().strip() or "claude-sonnet-4-20250514"

        loop = asyncio.new_event_loop()
        tools_schema = mcp_tools_to_anthropic_schema(self.mcp_tools)

        system_prompt = (
            "You are an AI assistant with access to the Superna Eyeglass DR failover API. "
            "Use the available tools to answer questions about DR readiness, nodes, jobs, and alarms. "
            "Always call relevant tools to get real data rather than guessing."
        )

        messages = [{"role": "user", "content": prompt}]

        while True:
            kwargs = dict(
                model=model,
                max_tokens=4096,
                system=system_prompt,
                messages=messages,
            )
            if tools_schema:
                kwargs["tools"] = tools_schema

            response = client.messages.create(**kwargs)

            # Check for tool use
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            text_blocks = [b for b in response.content if b.type == "text"]

            if tool_uses:
                # Add assistant message
                messages.append({"role": "assistant", "content": response.content})
                tool_results = []
                for tu in tool_uses:
                    fn = tu.name
                    args = tu.input or {}
                    self.after(0, lambda f=fn, a=args: (
                        self._append_chat("tool_label", f"\n⚙  TOOL: {f}\n"),
                        self._append_chat("tool_text", f"   args: {json.dumps(a)}\n")
                    ))
                    try:
                        result = loop.run_until_complete(call_mcp_tool(sse_url, fn, args))
                    except Exception as e:
                        result = f"Error: {e}"
                    self.after(0, lambda r=result: self._append_chat("tool_text", f"   → {r[:300]}\n"))
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": result
                    })
                messages.append({"role": "user", "content": tool_results})
            else:
                final = " ".join(b.text for b in text_blocks)
                ts = datetime.now().strftime("%H:%M:%S")
                self.after(0, lambda t=ts, f=final: (
                    self._append_chat("ai_label", f"\n[{t}] EYEGLASS AI\n"),
                    self._append_chat("ai_text", f"{f}\n\n")
                ))
                break

        loop.close()

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def _on_close(self):
        self._stop_server()
        self.destroy()


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = SupernaMCPApp()
    app.mainloop()
