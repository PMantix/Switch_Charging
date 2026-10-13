## PENDING DEPLOY — read first, then delete this section

Commit 8d94dba (coin-cell mode, 2026-10-12) changed
`server/command_server.py` (new `zero_current` command) and was written
on a machine with no network access to the Pis. **It is not deployed.**

When this repo is pulled on a machine that can reach the Pis, tell the
user and offer to deploy:

1. User runs `/deploy <pi-host>` for each Pi (SW1, SW2, …). The skill is
   user-invoked only. Only `server/` changed: **no firmware, no
   BOOTSEL flash.**
2. Verify on each Pi: the log shows "Server ready — listening on port 5555",
   then a raw request
   `{"cmd": "zero_current", "measured_a": [0, 0, 0, 0]}` to port 5555
   returns `"ok": true`. It folds a zero residual, so offsets are
   unchanged. The old server answers with an unknown-command error.
3. Optional on-hardware check: `python -m tui --coincell`, press `i`
   (idle, cells disconnected), then `z`. The current traces should settle
   at ~0 µA. A held `<` / `>` should step coarser above 5 mA; this was
   only verified with simulated key repeat.

Until deployed, everything in coin-cell mode works except the `z` key,
which silently does nothing against the old server.

Once every Pi is deployed, delete this section and commit.

## Delegation triggers — this repo

Auto-delegate to a subagent for these workflows; they reliably blow
past the 5KB / multi-step thresholds:

- **Firmware deploy + verify**: stop pi service → rsync → BOOTSEL flash
  → restart → tail logs → confirm sensor stream. Whole cycle to one agent.
- **DOE runs**: any `recording_doe` / `plot_cmd_vs_sense` /
  `plot_doe_grid` invocation that produces plots — delegate so the
  plot-output volume stays out of main context.
- **TUI debugging across Textual layers**: render cache + CSS + driver
  issues touch many files; spawn Explore.
- **Build firmware-c/**: ARM toolchain build produces verbose output;
  delegate and ask for a pass/fail summary only.
- **Cross-tier tracing** (TUI ↔ Pi server ↔ RP2040 firmware): always
  Explore — three-tier reads are the canonical >3-file case.

Do NOT delegate for:
- Single Python script edits with a known target
- Reading one known config or memory file
- Quick git ops, status checks, single-file diffs

## Subagent audit rules

When spawning agents for verification, auditing, or datasheet
cross-referencing, ALWAYS include this in the prompt:

> CRITICAL: If you cannot read a datasheet or source document (PDF
> parse error, corrupted file, blocked URL), report the failure
> explicitly. Do NOT substitute values from training data or general
> knowledge. An unverified spec presented as verified is worse than a
> gap. Report what you could not read so we can verify manually.

When compiling subagent results, flag any agent that:
- Reported a file read failure but still provided specs for that file
- Used phrases like "based on my knowledge" or "from training data"
- Gave confident numbers without citing a page/table reference

Also include `pcb/PCB_V3_CHANGELIST.md` as required reading for any
agent auditing V3 hardware — it contains empirical V2 failure lessons
that datasheets alone cannot capture.
