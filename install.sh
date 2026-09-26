#!/usr/bin/env bash
set -euo pipefail

install_dir="${XDG_BIN_HOME:-$HOME/.local/bin}"
mkdir -p "$install_dir"
install -m 755 "$(dirname "$0")/vitodo.py" "$install_dir/vitodo"

echo "Installed vitodo to $install_dir/vitodo"
case ":$PATH:" in
  *":$install_dir:"*) ;;
  *) echo "Add $install_dir to your PATH, then open a new terminal." ;;
esac
