"""
Switching Circuit V2 - Pi list widget.

Renders fleet hostnames with TCP probe latency. Live hits show ms,
unreachable hostnames are dimmed with an "offline" tag. Cloned from
FleetList rather than parameterised because the unit and primary key
differ enough to make sharing fiddly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from rich.text import Text
from textual.reactive import reactive
from textual.widget import Widget


@dataclass(frozen=True)
class PiEntry:
    hostname: str           # full hostname e.g. "pi-SW3.local"
    latency_ms: Optional[float]
    is_current: bool
    online: bool


class PiList(Widget):
    """Single-selection list of Pis with arrow-key + number-key nav."""

    DEFAULT_CSS = """
    PiList {
        height: auto;
        max-height: 10;
        padding: 0 1;
        background: $panel;
        border: round $accent;
    }
    """

    entries: reactive[tuple[PiEntry, ...]] = reactive(())
    selected_index: reactive[int] = reactive(0)

    can_focus = True

    def set_entries(self, entries: Iterable[PiEntry]) -> None:
        self.entries = tuple(entries)
        if self.selected_index >= len(self.entries):
            self.selected_index = max(0, len(self.entries) - 1)
        self.refresh(layout=True)

    def set_selected(self, index: int) -> None:
        if 0 <= index < len(self.entries):
            self.selected_index = index
            self.refresh()

    def selected_entry(self) -> Optional[PiEntry]:
        if not self.entries:
            return None
        idx = max(0, min(self.selected_index, len(self.entries) - 1))
        return self.entries[idx]

    # -- rendering -----------------------------------------------------------

    def get_content_height(self, container, viewport, width: int) -> int:
        """Tell Textual how many lines `render()` actually needs — without
        this, `height: auto` on a plain Widget with a custom render() has
        no way to know, and silently collapses to 1 line regardless of
        how many entries there are."""
        return max(1, len(self.entries))

    def render(self) -> Text:
        if not self.entries:
            return Text("  (no Pis discovered — press r to rescan)", style="dim")

        out = Text()
        for i, entry in enumerate(self.entries):
            prefix = "▶ " if i == self.selected_index else "  "
            index_label = f"[{i + 1}]" if i < 9 else "[ ]"
            short = entry.hostname.replace(".local", "")

            row = Text()
            row.append(prefix, style="bold cyan" if i == self.selected_index else "dim")
            row.append(f"{index_label} ", style="bold")
            row.append(short.ljust(10))

            if entry.online:
                if entry.latency_ms is not None:
                    row.append(f"  {entry.latency_ms:>4.0f} ms")
                else:
                    row.append("    —    ")
                if entry.is_current:
                    row.append("  (current)", style="bold green")
            else:
                row.append("  offline", style="dim italic")

            out.append(row)
            if i < len(self.entries) - 1:
                out.append("\n")
        return out

    # -- keys ----------------------------------------------------------------

    def on_key(self, event) -> None:
        if event.key == "up":
            event.stop()
            if self.entries:
                self.selected_index = (self.selected_index - 1) % len(self.entries)
                self.refresh()
        elif event.key == "down":
            event.stop()
            if self.entries:
                self.selected_index = (self.selected_index + 1) % len(self.entries)
                self.refresh()
