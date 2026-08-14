#!/usr/bin/env bash
# Install the Krita MCP bridge plugin on Linux or macOS.
#
# Copies pykrita/krita_mcp into Krita's resource folder and switches the plugin
# on in kritarc. Safe to re-run.
#
# Close Krita first: it rewrites kritarc when it exits and would undo the
# enable flag. Pass --force to write it anyway.

set -euo pipefail

PLUGIN="krita_mcp"
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source_dir="$here/pykrita"

case "$(uname -s)" in
    Darwin)
        resource_dir="$HOME/Library/Application Support/krita"
        config_file="$HOME/Library/Preferences/kritarc"
        ;;
    *)
        resource_dir="${XDG_DATA_HOME:-$HOME/.local/share}/krita"
        config_file="${XDG_CONFIG_HOME:-$HOME/.config}/kritarc"
        ;;
esac

pykrita_dir="$resource_dir/pykrita"

if [ ! -d "$source_dir" ]; then
    echo "Cannot find $source_dir - run this from inside the repository." >&2
    exit 1
fi

# ---- copy the plugin -------------------------------------------------------
mkdir -p "$pykrita_dir"
rm -rf "${pykrita_dir:?}/$PLUGIN"
cp -R "$source_dir/$PLUGIN" "$pykrita_dir/"
cp "$source_dir/$PLUGIN.desktop" "$pykrita_dir/"
find "$pykrita_dir/$PLUGIN" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

echo "Installed plugin to $pykrita_dir/$PLUGIN"

# ---- enable it in kritarc --------------------------------------------------
if pgrep -x krita >/dev/null 2>&1 && [ "$FORCE" -eq 0 ]; then
    cat <<EOF

Krita is running, so kritarc was NOT modified (Krita would overwrite the
change when it exits). Close Krita and re-run, or enable it by hand:
  Settings > Configure Krita > Python Plugin Manager > MCP Bridge
EOF
    exit 0
fi

key="enable_$PLUGIN"
mkdir -p "$(dirname "$config_file")"
touch "$config_file"

if grep -q "^\[python\]" "$config_file"; then
    if grep -q "^$key=" "$config_file"; then
        # portable in-place edit: BSD sed needs an argument to -i
        tmp="$(mktemp)"
        sed "s/^$key=.*/$key=true/" "$config_file" > "$tmp"
        mv "$tmp" "$config_file"
    else
        tmp="$(mktemp)"
        sed "/^\[python\]/a\\
$key=true
" "$config_file" > "$tmp"
        mv "$tmp" "$config_file"
    fi
else
    printf '\n[python]\n%s=true\n' "$key" >> "$config_file"
fi

echo "Enabled $key in $config_file"
echo
echo "Done. Start Krita, then verify with:"
echo "  python3 mcp_server.py --selftest"
