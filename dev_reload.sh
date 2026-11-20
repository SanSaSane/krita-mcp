#!/usr/bin/env bash
# Development helper for Linux and macOS: copy the plugin sources into Krita's
# pykrita folder and hot-reload krita_mcp.imaging and krita_mcp.ops in the
# running Krita. Changes to extension.py, httpserver.py or mainthread.py still
# need a Krita restart.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
case "$(uname -s)" in
    Darwin) target="$HOME/Library/Application Support/krita/pykrita/krita_mcp" ;;
    *)      target="${XDG_DATA_HOME:-$HOME/.local/share}/krita/pykrita/krita_mcp" ;;
esac
cp "$here"/pykrita/krita_mcp/*.py "$target/"
find "$target" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
params="$(mktemp)"
trap 'rm -f "$params"' EXIT
cat > "$params" <<'JSON'
{"code": "import importlib, sys\nimport krita_mcp.imaging, krita_mcp.ops\nimportlib.reload(krita_mcp.imaging)\nfor name in sorted(n for n in sys.modules if n.startswith('krita_mcp.ops_')):\n    importlib.reload(sys.modules[name])\nimportlib.reload(krita_mcp.ops)\nresult = {'operations': len(krita_mcp.ops.OPS), 'plugin_version': krita_mcp.ops.PLUGIN_VERSION}"}
JSON
"${KRITA_MCP_PYTHON:-python3}" "$here/mcp_server.py" --call run_python --params-file "$params"
