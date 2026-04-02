@echo off
echo =============================================
echo  Superna Eyeglass MCP GUI - Windows Builder
echo =============================================

:: Install dependencies
echo Installing dependencies...
pip install -r requirements-gui.txt
pip install pyinstaller

:: Build the exe
echo Building SupernaMCP-GUI.exe ...
pyinstaller ^
  --onefile ^
  --windowed ^
  --name SupernaMCP-GUI ^
  --add-data "superna_mcp.json;." ^
  --hidden-import customtkinter ^
  --hidden-import openai ^
  --hidden-import anthropic ^
  --hidden-import mcp ^
  --hidden-import mcp.client.sse ^
  --hidden-import requests ^
  --collect-all customtkinter ^
  gui.py

echo.
echo Done! Executable is in: dist\SupernaMCP-GUI.exe
echo Copy superna_mcp.json into the same folder as the exe before distributing.
pause
