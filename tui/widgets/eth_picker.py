"""
Switching Circuit V2 - Ethernet Pi Picker modal.

Lists Pis reachable on the shared Ethernet/switch network (static IPs,
see tui/eth_scan.py) and lets the user pick one to make active. Unlike
PiPicker (WiFi), no join step is needed — every listed Pi is already
simultaneously reachable, so this only ever swaps which one the TUI's
active connection points at.
"""

from __future__ import annotations

import threading
from typing import Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label

from tui.eth_scan import EthPi, scan_eth_pis
from tui.widgets.pi_list import PiEntry, PiList


class EthPicker(ModalScreen[str]):
    """Modal that lists Ethernet-reachable Pis and lets the user pick one.

    Dismisses with the chosen IP address, or "" on cancel.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("r", "rescan", "Rescan", show=False),
        Binding("enter", "confirm", "Switch", show=False),
        *[Binding(str(n), f"select_{n}", show=False) for n in range(1, 9)],
    ]

    DEFAULT_CSS = """
    EthPicker {
        align: center middle;
    }
    #eth-picker-box {
        width: 60;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #eth-picker-title {
        text-align: center;
        width: 100%;
        margin-bottom: 1;
    }
    #eth-picker-current {
        width: 100%;
        margin-bottom: 1;
        color: $text-muted;
    }
    #eth-picker-status {
        text-align: center;
        width: 100%;
        margin-top: 1;
        color: $text-muted;
    }
    #eth-picker-hint {
        text-align: center;
        width: 100%;
        margin-top: 1;
        color: $text-muted;
    }
    """

    def __init__(self, current_host: str = ""):
        super().__init__()
        self._current_host = current_host
        self._pis: list[EthPi] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="eth-picker-box"):
            yield Label("Switch Pi (Ethernet)", id="eth-picker-title")
            yield Label(
                f"Currently: {self._current_host or '(not connected)'}",
                id="eth-picker-current",
            )
            yield PiList(id="eth-picker-list")
            yield Label("[dim]Probing Ethernet fleet...[/]", id="eth-picker-status")
            yield Label(
                "↑↓ select  1-8 jump  r rescan  ⏎ switch  Esc cancel",
                id="eth-picker-hint",
            )

    def on_mount(self) -> None:
        self._start_scan()

    def _start_scan(self) -> None:
        try:
            self.query_one("#eth-picker-status", Label).update(
                "[dim]Probing Ethernet fleet...[/]"
            )
        except Exception:
            pass
        t = threading.Thread(target=self._scan_thread, daemon=True)
        t.start()

    def _scan_thread(self) -> None:
        pis = scan_eth_pis()
        try:
            self.app.call_from_thread(self._on_scan_done, pis)
        except Exception:
            pass

    def _on_scan_done(self, pis: list[EthPi]) -> None:
        self._pis = pis
        entries = [
            PiEntry(
                hostname=f"{p.label} ({p.ip})",
                latency_ms=p.latency_ms,
                is_current=(p.ip == self._current_host),
                online=p.online,
            )
            for p in pis
        ]
        try:
            self.query_one("#eth-picker-list", PiList).set_entries(entries)
            n_online = sum(1 for p in pis if p.online)
            self.query_one("#eth-picker-status", Label).update(
                f"[dim]{n_online}/{len(pis)} online[/]"
            )
        except Exception:
            pass

    def action_cancel(self) -> None:
        self.dismiss("")

    def action_rescan(self) -> None:
        self._start_scan()

    def action_confirm(self) -> None:
        picker = self.query_one("#eth-picker-list", PiList)
        entry = picker.selected_entry()
        if not entry:
            self._set_status("[bold red]No Pi selected[/]")
            return
        idx = picker.selected_index
        if idx >= len(self._pis):
            return
        p = self._pis[idx]
        if not p.online:
            self._set_status(f"[bold yellow]{p.label} is offline[/]")
            return
        if p.ip == self._current_host:
            self._set_status("[dim]Already connected[/]")
            return
        self.dismiss(p.ip)

    def _set_status(self, msg: str) -> None:
        try:
            self.query_one("#eth-picker-status", Label).update(msg)
        except Exception:
            pass

    def _select(self, idx: int) -> None:
        self.query_one("#eth-picker-list", PiList).set_selected(idx)

    def action_select_1(self) -> None: self._select(0)
    def action_select_2(self) -> None: self._select(1)
    def action_select_3(self) -> None: self._select(2)
    def action_select_4(self) -> None: self._select(3)
    def action_select_5(self) -> None: self._select(4)
    def action_select_6(self) -> None: self._select(5)
    def action_select_7(self) -> None: self._select(6)
    def action_select_8(self) -> None: self._select(7)
