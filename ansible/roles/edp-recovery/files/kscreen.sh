# Sourced by edp-watchdog and edp-recover. Runs kscreen-doctor against the
# live KWin session from any context: a systemd user service, SSH, or a
# desktop shortcut.

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"
export QT_QPA_PLATFORM=wayland

# The KWin socket name (wayland-0, wayland-1, ...) changes between sessions,
# and callers may start before or outlive the session, so resolve it per call.
# With no live socket Qt aborts and dumps core; return 1 instead.
kdoc() {
    local s f
    s=$(systemctl --user show-environment 2>/dev/null | sed -n 's/^WAYLAND_DISPLAY=//p')
    if [[ -z $s || ! -S $XDG_RUNTIME_DIR/$s ]]; then
        s=""
        for f in "$XDG_RUNTIME_DIR"/wayland-[0-9]*; do
            [[ -S $f ]] && { s=${f##*/}; break; }
        done
    fi
    [[ -z $s ]] && return 1
    WAYLAND_DISPLAY=$s kscreen-doctor "$@"
}

# Retrain the eDP link with a full modeset: DPMS off takes the pipe and DDI
# down, DPMS on powers them back up and retrains the link. Disabling the
# output would do the same, but KWin refuses to disable the last enabled
# output (and still exits 0), so that fails on a laptop with no external
# monitor. Blanks every output for $1 seconds (default 3). Returns 1 if the
# kernel does not report the panel on again.
edp_retrain() {
    local hold=${1:-3} f
    kdoc --dpms off >/dev/null 2>&1 || return 1
    sleep "$hold"
    kdoc --dpms on >/dev/null 2>&1 || return 1
    sleep 1
    for f in /sys/class/drm/card*-eDP-*/dpms; do
        [[ -r $f && $(cat "$f") == On ]] && return 0
    done
    return 1
}

# Name of the internal panel output, e.g. eDP-1.
find_edp() {
    kdoc -o 2>/dev/null | sed 's/\x1b\[[0-9;]*m//g' \
        | awk '/^Output:/ && $3 ~ /^eDP/ {print $3; exit}'
}
