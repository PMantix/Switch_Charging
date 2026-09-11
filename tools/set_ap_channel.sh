#!/usr/bin/env bash
#
# set_ap_channel.sh — move selected Switching Circuit Pi APs to 2.4 GHz.
#
# Default plan (override below):
#   SW2 -> 2.4 GHz, channel 6
#   SW3 -> 2.4 GHz, channel 11
#
# Why 2.4 GHz: longer range / better wall penetration so a 2.4 GHz USB
# adapter on the remote PC can see the APs. (Channels 6 and 11 are the
# non-overlapping 2.4 GHz channels — your "2.5 GHz" almost certainly
# meant 2.4 GHz.)
#
# What it does, per target Pi:
#   1. Join the Pi's AP from this Mac (en0).
#   2. Wait for SSH (10.42.0.1:22) to come up.
#   3. ssh pi@10.42.0.1 -> nmcli modify wifi.band bg + wifi.channel <ch>
#   4. Read the value back to confirm it stuck.
#   5. Schedule a DETACHED down/up on the Pi so the AP restarts on the new
#      channel *after* our SSH session drops (the restart kills our link).
#   Then it moves to the next Pi, and finally rejoins ALLHONDAWLAN.
#
# RUN THIS FROM YOUR OWN Terminal, not from inside Claude Code:
#   - en0 roams the Pi APs, so internet (and any remote agent) drops while
#     it runs. That's expected; it rejoins ALLHONDAWLAN at the end.
#   - macOS needs Location Services granted to Terminal for the Wi-Fi join,
#     and may pop an admin prompt per join on Sequoia+.
#   - SSH auth uses whatever you set up when imaging (key in your agent, or
#     you'll be prompted for the pi password). This script does not embed one.
#
# Usage:
#   ./tools/set_ap_channel.sh
#   ./tools/set_ap_channel.sh "pi_SW2:6 pi_SW3:11"     # custom targets
#
set -uo pipefail

# ---- config ---------------------------------------------------------------
WIFI_IFACE="${WIFI_IFACE:-en0}"
PI_IP="${PI_IP:-10.42.0.1}"
SSH_USER="${SSH_USER:-pi}"

# Wi-Fi PSK used to JOIN the pi_SW# APs from this Mac. The repo is
# inconsistent (server/fleet.py says "raspberry", tui/app.py says
# "switching"). Override with AP_PSK=... if the join fails.
AP_PSK="${AP_PSK:-raspberry}"

# Network to rejoin at the end. If HOME_PSK is empty we just bounce the
# radio and let macOS auto-rejoin this remembered network (no password
# needed). Set HOME_PSK=... to force an explicit join.
HOME_SSID="${HOME_SSID:-ALLHONDAWLAN}"
HOME_PSK="${HOME_PSK:-}"

# Targets: "con-name:channel" pairs. con-name == SSID == pi_SW<n>.
TARGETS="${1:-pi_SW2:6 pi_SW3:11}"

# SSH opts: bypass known_hosts because every Pi answers on the SAME IP
# (10.42.0.1) with a DIFFERENT host key — a real known_hosts entry would
# fail verification on the second Pi.
SSH_OPTS=(-o StrictHostKeyChecking=no
          -o UserKnownHostsFile=/dev/null
          -o GlobalKnownHostsFile=/dev/null
          -o ConnectTimeout=10
          -o LogLevel=ERROR)

# ---- helpers --------------------------------------------------------------
say()  { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

join_ap() {  # join_ap <ssid>
    local ssid="$1"
    say "Joining AP '$ssid' on $WIFI_IFACE …"
    if ! networksetup -setairportnetwork "$WIFI_IFACE" "$ssid" "$AP_PSK"; then
        die "Could not join '$ssid'. Wrong AP_PSK, or out of range / on 5 GHz the radio can't reach?"
    fi
}

wait_for_ssh() {  # wait_for_ssh <deadline_seconds>
    local deadline=$(( SECONDS + ${1:-30} ))
    say "Waiting for $PI_IP:22 …"
    while (( SECONDS < deadline )); do
        if /usr/bin/nc -z -G 2 "$PI_IP" 22 >/dev/null 2>&1; then
            echo "    reachable."
            return 0
        fi
        sleep 1
    done
    return 1
}

configure_pi() {  # configure_pi <con-name> <channel>
    local con="$1" ch="$2"

    say "Setting $con -> 2.4 GHz (band bg), channel $ch"
    ssh "${SSH_OPTS[@]}" "$SSH_USER@$PI_IP" \
        "sudo nmcli connection modify '$con' wifi.band bg wifi.channel $ch" \
        || die "modify failed on $con (ssh auth? sudo? wrong con-name?)"

    # Read it back BEFORE the restart drops us.
    local got
    got="$(ssh "${SSH_OPTS[@]}" "$SSH_USER@$PI_IP" \
        "sudo nmcli -g 802-11-wireless.band,802-11-wireless.channel connection show '$con'" 2>/dev/null)"
    echo "    profile now reports: ${got//$'\n'/ }   (expected: bg / $ch)"
    case "$got" in
        *bg*"$ch"*) echo "    confirmed." ;;
        *) warn "readback did not match expected 'bg / $ch' — check the Pi manually." ;;
    esac

    # Restart the AP DETACHED so it survives our SSH disconnect. The 'sleep 3'
    # lets this ssh return cleanly before the radio drops. A reboot would also
    # reapply it via ap-fallback.service, so even a failed bounce self-heals.
    say "Scheduling AP restart on $con (our link will drop shortly) …"
    ssh "${SSH_OPTS[@]}" "$SSH_USER@$PI_IP" \
        "sudo bash -c 'nohup sh -c \"sleep 3; nmcli connection down \\\"$con\\\"; nmcli connection up \\\"$con\\\"\" >/tmp/ap-rechannel.log 2>&1 </dev/null &'" \
        || warn "could not schedule restart on $con; it will still apply on next reboot."
    echo "    restart scheduled; moving on."
}

rejoin_home() {
    say "Rejoining '$HOME_SSID' …"
    if [[ -n "$HOME_PSK" ]]; then
        networksetup -setairportnetwork "$WIFI_IFACE" "$HOME_SSID" "$HOME_PSK" \
            || warn "explicit join failed; falling back to radio bounce."
    fi
    # Bounce the radio so macOS auto-rejoins the preferred (remembered) network.
    networksetup -setairportpower "$WIFI_IFACE" off
    sleep 2
    networksetup -setairportpower "$WIFI_IFACE" on
    echo "    Wi-Fi bounced; macOS should reconnect to '$HOME_SSID' shortly."
}

# ---- main -----------------------------------------------------------------
command -v networksetup >/dev/null || die "networksetup not found — macOS only."

say "Plan: $TARGETS   (iface=$WIFI_IFACE, pi=$SSH_USER@$PI_IP)"

for pair in $TARGETS; do
    con="${pair%%:*}"
    ch="${pair##*:}"
    [[ "$con" == pi_SW* && -n "$ch" ]] || die "bad target '$pair' (want con-name:channel, e.g. pi_SW2:6)"

    join_ap "$con"
    if ! wait_for_ssh 30; then
        warn "no SSH on $PI_IP after joining $con — skipping it."
        continue
    fi
    configure_pi "$con" "$ch"
    # Give the detached restart a moment to begin before we roam away.
    sleep 4
done

rejoin_home
say "Done. Verify from the remote PC's 2.4 GHz adapter that the APs now appear."
