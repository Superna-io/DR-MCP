"""
Superna Eyeglass MCP GUI
Launches the MCP server and provides a chat interface using OpenAI or Anthropic LLMs.
"""

import os
import sys
import json
import time
import shutil
import logging
import threading
import subprocess
import asyncio
import textwrap
from pathlib import Path
from datetime import datetime

# ─── Frozen / PyInstaller helpers ─────────────────────────────────────────────

def _is_frozen() -> bool:
    return getattr(sys, 'frozen', False)

def _bundle_dir() -> Path:
    """Directory where PyInstaller extracts bundled data files."""
    if _is_frozen():
        return Path(sys._MEIPASS)
    return Path(__file__).parent

def _find_python() -> str | None:
    """Return a usable Python executable path."""
    if not _is_frozen():
        return sys.executable
    # When frozen sys.executable is the .exe — find real Python in PATH
    for name in ("python", "python3", "py"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _extract_bundled_files():
    """
    When running as a frozen exe, copy server.py and superna_mcp.json
    from the PyInstaller temp bundle to the same folder as the exe.
    server.py is always overwritten (it is code, not user data).
    superna_mcp.json is only copied if it does not already exist so
    user configuration is preserved across upgrades.
    """
    if not _is_frozen():
        return
    exe_dir = Path(sys.executable).parent
    for filename in ("server.py", "superna_mcp.json"):
        src = _bundle_dir() / filename
        dst = exe_dir / filename
        if not src.exists():
            continue
        if filename == "server.py" or not dst.exists():
            shutil.copy2(src, dst)

BUILD = "1.1.0"


def _gui_log_path() -> Path:
    """Same superna_mcp.log used by server.py — one unified log for both processes."""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent / "superna_mcp.log"
    return Path(os.path.abspath(__file__)).parent / "superna_mcp.log"


def _setup_gui_logging() -> logging.Logger:
    log_path = _gui_log_path()
    logger = logging.getLogger("superna_gui")
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-8s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        logger.addHandler(fh)
    return logger


gui_log = _setup_gui_logging()


import customtkinter as ctk
from PIL import Image, ImageTk
import requests

def _load_image(filename: str, size: tuple) -> ctk.CTkImage | None:
    """Load an image from the bundle directory, return CTkImage or None."""
    path = _bundle_dir() / filename
    if not path.exists():
        return None
    try:
        img = Image.open(path)
        return ctk.CTkImage(light_image=img, dark_image=img, size=size)
    except Exception:
        return None

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

# When frozen the exe lives at sys.executable; save config next to it so it persists.
# When running as a script, save alongside the script as before.
def _config_file() -> Path:
    if _is_frozen():
        return Path(sys.executable).parent / "superna_mcp.json"
    return Path(__file__).parent / "superna_mcp.json"

CONFIG_FILE = _config_file()

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
    "server_py_path": str(Path(sys.executable).parent / "server.py") if _is_frozen() else "server.py",
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

        self.title(f"Superna DR MCP Server and Agentic AI Console  v{BUILD}")
        self.geometry("1180x820")
        self.minsize(900, 650)
        self.configure(fg_color=DARK_BG)

        self._build_ui()
        self._set_window_icon()
        self._load_config_into_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI Construction ───────────────────────────────────────────────────────

    def _set_window_icon(self):
        """Set the window/taskbar icon from logo.png via a temp .ico file (Windows compatible)."""
        try:
            icon_path = _bundle_dir() / "logo.png"
            if not icon_path.exists():
                return
            img = Image.open(icon_path).resize((32, 32), Image.LANCZOS)
            import tempfile, os
            tmp = tempfile.NamedTemporaryFile(suffix=".ico", delete=False)
            tmp.close()
            img.save(tmp.name, format="ICO", sizes=[(32, 32), (16, 16)])
            self._ico_path = tmp.name  # keep reference
            self.after(100, lambda: self.iconbitmap(self._ico_path))
        except Exception:
            pass

    def _build_ui(self):
        # ── Top bar ──
        topbar = ctk.CTkFrame(self, fg_color=PANEL_BG, height=52, corner_radius=0)
        topbar.pack(fill="x", side="top")
        topbar.pack_propagate(False)

        # Header: logo icon + title text
        icon_img = _load_image("logo.png", (36, 36))
        if icon_img:
            ctk.CTkLabel(topbar, image=icon_img, text="").pack(side="left", padx=(14, 8), pady=6)

        ctk.CTkLabel(
            topbar, text="Superna DR MCP Server and Agentic AI Console",
            font=("Segoe UI Semibold", 13), text_color=TEXT_PRIMARY
        ).pack(side="left", padx=(0, 8))

        ctk.CTkLabel(
            topbar, text=f"v{BUILD}",
            font=("Segoe UI", 10), text_color=TEXT_MUTED
        ).pack(side="left", padx=(0, 20))

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

        # Install Dependencies button
        btn_row_dep = ctk.CTkFrame(parent, fg_color="transparent")
        btn_row_dep.pack(fill="x", padx=14, pady=(10, 2))
        self.install_btn = ctk.CTkButton(
            btn_row_dep, text="⬇  Install Dependencies",
            fg_color=BORDER, hover_color=ACCENT,
            text_color=TEXT_PRIMARY, font=("Segoe UI Semibold", 12),
            command=self._install_dependencies
        )
        self.install_btn.pack(fill="x")

        # Start / Stop button
        btn_row = ctk.CTkFrame(parent, fg_color="transparent")
        btn_row.pack(fill="x", padx=14, pady=(4, 4))
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
        self.chat_box.tag_config("user_label",   foreground=ACCENT2)
        self.chat_box.tag_config("ai_label",     foreground=ACCENT)
        self.chat_box.tag_config("tool_label",   foreground=WARNING)
        self.chat_box.tag_config("error_label",  foreground=ERROR)
        self.chat_box.tag_config("user_text",    foreground=TEXT_PRIMARY)
        self.chat_box.tag_config("ai_text",      foreground=TEXT_PRIMARY)
        self.chat_box.tag_config("tool_text",    foreground=TEXT_MUTED)
        self.chat_box.tag_config("muted",        foreground=TEXT_MUTED)
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

    def _install_dependencies(self):
        python_exe = _find_python()
        if not python_exe:
            self._append_chat("error_text", "✗ Python not found in PATH.\n")
            return
        self.install_btn.configure(state="disabled", text="⬇  Installing...")
        self._append_chat("muted", "⟳ Installing Python dependencies...\n")
        threading.Thread(target=self._run_pip_install, args=(python_exe,), daemon=True).start()

    def _run_pip_install(self, python_exe: str):
        packages = ["mcp", "requests", "cryptography", "urllib3"]
        try:
            proc = subprocess.Popen(
                [python_exe, "-m", "pip", "install"] + packages,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True
            )
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    self.after(0, lambda l=line: self._append_chat("tool_text", f"  {l}\n"))
            proc.wait()
            if proc.returncode == 0:
                self.after(0, lambda: self._append_chat("ai_text", "✓ Dependencies installed successfully.\n"))
            else:
                self.after(0, lambda: self._append_chat("error_text", "✗ pip install failed — see output above.\n"))
        except Exception as e:
            self.after(0, lambda: self._append_chat("error_text", f"✗ Install error: {e}\n"))
        finally:
            self.after(0, lambda: self.install_btn.configure(state="normal", text="⬇  Install Dependencies"))

    def _start_server(self):
        server_path = self.server_path_var.get().strip()

        # When frozen and no path set, use the bundled server.py
        if not server_path and _is_frozen():
            server_path = str(_bundle_dir() / "server.py")
            self.server_path_var.set(server_path)

        if not server_path:
            self._append_chat("error_text", "✗ No server.py path set.\n")
            return

        server_path = Path(server_path)
        if not server_path.exists():
            self._append_chat("error_text", f"✗ server.py not found at: {server_path}\n")
            return

        python_exe = _find_python()
        if not python_exe:
            self._append_chat("error_text", "✗ Python not found in PATH. Please install Python and ensure it is on your PATH.\n")
            return

        port = int(self.port_var.get() or 8000)
        self._append_chat("muted", f"⟳ Starting MCP server on port {port}...\n")
        gui_log.info("=" * 60)
        gui_log.info("GUI  Starting server  path=%s  port=%s", server_path, port)

        try:
            self.server_process = subprocess.Popen(
                [python_exe, str(server_path), "--sse", "--port", str(port)],
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
            # Check if process already died — capture and show stderr
            if self.server_process and self.server_process.poll() is not None:
                stderr = ""
                try:
                    stderr = self.server_process.stderr.read().decode(errors="replace").strip()
                except Exception:
                    pass
                msg = f"✗ Server process exited unexpectedly.\n"
                if stderr:
                    msg += f"  Error: {stderr}\n"
                    gui_log.error("GUI  Server exited unexpectedly:\n%s", stderr)
                else:
                    msg += "  Check that all Python packages are installed:\n"
                    msg += "  pip install mcp requests cryptography urllib3\n"
                    gui_log.error("GUI  Server exited unexpectedly (no stderr)")
                self.after(0, lambda m=msg: self._append_chat("error_text", m))
                return
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
        gui_log.info("GUI  Server ready")

    def _stop_server(self):
        gui_log.info("GUI  Server stopped by user")
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
        import traceback as _tb
        gui_log.info("GUI  _load_tools starting  url=%s", sse_url)
        try:
            loop = asyncio.new_event_loop()
            # 20-second timeout — prevents permanent hang if SSE response never arrives
            tools = loop.run_until_complete(
                asyncio.wait_for(get_mcp_tools(sse_url), timeout=20.0)
            )
            loop.close()
            self.mcp_tools = tools
            names = [t.name for t in tools]
            gui_log.info("GUI  _load_tools OK  count=%d  tools=%s", len(tools), names)
            if len(tools) == 0:
                gui_log.warning("GUI  _load_tools returned 0 tools — check server registration")
            self.after(0, lambda n=len(tools): self.tools_lbl.configure(
                text=f"✓ {n} tools loaded", text_color=SUCCESS if n > 0 else WARNING
            ))
            self.after(0, lambda n=len(tools): self._append_chat(
                "muted" if n > 0 else "error_text",
                f"✓ {n} MCP tools available\n\n" if n > 0
                else "✗ 0 tools returned — server may not have registered tools correctly.\n\n"
            ))
        except Exception as e:
            gui_log.error("GUI  _load_tools FAILED  %s: %s\n%s",
                          type(e).__name__, e, _tb.format_exc())
            self.after(0, lambda err=f"{type(e).__name__}: {e}":
                       self._append_chat("error_text", f"✗ Tool load error: {err}\n"))

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

        if not self.mcp_tools:
            self._append_chat("error_text",
                              "✗ No MCP tools loaded — the server has not returned any tools.\n"
                              "  Check superna_mcp.log for registration errors.\n\n")
            gui_log.error("GUI  Send blocked — mcp_tools is empty")
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
        gui_log.info("USER  %s", prompt)

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
            gui_log.error("GUI LOOP ERROR  %s: %s", type(e).__name__, e, exc_info=True)
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
        tools_used_total = 0
        first_call = True

        SYSTEM = (
            "You are a Superna Eyeglass DR operations assistant. "
            "You have MCP tools that query the live Eyeglass appliance in real time. "
            "RULES — you must follow these without exception:\n"
            "1. ALWAYS call the appropriate MCP tool(s) before answering any question about system state.\n"
            "2. NEVER answer from training knowledge, guess, estimate, or fabricate any data.\n"
            "3. If a tool returns an error, report the exact error — do not substitute invented data.\n"
            "4. Every factual claim in your answer must come directly from a tool result in this conversation."
        )

        messages = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt}
        ]

        gui_log.info("LOOP START (openai)  prompt=%s", prompt[:200])

        while True:
            # Force tool use on the first call so the LLM cannot answer from training data.
            # After tool results are in, switch to auto so it can give a final answer.
            if tools_schema:
                tc_setting = "required" if first_call else "auto"
            else:
                tc_setting = openai.NOT_GIVEN

            response = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools_schema if tools_schema else openai.NOT_GIVEN,
                tool_choice=tc_setting,
            )
            first_call = False
            msg = response.choices[0].message

            if msg.tool_calls:
                messages.append(msg)
                gui_log.info("LLM called %d tool(s) this turn", len(msg.tool_calls))
                for tc in msg.tool_calls:
                    fn = tc.function.name
                    args = json.loads(tc.function.arguments or "{}")
                    tools_used_total += 1
                    gui_log.info("GUI TOOL CALL  %-38s  args=%s", fn, args)
                    self.after(0, lambda f=fn, a=args: (
                        self._append_chat("tool_label", f"\n⚙  TOOL: {f}\n"),
                        self._append_chat("tool_text", f"   args: {json.dumps(a)}\n")
                    ))
                    try:
                        result = loop.run_until_complete(call_mcp_tool(sse_url, fn, args))
                        gui_log.info("GUI TOOL OK    %-38s  result=%s", fn, result[:500])
                    except Exception as e:
                        gui_log.error("GUI TOOL ERROR %-38s  %s: %s", fn, type(e).__name__, e)
                        result = f"Error: {e}"
                    self.after(0, lambda r=result: self._append_chat("tool_text", f"   -> {r[:300]}\n"))
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result
                    })
            else:
                final = msg.content or ""
                if tools_used_total == 0:
                    gui_log.warning("WARNING: LLM answered without calling ANY tools — response may be fabricated")
                    self.after(0, lambda: self._append_chat(
                        "error_text", "⚠  WARNING: No MCP tools were called — answer may not reflect live data.\n"
                    ))
                gui_log.info("LOOP END (openai)  tools_used=%d  response=%s", tools_used_total, final[:1000])
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
        tools_used_total = 0
        first_call = True

        SYSTEM = (
            "You are a Superna Eyeglass DR operations assistant. "
            "You have MCP tools that query the live Eyeglass appliance in real time. "
            "RULES — you must follow these without exception:\n"
            "1. ALWAYS call the appropriate MCP tool(s) before answering any question about system state.\n"
            "2. NEVER answer from training knowledge, guess, estimate, or fabricate any data.\n"
            "3. If a tool returns an error, report the exact error — do not substitute invented data.\n"
            "4. Every factual claim in your answer must come directly from a tool result in this conversation."
        )

        messages = [{"role": "user", "content": prompt}]

        gui_log.info("LOOP START (anthropic)  prompt=%s", prompt[:200])

        while True:
            kwargs = dict(
                model=model,
                max_tokens=4096,
                system=SYSTEM,
                messages=messages,
            )
            if tools_schema:
                kwargs["tools"] = tools_schema
                # Force tool use on the first call; auto after tool results are in
                kwargs["tool_choice"] = {"type": "any"} if first_call else {"type": "auto"}

            response = client.messages.create(**kwargs)
            first_call = False

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            text_blocks = [b for b in response.content if b.type == "text"]

            if tool_uses:
                messages.append({"role": "assistant", "content": response.content})
                gui_log.info("LLM called %d tool(s) this turn", len(tool_uses))
                tool_results = []
                for tu in tool_uses:
                    fn = tu.name
                    args = tu.input or {}
                    tools_used_total += 1
                    gui_log.info("GUI TOOL CALL  %-38s  args=%s", fn, args)
                    self.after(0, lambda f=fn, a=args: (
                        self._append_chat("tool_label", f"\n⚙  TOOL: {f}\n"),
                        self._append_chat("tool_text", f"   args: {json.dumps(a)}\n")
                    ))
                    try:
                        result = loop.run_until_complete(call_mcp_tool(sse_url, fn, args))
                        gui_log.info("GUI TOOL OK    %-38s  result=%s", fn, result[:500])
                    except Exception as e:
                        gui_log.error("GUI TOOL ERROR %-38s  %s: %s", fn, type(e).__name__, e)
                        result = f"Error: {e}"
                    self.after(0, lambda r=result: self._append_chat("tool_text", f"   -> {r[:300]}\n"))
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": result
                    })
                messages.append({"role": "user", "content": tool_results})
            else:
                final = " ".join(b.text for b in text_blocks)
                if tools_used_total == 0:
                    gui_log.warning("WARNING: LLM answered without calling ANY tools — response may be fabricated")
                    self.after(0, lambda: self._append_chat(
                        "error_text", "⚠  WARNING: No MCP tools were called — answer may not reflect live data.\n"
                    ))
                gui_log.info("LOOP END (anthropic)  tools_used=%d  response=%s", tools_used_total, final[:1000])
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
    _extract_bundled_files()
    app = SupernaMCPApp()
    app.mainloop()
