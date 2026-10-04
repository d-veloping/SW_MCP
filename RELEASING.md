# Releasing

SW_MCP is not published to PyPI or the MCP Registry yet. The tag-triggered workflow that upstream
`solidworks-mcp` used for that was removed when development moved to this repository, because it
published under upstream's PyPI name and registry namespace (`io.github.limuzi013/solidworks-mcp`).
The original procedure is in the history (`git show be51883^:RELEASING.md`).

Until a release channel exists, install from this repository:

```powershell
git clone https://github.com/d-veloping/SW_MCP.git solidworks-mcp
cd solidworks-mcp
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

Pin a commit when another project depends on the server; the console script and the package keep the
names `solidworks-mcp` and `solidworks_mcp`.
