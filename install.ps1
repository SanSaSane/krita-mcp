<#
.SYNOPSIS
    Installs the Krita MCP bridge plugin and enables it.

.DESCRIPTION
    Copies pykrita\krita_mcp into Krita's resource folder and switches the
    plugin on in kritarc. Safe to re-run: it overwrites the plugin files and
    leaves every other setting alone.

    Krita rewrites kritarc when it exits, so close Krita before running this,
    or the enable flag may be lost.

.PARAMETER Force
    Enable the plugin even if Krita is currently running.
#>
[CmdletBinding()]
param(
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

$source      = Join-Path $PSScriptRoot 'pykrita'
$pykritaDir  = Join-Path $env:APPDATA 'krita\pykrita'
$kritarc     = Join-Path $env:LOCALAPPDATA 'kritarc'
$pluginName  = 'krita_mcp'

# Krita on Windows keeps resources under %APPDATA%\krita but its config under
# %LOCALAPPDATA%\kritarc. Both are wrong often enough to be worth stating.

if (-not (Test-Path $source)) {
    throw "Cannot find $source - run this script from inside the krita-mcp folder."
}

# ---- copy the plugin -------------------------------------------------------
if (-not (Test-Path $pykritaDir)) {
    New-Item -ItemType Directory -Force -Path $pykritaDir | Out-Null
}

$target = Join-Path $pykritaDir $pluginName
if (Test-Path $target) {
    Remove-Item $target -Recurse -Force
}
Copy-Item (Join-Path $source $pluginName) $pykritaDir -Recurse -Force
Copy-Item (Join-Path $source "$pluginName.desktop") $pykritaDir -Force

# Stale bytecode from an older version would shadow the new files.
Get-ChildItem $target -Filter '__pycache__' -Recurse -Directory -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force

$copied = (Get-ChildItem $target -Filter *.py | Measure-Object).Count
Write-Host "Installed $copied plugin modules to $target" -ForegroundColor Green

# ---- enable it in kritarc --------------------------------------------------
$running = @(Get-Process krita -ErrorAction SilentlyContinue)
if ($running.Count -gt 0 -and -not $Force) {
    Write-Host ''
    Write-Host 'Krita is running, so kritarc was NOT modified (Krita would' -ForegroundColor Yellow
    Write-Host 'overwrite the change when it exits).' -ForegroundColor Yellow
    Write-Host 'Close Krita and re-run this script, or enable the plugin by hand:' -ForegroundColor Yellow
    Write-Host '  Settings > Configure Krita > Python Plugin Manager > MCP Bridge'
    exit 0
}

$key = "enable_$pluginName"
if (Test-Path $kritarc) {
    $lines = [System.Collections.Generic.List[string]](Get-Content $kritarc)
} else {
    $lines = [System.Collections.Generic.List[string]]@()
}

$sectionStart = -1
for ($i = 0; $i -lt $lines.Count; $i++) {
    if ($lines[$i].Trim() -eq '[python]') { $sectionStart = $i; break }
}

if ($sectionStart -lt 0) {
    if ($lines.Count -gt 0 -and $lines[$lines.Count - 1].Trim() -ne '') { $lines.Add('') }
    $lines.Add('[python]')
    $lines.Add("$key=true")
    Write-Host "Added [python] $key=true to $kritarc" -ForegroundColor Green
} else {
    # find the end of the [python] section
    $sectionEnd = $lines.Count
    for ($i = $sectionStart + 1; $i -lt $lines.Count; $i++) {
        if ($lines[$i].TrimStart().StartsWith('[')) { $sectionEnd = $i; break }
    }
    $existing = -1
    for ($i = $sectionStart + 1; $i -lt $sectionEnd; $i++) {
        if ($lines[$i] -match "^\s*$key\s*=") { $existing = $i; break }
    }
    if ($existing -ge 0) {
        $lines[$existing] = "$key=true"
        Write-Host "Set $key=true in $kritarc" -ForegroundColor Green
    } else {
        $lines.Insert($sectionStart + 1, "$key=true")
        Write-Host "Added $key=true under [python] in $kritarc" -ForegroundColor Green
    }
}

Set-Content -Path $kritarc -Value $lines -Encoding utf8

Write-Host ''
Write-Host 'Done. Start Krita, then verify with:' -ForegroundColor Cyan
Write-Host '  python mcp_server.py --selftest'
