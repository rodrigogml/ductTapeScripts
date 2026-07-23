#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON_BIN=${PYTHON_BIN:-python3}
SCRIPT_PATH="$SCRIPT_DIR/IPaparazzi.py"
CONFIG_PATH="$SCRIPT_DIR/IPaparazzi.toml"
INTERVAL_MINUTES=15
MARKER="# IPaparazzi managed job"

usage() {
    cat <<'EOF'
Usage: ./install-cron.sh [options]

Options:
  --interval MINUTES  Allowed: 1,2,3,4,5,6,10,12,15,20,30,60 (default: 15)
  --python PATH       Python executable (default: python3)
  --script PATH       IPaparazzi.py path
  --config PATH       IPaparazzi.toml path
  --help              Show this help
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --interval)
            [ "$#" -ge 2 ] || { echo "missing value for --interval" >&2; exit 2; }
            INTERVAL_MINUTES=$2
            shift 2
            ;;
        --python)
            [ "$#" -ge 2 ] || { echo "missing value for --python" >&2; exit 2; }
            PYTHON_BIN=$2
            shift 2
            ;;
        --script)
            [ "$#" -ge 2 ] || { echo "missing value for --script" >&2; exit 2; }
            SCRIPT_PATH=$2
            shift 2
            ;;
        --config)
            [ "$#" -ge 2 ] || { echo "missing value for --config" >&2; exit 2; }
            CONFIG_PATH=$2
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            echo "unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

case "$INTERVAL_MINUTES" in
    1|2|3|4|5|6|10|12|15|20|30)
        SCHEDULE="*/$INTERVAL_MINUTES * * * *"
        ;;
    60)
        SCHEDULE="0 * * * *"
        ;;
    *)
        echo "invalid interval: $INTERVAL_MINUTES" >&2
        echo "use a whole-hour divisor: 1,2,3,4,5,6,10,12,15,20,30,60" >&2
        exit 2
        ;;
esac

command -v crontab >/dev/null 2>&1 || {
    echo "crontab command not found" >&2
    exit 3
}

[ -f "$SCRIPT_PATH" ] || { echo "script not found: $SCRIPT_PATH" >&2; exit 2; }
[ -f "$CONFIG_PATH" ] || { echo "config not found: $CONFIG_PATH" >&2; exit 2; }
command -v "$PYTHON_BIN" >/dev/null 2>&1 || {
    echo "Python executable not found: $PYTHON_BIN" >&2
    exit 2
}

SCRIPT_PATH=$(cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd)/$(basename -- "$SCRIPT_PATH")
CONFIG_PATH=$(cd -- "$(dirname -- "$CONFIG_PATH")" && pwd)/$(basename -- "$CONFIG_PATH")

"$PYTHON_BIN" "$SCRIPT_PATH" --config "$CONFIG_PATH" --check-config
chmod 600 "$CONFIG_PATH"
chmod 755 "$SCRIPT_PATH"

shell_quote() {
    escaped=$(printf '%s' "$1" | sed "s/'/'\\\\''/g")
    printf "'%s'" "$escaped"
}

PYTHON_QUOTED=$(shell_quote "$PYTHON_BIN")
SCRIPT_QUOTED=$(shell_quote "$SCRIPT_PATH")
CONFIG_QUOTED=$(shell_quote "$CONFIG_PATH")
CRON_LINE="$SCHEDULE $PYTHON_QUOTED $SCRIPT_QUOTED --config $CONFIG_QUOTED >/dev/null 2>&1 $MARKER"

TEMP_FILE=$(mktemp "${TMPDIR:-/tmp}/IPaparazzi-cron.XXXXXX")
trap 'rm -f "$TEMP_FILE"' EXIT HUP INT TERM

crontab -l 2>/dev/null | grep -Fv "$MARKER" >"$TEMP_FILE" || true
printf '%s\n' "$CRON_LINE" >>"$TEMP_FILE"
crontab "$TEMP_FILE"

echo "IPaparazzi cron installed for the current user: every $INTERVAL_MINUTES minute(s)."
echo "Config: $CONFIG_PATH"
