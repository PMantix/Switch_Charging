"""Tests for RP2040 link supervision in GPIODriver: reconnect after a
serial error, stream watchdog, stale-data reporting, and AutoFollow
holding its mode while sensor data is stale."""

import queue
import time
import unittest
from unittest.mock import MagicMock, patch

import serial

from server import gpio_driver
from server.auto_follow import AutoFollow
from server.gpio_driver import GPIODriver
from server.mode_controller import Mode, ModeController

D_LINE = b"D 1000 1 4.0 0.05 4.0 0.05 0.0 0.05 0.0 0.05\n"

# Canned firmware replies, keyed by command letter.
_REPLIES = {
    "T": "OK T 15.0", "A": "OK A 4 350.0", "V": "OK V 1 350.0",
    "M": "OK M 4 1 350.0", "P": "OK P 123456", "G": "OK G 0 0 123456",
}


class FakeSerial:
    """Stand-in for serial.Serial: answers commands, streams D lines on
    demand, and can be told to fail the next read."""
    available = {"/dev/ttyACM0"}
    instances = []
    old_firmware = False  # predates live commands E / Y

    def __init__(self, port, baud, timeout=0.5):
        if port not in FakeSerial.available:
            raise serial.SerialException(f"could not open {port}")
        self.port = port
        self.is_open = True
        self.rx = queue.Queue()
        self.written = []
        self.fail_next_read = False
        self.rx.put(b"OK READY\n")
        FakeSerial.instances.append(self)

    @property
    def in_waiting(self):
        return self.rx.qsize()

    def readline(self):
        if not self.is_open:
            raise serial.SerialException("port closed")
        if self.fail_next_read:
            self.fail_next_read = False
            raise serial.SerialException(
                "device reports readiness to read but returned no data")
        try:
            return self.rx.get(timeout=0.05)
        except queue.Empty:
            return b""

    def write(self, data):
        cmd = data.decode().strip()
        self.written.append(cmd)
        toks = cmd.split()
        letter = toks[0]
        if letter in ("E", "Y") and FakeSerial.old_firmware:
            reply = f"ERR unknown command: {letter}"
        elif letter == "Y" and len(toks) != 5:
            reply = "ERR Y requires 4 args: Y P1 P2 N1 N2"
        else:
            reply = _REPLIES.get(letter, f"OK {letter}")
        self.rx.put((reply + "\n").encode())

    def flush(self):
        pass

    def close(self):
        self.is_open = False


def _wait_for(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


@patch.object(gpio_driver, "RP2040_RECONNECT_INTERVAL_S", 0.05)
@patch.object(gpio_driver.serial, "Serial", FakeSerial)
class TestGPIOLink(unittest.TestCase):

    def setUp(self):
        FakeSerial.available = {"/dev/ttyACM0"}
        FakeSerial.instances = []
        FakeSerial.old_firmware = False
        self.drivers = []

    def tearDown(self):
        for d in self.drivers:
            d._stop_event.set()

    def _driver(self):
        d = GPIODriver()
        self.drivers.append(d)
        return d

    def test_connects_and_reports_stale_until_first_frame(self):
        d = self._driver()
        st = d.link_status()
        self.assertEqual(st["state"], "connected")
        self.assertTrue(st["stale"])  # nothing streamed yet
        FakeSerial.instances[-1].rx.put(D_LINE)
        self.assertTrue(_wait_for(d.sensor_data_fresh))
        self.assertEqual(d.get_sensor_data()["P1"]["voltage"], 4.0)

    def test_read_error_reconnects_and_restores(self):
        d = self._driver()
        d.set_sensor_rate(15.0)
        d.set_ina226_avg(4)
        reconnected = MagicMock()
        d.on_reconnect = reconnected
        first = FakeSerial.instances[-1]

        first.fail_next_read = True
        self.assertTrue(_wait_for(lambda: d.link_status()["reconnects"] == 1))
        self.assertFalse(first.is_open)
        second = FakeSerial.instances[-1]
        self.assertIsNot(second, first)
        # Stream settings re-sent to the (possibly rebooted) firmware,
        # then the mode callback fired.
        self.assertTrue(_wait_for(lambda: reconnected.called))
        self.assertIn("A 4", second.written)
        self.assertIn("T 15.0", second.written)
        st = d.link_status()
        self.assertEqual(st["state"], "connected")
        self.assertIn("readiness to read", st["last_error"])

    def test_stays_down_and_stale_while_port_missing(self):
        d = self._driver()
        FakeSerial.instances[-1].rx.put(D_LINE)
        self.assertTrue(_wait_for(d.sensor_data_fresh))
        FakeSerial.available = set()  # unplugged
        FakeSerial.instances[-1].fail_next_read = True
        self.assertTrue(_wait_for(lambda: d.link_status()["state"] == "disconnected"))
        self.assertTrue(d.link_status()["stale"])
        self.assertIsNone(d._send("P"))  # commands fail fast, no hang
        FakeSerial.available = {"/dev/ttyACM1"}  # re-enumerated
        self.assertTrue(_wait_for(lambda: d.link_status()["state"] == "connected"))
        self.assertEqual(d.link_status()["port"], "/dev/ttyACM1")

    def test_watchdog_reopens_silent_stream(self):
        d = self._driver()
        d.set_sensor_rate(15.0)
        first = FakeSerial.instances[-1]
        with patch.object(gpio_driver, "SENSOR_WATCHDOG_S", 0.2):
            self.assertTrue(_wait_for(lambda: d.link_status()["reconnects"] >= 1))
        self.assertFalse(first.is_open)
        self.assertIn("no sensor data", d.link_status()["last_error"])

    def test_probe_detects_current_firmware(self):
        d = self._driver()
        self.assertTrue(d.live_commands_supported)
        self.assertTrue(d.link_status()["live_cmds"])
        # The probe must not have changed any FET state.
        self.assertIn("Y", FakeSerial.instances[-1].written)
        self.assertNotIn("Y 1 1 1 1", FakeSerial.instances[-1].written)

    def test_probe_detects_old_firmware(self):
        FakeSerial.old_firmware = True
        d = self._driver()
        self.assertFalse(d.live_commands_supported)
        self.assertIs(d.link_status()["live_cmds"], False)

    def test_rejected_live_command_marks_unsupported(self):
        d = self._driver()
        FakeSerial.old_firmware = True  # e.g. reflashed down; probe was stale
        d.all_on_no_halt()
        self.assertFalse(d.live_commands_supported)

    def test_no_pyserial_reports_mock(self):
        with patch.object(gpio_driver, "_HAS_SERIAL", False):
            d = self._driver()
            self.assertEqual(d.link_status()["state"], "mock")


class TestModeTransitionsOnOldFirmware(unittest.TestCase):
    """Auto-follow's seamless discharge <-> charge path must not depend on
    E / Y when the firmware lacks them."""

    def _controller(self, live):
        gpio = MagicMock()
        gpio.live_commands_supported = live
        gpio.get_sensor_data.return_value = {}
        gpio.sensor_data_fresh.return_value = False
        engine = MagicMock()
        mc = ModeController(gpio, engine)
        self.addCleanup(mc._auto_follow.stop)
        self.addCleanup(mc._schedule_monitor.stop_thread)
        mc.set_mode("discharge")
        gpio.reset_mock()
        engine.reset_mock()
        return mc, gpio, engine

    def test_live_firmware_uses_live_commands(self):
        mc, gpio, engine = self._controller(live=True)
        mc._set_mode_internal(Mode.CHARGE)
        engine.resume_live.assert_called_once()
        engine.resume.assert_not_called()
        mc._set_mode_internal(Mode.DISCHARGE)
        gpio.all_on_no_halt.assert_called_once()
        gpio.all_on.assert_not_called()

    def test_old_firmware_falls_back(self):
        mc, gpio, engine = self._controller(live=False)
        mc._set_mode_internal(Mode.CHARGE)
        engine.resume.assert_called_once()
        engine.resume_live.assert_not_called()
        mc._set_mode_internal(Mode.DISCHARGE)
        gpio.all_on.assert_called_once()
        gpio.all_on_no_halt.assert_not_called()
        gpio.program_sequence_live.assert_not_called()
        # Never the all-off / dead-time path.
        gpio.all_off.assert_not_called()

    def test_reapply_after_reconnect(self):
        mc, gpio, engine = self._controller(live=False)
        mc._reapply_mode()
        gpio.all_on.assert_called_once()
        mc._set_mode_internal(Mode.CHARGE)
        engine.reset_mock()
        mc._reapply_mode()
        engine.resume.assert_called_once()


class TestAutoFollowHoldsOnStaleData(unittest.TestCase):

    def test_tick_skipped_while_stale(self):
        fresh = {"v": False}
        set_mode = MagicMock()
        sensor = {
            "P1": {"voltage": 3.7, "current": 0.5},
            "P2": {"voltage": 0.0, "current": 0.0},
            "N1": {"voltage": 3.7, "current": 0.5},
            "N2": {"voltage": 0.0, "current": 0.0},
        }
        af = AutoFollow(lambda: sensor, set_mode,
                        is_data_fresh_fn=lambda: fresh["v"])
        af.set_enabled(True)
        set_mode.reset_mock()
        for _ in range(5):
            af._tick()
        set_mode.assert_not_called()
        self.assertFalse(af.active)
        fresh["v"] = True
        for _ in range(5):
            af._tick()
        set_mode.assert_called_with("charge")


if __name__ == "__main__":
    unittest.main()
