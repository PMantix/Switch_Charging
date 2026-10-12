"""
Switching Circuit V2 - Schedule Preview & Edit Screen.

Two-stage flow:
  1. Pick a schedule file (or enter a path)
  2. Preview all steps with parameters, optionally adjust, then Load into
     the schedule monitor or Cancel. Loading does not change the circuit
     mode; the monitor passively tracks the plan against observed state.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, Label, Static


STATE_ABBREV = {
    "cc_charge": "CC CHG",
    "cv_charge": "CV CHG",
    "rest": "REST",
    "discharge": "DISCH",
}

ACTION_LABELS = {
    "charge": "Switching",
    "discharge": "All ON",
    "idle": "All OFF",
    "pulse_charge": "Pulse",
}


# ---------------------------------------------------------------------------
# Stage 1: File picker (same as old ScheduleDialog, but dismisses with path)
# ---------------------------------------------------------------------------
class SchedulePickerScreen(ModalScreen[str]):
    """Pick a schedule JSON file."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    SchedulePickerScreen { align: center middle; }
    #picker-box {
        width: 60; height: 16;
        border: thick $accent; background: $surface; padding: 1 2;
    }
    #picker-title { text-align: center; width: 100%; margin-bottom: 1; }
    #picker-list { width: 100%; margin-bottom: 1; color: $text-muted; }
    #picker-hint { text-align: center; width: 100%; margin-top: 1; color: $text-muted; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-box"):
            yield Label("[bold cyan]Select Schedule File[/]", id="picker-title")
            yield Label("", id="picker-list")
            yield Input(placeholder="Number or path to .json", id="picker-input")
            yield Label("Enter=select, Escape=cancel", id="picker-hint")

    def on_mount(self) -> None:
        self._paths: list[str] = []
        sched_dir = Path("schedules")
        label = self.query_one("#picker-list", Label)
        if sched_dir.is_dir():
            files = sorted(sched_dir.glob("*.json"))
            self._paths = [str(f) for f in files]
            if files:
                lines = [f"  [bold]{i}[/] {f.name}" for i, f in enumerate(files, 1)]
                label.update("\n".join(lines))
            else:
                label.update("[dim]No .json files in schedules/[/]")
        else:
            label.update("[dim]No schedules/ directory[/]")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        v = event.value.strip()
        if not v:
            self.dismiss("")
            return
        if v.isdigit():
            idx = int(v) - 1
            if 0 <= idx < len(self._paths):
                self.dismiss(self._paths[idx])
                return
        self.dismiss(v)

    def action_cancel(self) -> None:
        self.dismiss("")


# ---------------------------------------------------------------------------
# Stage 2: Preview + edit + start
# ---------------------------------------------------------------------------
class SchedulePreviewScreen(ModalScreen[Optional[dict]]):
    """Preview a loaded schedule, tweak parameters, then start or cancel.

    Dismisses with the (possibly edited) schedule dict, or None on cancel.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "start", "Load to Monitor"),
    ]

    DEFAULT_CSS = """
    SchedulePreviewScreen { align: center middle; }
    #preview-box {
        width: 80; max-width: 90%;
        height: auto; max-height: 85%;
        border: thick $accent; background: $surface; padding: 1 2;
        overflow-y: auto;
    }
    #preview-title { text-align: center; width: 100%; margin-bottom: 1; }
    #preview-desc { width: 100%; margin-bottom: 1; color: $text-muted; }
    #step-table { width: 100%; margin-bottom: 1; }
    #preview-settings { width: 100%; margin-bottom: 1; }
    #preview-warnings { width: 100%; margin-bottom: 1; }
    #btn-row { width: 100%; height: 3; align: center middle; }
    #btn-row Button { margin: 0 2; min-width: 16; }
    """

    def __init__(self, schedule_raw: dict, path: str = "",
                 warnings: list[str] = None):
        super().__init__()
        self._raw = schedule_raw
        self._path = path
        self._warnings = warnings or []

    def compose(self) -> ComposeResult:
        r = self._raw
        name = r.get("name", "Untitled")
        desc = r.get("description", "")
        steps = r.get("steps", [])
        repeat = r.get("repeat", 1)
        on_timeout = r.get("default_on_timeout", "wait")
        grace = r.get("default_timeout_grace_s", -1)

        with VerticalScroll(id="preview-box"):
            yield Label(f"[bold cyan]Schedule: {name}[/]", id="preview-title")
            if desc:
                yield Label(f"[dim]{desc}[/]", id="preview-desc")

            # Summary line
            total_time = sum(s.get("timeout_s", 0) for s in steps)
            yield Label(
                f"[bold]{len(steps)}[/] steps  |  "
                f"[bold]{repeat}[/] cycles  |  "
                f"~[bold]{self._fmt_time(total_time)}[/] per cycle  |  "
                f"on_timeout=[bold]{on_timeout}[/]"
            )

            # Step table
            yield Static(self._build_step_table(steps), id="step-table")

            # Editable settings
            yield Label("[bold white]Settings[/] (edit below, or press Enter to load as-is)",
                        id="preview-settings")
            yield Label("Repeat cycles:", classes="field-label")
            yield Input(value=str(repeat), id="input-repeat",
                        placeholder="Number of cycles")
            yield Label("Default on_timeout (wait/advance/abort):", classes="field-label")
            yield Input(value=on_timeout, id="input-on-timeout",
                        placeholder="wait, advance, or abort")

            # Warnings
            if self._warnings:
                warn_text = "[bold yellow]Warnings:[/]\n"
                for w in self._warnings:
                    warn_text += f"  [yellow]\u26a0[/] {w}\n"
                yield Label(warn_text, id="preview-warnings")

            # Buttons
            with Horizontal(id="btn-row"):
                yield Button("Load to Monitor", variant="success", id="btn-start")
                yield Button("Cancel", variant="error", id="btn-cancel")

    def _build_step_table(self, steps: list[dict]) -> str:
        """Build a Rich-markup table of schedule steps."""
        lines = []
        lines.append(
            f"[bold]{'#':>3} {'Name':<22} {'Expect':<8} {'Action':<10} "
            f"{'Freq':>6} {'Timeout':>8} {'OnTimeout':<8}[/]"
        )
        lines.append("[dim]" + "\u2500" * 72 + "[/]")

        for i, s in enumerate(steps):
            name = s.get("name", f"Step {i}")
            if len(name) > 20:
                name = name[:19] + "\u2026"
            expect = STATE_ABBREV.get(s.get("expected_state", ""), "?")
            action = ACTION_LABELS.get(s.get("circuit_action", ""), "?")
            freq = f"{s.get('frequency', 0):.0f}Hz" if s.get("circuit_action") == "charge" else ""
            timeout = self._fmt_time(s.get("timeout_s", 0))
            on_t = s.get("on_timeout", "")

            lines.append(
                f" {i+1:>2}  {name:<22} [bold]{expect:<8}[/] {action:<10} "
                f"{freq:>6} {timeout:>8} {on_t:<8}"
            )

        return "\n".join(lines)

    @staticmethod
    def _fmt_time(seconds) -> str:
        s = int(seconds)
        if s >= 3600:
            return f"{s//3600}h{(s%3600)//60:02d}m"
        if s >= 60:
            return f"{s//60}m{s%60:02d}s"
        return f"{s}s"

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-start":
            self._apply_edits()
            self.dismiss(self._raw)
        elif event.button.id == "btn-cancel":
            self.dismiss(None)

    def action_start(self) -> None:
        self._apply_edits()
        self.dismiss(self._raw)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _apply_edits(self) -> None:
        """Read input fields and update the raw schedule dict."""
        try:
            repeat_input = self.query_one("#input-repeat", Input)
            val = int(repeat_input.value.strip())
            if val >= 1:
                self._raw["repeat"] = val
        except (ValueError, Exception):
            pass

        try:
            timeout_input = self.query_one("#input-on-timeout", Input)
            val = timeout_input.value.strip().lower()
            if val in ("wait", "advance", "abort"):
                self._raw["default_on_timeout"] = val
        except Exception:
            pass
