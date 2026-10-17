---
name: deploy
description: Deploy server code (and optionally firmware) to a fleet Raspberry Pi over SSH
disable-model-invocation: true
allowed-tools: Bash(git *) Bash(ssh *) Bash(scp *) Bash(python *)
argument-hint: [pi-host or IP, e.g. 192.168.137.105]
---

Deploy the switching circuit server to one Pi.

Target host: $ARGUMENTS (default: `~/.switching-circuit-host`)

## How the fleet Pis are set up (checked 2026-10-17)

- Code lives in `/home/pi/Code/Switch_Charging_V2/` (a git checkout on most boards).
- The server runs under **systemd**: unit `switching-circuit`, `WorkingDirectory=/home/pi/Code/Switch_Charging_V2`,
  `ExecStart=/usr/bin/python3 -m server`, `Restart=on-failure`. **Never** start it by hand with `nohup python3 -m
  server`: that launches a second server that fights the systemd one for port 5555 and the RP2040 serial port.
- `server/fleet.py` is **per board** (`my_ap_ssid()` → `pi_SW<n>`, `my_static_eth_ip()` → that board's
  192.168.137.x). The repo copy returns hostname/None. Overwriting it loses the board's static IP on the next boot,
  so it is always excluded below.
- Settings (mode, frequency, auto-follow, sweep position) persist in `~/.switching_circuit_state.json` and are
  restored on restart; the deploy leaves that file alone.

## Steps

1. Deploy a commit, not a dirty tree: `git status --short server/` must be empty. Note `git rev-parse --short HEAD`.

2. **Back up** the board's current server (once per day is enough):
   ```
   ssh pi@<host> "cd ~/Code/Switch_Charging_V2 && cp -a server ~/server_backup_$(date +%Y%m%d)"
   ```

3. **Upload** HEAD's `server/`, keeping the board's `fleet.py` (git archive gives LF endings):
   ```
   git archive --format=tar HEAD server | ssh pi@<host> "cd ~/Code/Switch_Charging_V2 && tar -x --exclude=server/fleet.py && echo <rev> > server/DEPLOYED_REV"
   ```
   "time stamp ... in the future" warnings are harmless (the Pi clocks run days behind).

4. **Restart through systemd** (passwordless sudo is set up):
   ```
   ssh pi@<host> "sudo -n systemctl restart switching-circuit"
   ```

5. **Verify**: within ~10 s the journal shows "Server ready — listening on port 5555", and the status commands answer:
   ```
   ssh pi@<host> "journalctl -u switching-circuit --since '-60 s' --no-pager | tail -5"
   ssh pi@<host> "python3 -c \"import socket,json;s=socket.create_connection(('127.0.0.1',5555),3);s.sendall(b'{\\\"cmd\\\": \\\"auto_follow_status\\\"}\\n');print(s.recv(65536).decode())\""
   ```
   "Failed to initialise local hardware ... Cannot determine SOC peripheral base address" at startup is
   pre-existing on every board and harmless (the RP2040 link is separate).
   Check auto-follow `enabled` afterwards: it comes back as last **persisted**, which is not always what was live.

6. **Firmware** only if `firmware-c/` changed: stop the service first (`sudo -n systemctl stop switching-circuit`),
   flash with `~/flash_rp2040.sh`, then `sudo -n systemctl start switching-circuit`. Boards whose status reports
   `"live_cmds": false` (SW6, SW8 on 2026-10-17) run firmware older than 2026-05-20.

## Rollback

```
ssh pi@<host> "cd ~/Code/Switch_Charging_V2 && rm -rf server && cp -a ~/server_backup_<date> server && sudo -n systemctl restart switching-circuit"
```

## Fleet IPs

.105 SW1, .107 SW3, .108 SW4, .109 SW5, .104 SW6, .101 SW7, .102 SW8, .103 SW9, .110 SW10.
