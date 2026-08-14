<#
.SYNOPSIS
    Copy the plugin sources into Krita's pykrita folder and hot-reload them.

.DESCRIPTION
    Development helper. Krita only imports plugins at startup, but the bridge
    resolves operations through the module at call time, so reloading
    krita_mcp.ops picks up edits without restarting Krita.

    Changes to extension.py, httpserver.py or mainthread.py still need a
    restart -- those objects are already constructed.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$py = if ($env:KRITA_MCP_PYTHON) { $env:KRITA_MCP_PYTHON }
      elseif (Get-Command py -ErrorAction SilentlyContinue) { 'py' }
      elseif (Get-Command python -ErrorAction SilentlyContinue) { 'python' }
      else { throw 'No Python found. Set $env:KRITA_MCP_PYTHON to its path.' }

$source     = Join-Path $PSScriptRoot 'pykrita\krita_mcp'
$target     = Join-Path $env:APPDATA 'krita\pykrita\krita_mcp'
$mcpServer  = Join-Path $PSScriptRoot 'mcp_server.py'

Copy-Item (Join-Path $source '*.py') $target -Force
Get-ChildItem $target -Filter '__pycache__' -Recurse -Directory -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force

$code = @'
import importlib
import krita_mcp.imaging, krita_mcp.ops
importlib.reload(krita_mcp.imaging)
importlib.reload(krita_mcp.ops)
result = {"operations": len(krita_mcp.ops.OPS),
          "plugin_version": krita_mcp.ops.PLUGIN_VERSION}
'@

$paramsFile = Join-Path ([System.IO.Path]::GetTempPath()) 'krita_mcp_reload.json'
@{ code = $code } | ConvertTo-Json -Compress | Set-Content -Path $paramsFile -Encoding utf8
try {
    & $py $mcpServer --call run_python --params-file $paramsFile
} finally {
    Remove-Item $paramsFile -ErrorAction SilentlyContinue
}
