#!/usr/bin/env bash
# Periodically scan the guwu-oj project tree for file changes (devlog feature).
set -euo pipefail
PROJECT_DIR="/www/wwwroot/guwu-oj"
cd "$PROJECT_DIR"
exec "$PROJECT_DIR/venv/bin/python" manage.py scan_file_changes
