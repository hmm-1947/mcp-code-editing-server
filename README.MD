# Cody MCP Server

An MCP server that lets a connected LLM read, edit and run code on this machine.
It exposes a single tool, `run(command)`, which drives the Cody CLI: code
read/search/edit, shell commands, background processes and a browser. Send
`help` for the full command list; `SKILL.md` explains how a client should use it.

```
AI client -> MCP -> run(command) -> cody CLI -> code ops | playwright-cli
```

## Running

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/python server.py                      # streamable-http on 127.0.0.1:8002
venv/bin/python server.py --transport stdio    # for stdio clients
venv/bin/python server.py --port 9000
```

On Windows use `venv\Scripts\python.exe server.py`, or start `mcp_gui.py` to run the
server on port 8001 together with an ngrok tunnel.

Expose it to a remote client with a tunnel (e.g. ngrok) and add the URL to the
client's custom MCP configuration.

## Building the installer

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
venv\Scripts\python.exe packaging\smoke_test.py      # optional check of the built server
```

Needs `ngrok` and `rg` on PATH (or `-NgrokPath` / `-RgPath`) and Inno Setup 6.
Output goes to `packaging\dist_app` and `packaging\installer_output`.
