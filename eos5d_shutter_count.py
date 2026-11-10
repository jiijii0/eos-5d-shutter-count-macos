#!/usr/bin/env python3
"""Canon EOS 5D classic: read the shutter count over USB on macOS.

macOS port of eos5d_shutter_count_auto2.py by Blianahui
(https://github.com/Blianahui/EOS_5D_Shutter_Count), GPL-3.0.

What differs from the Windows version:
    * No driver switching. libusb can talk to the camera directly on macOS;
      the whole SetupAPI / WinUSB layer is gone.
    * macOS' own PTP daemon (ptpcamerad, or PTPCamera on old systems) grabs
      PTP cameras as soon as they are plugged in. It is stopped while the
      camera is being opened; launchd restarts it on demand afterwards, so
      Photos / Image Capture work again once the camera is back in PTP mode.
    * No administrator rights needed (ptpcamerad runs as the logged-in user).

The Canon DCP/PTP exchange and the counter decoding are unchanged.

Requirements:
    python3 -m pip install pyusb libusb-package

Run:
    python3 eos5d_shutter_count.py        # add -v for raw packet output
"""

from __future__ import annotations

import struct
import subprocess
import sys
import threading
import time
import traceback

try:
    import usb.core
    import usb.util
    import usb.backend.libusb1
except ImportError as exc:
    raise SystemExit(
        "Missing USB Python package(s). Run:\n"
        "  python3 -m pip install pyusb libusb-package\n"
        f"Details: {exc}"
    )

try:
    import libusb_package
except ImportError:  # fall back to a system libusb (e.g. `brew install libusb`)
    libusb_package = None

VID = 0x04A9
EOS5D_PID, DCP_PID = 0x3102, 0x3086

# ============================================================================
# macOS: keep the system PTP daemon away from the camera
# ============================================================================

# ptpcamerad: macOS 10.15+ (LaunchAgent com.apple.ptpcamerad, runs as the user)
# PTPCamera:  older macOS (ImageCaptureCore helper app)
PTP_DAEMONS = ("ptpcamerad", "PTPCamera")


def ptp_daemon_running():
    """Name of a running macOS PTP daemon, or None."""
    for name in PTP_DAEMONS:
        r = subprocess.run(["pgrep", "-x", name], capture_output=True)
        if r.returncode == 0:
            return name
    return None


def stop_ptp_daemon(quiet=False):
    """Kill the macOS PTP daemon so libusb can claim the camera.

    It is a per-user LaunchAgent, so no root is needed. launchd restarts it the
    next time something (Photos, Image Capture, a PTP camera) asks for it.
    """
    killed = []
    for name in PTP_DAEMONS:
        r = subprocess.run(["pkill", "-9", "-x", name], capture_output=True)
        if r.returncode == 0:
            killed.append(name)
    if killed and not quiet:
        print(f"Stopped macOS camera daemon: {', '.join(killed)}", flush=True)
    return bool(killed)


# ============================================================================
# Canon EOS 5D PTP / DCP exchange (unchanged from the Windows version)
# ============================================================================

BULK_OUT, INT_IN = 0x02, 0x83
MAGIC = b"EVNTPROCEXE\x00"
REG_PATH = b"SERVER\\1.00\\CAMERA\\PROTO\\SERIAL_NUMBER"
WINDOW_BASE, CHUNK = 0x938, 12
MEMORY_KIND = 2
COUNT_CEILING = 10_000_000   # the original program rejects counts at or above this
REPLY_POLLS = 48             # 48 x 2 s, as in the original program
CLEANUP_POLLS = 3            # after an error: do not wait 96 s per clean-up command
VERBOSE = "-v" in sys.argv


def hx(b):
    return bytes(b).hex(" ")


def handshake_frame():
    return struct.pack("<IIIII", 0, 12, 1, 4, 0)


def register_frames(name):
    """Two frames per service name: registration (160 bytes) and commit (32)."""
    name = name.ljust(12, b"\x00")
    body = struct.pack("<I", 1) + name + struct.pack("<II", 0x84, 0) + REG_PATH.ljust(0x80, b"\x00")
    first = struct.pack("<II", 1, len(body)) + body
    body = struct.pack("<I", 3) + name + struct.pack("<II", 4, 0)
    return first, struct.pack("<II", 1, len(body)) + body


def request(kind, session, seq, payload=b""):
    inner = MAGIC + struct.pack("<IHHHI", 0, kind, session, seq, len(payload)) + payload
    return struct.pack("<II", 2, len(inner)) + inner


def open_session_frame(seq):
    return request(1, 0, seq)


def noarg_frame(session, seq, name):
    return request(5, session, seq, name.encode() + b"\x00" + struct.pack("<II", 0, 0))


def monread_frame(session, seq, address, length):
    p = b"MonReadAndGetData\x00" + struct.pack("<I", 4)
    for v in (length, MEMORY_KIND, address, length):
        p += struct.pack("<IIIII", 2, v, 0, 0, 0)
    return request(7, session, seq, p + struct.pack("<I", 0))


def parse_reply(pkt):
    """Decode an EVNTPROCEXE interrupt packet."""
    if len(pkt) < 0x2C or pkt[0x10:0x1C] != MAGIC:
        return None
    kind, subtype, session, seq, plen = struct.unpack_from("<HHHHI", pkt, 0x20)
    return {
        "type": kind, "subtype": subtype, "session": session, "seq": seq,
        "payload": pkt[0x2C:0x2C + plen],
    }


def decode(window):
    """MECHANICS.md: page = BE16 at 0x943, live = ring[page % 8]."""
    page = int.from_bytes(window[0x0B:0x0D], "big")
    ring = window[0x0D:0x15]
    live = ring[page % 8]
    return page, ring, live, page * 256 + live


# ------------------------------- USB layer ---------------------------------


def backend():
    b = None
    if libusb_package is not None:
        b = usb.backend.libusb1.get_backend(find_library=libusb_package.find_library)
    if b is None:
        b = usb.backend.libusb1.get_backend()  # system libusb, e.g. Homebrew
    if b is None:
        raise RuntimeError("Cannot load libusb-1.0. Run: python3 -m pip install libusb-package "
                           "(or: brew install libusb)")
    return b


BACKEND = None
_BACKEND_LOCK = threading.Lock()


def _backend():
    global BACKEND
    with _BACKEND_LOCK:  # the GUI polls from another thread
        if BACKEND is None:
            BACKEND = backend()
    return BACKEND


def find(pid):
    return usb.core.find(backend=_backend(), idVendor=VID, idProduct=pid)


def wait_for_usb(pid, seconds=25):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        dev = find(pid)
        if dev is not None:
            return dev
        time.sleep(0.25)
    return None


def camera_state():
    """Read-only look at the USB bus: ('service'|'normal'|'none'|'error', detail)."""
    try:
        if find(DCP_PID) is not None:
            return "service", ""
        if find(EOS5D_PID) is not None:
            daemon = ptp_daemon_running()
            return "normal", (f"in use by macOS {daemon}" if daemon else "free")
        return "none", ""
    except Exception as exc:
        return "error", str(exc)


def open_claimed(pid, seconds=20):
    """Open the device through libusb and claim interface 0.

    On macOS the system PTP daemon may hold the camera; stop it and retry. It can
    respawn (e.g. if Photos is set to open on camera connect), so keep trying
    until we own the interface.
    """
    deadline = time.monotonic() + seconds
    last = "device not found"
    while True:
        dev = find(pid)
        if dev is not None:
            try:
                try:
                    dev.get_active_configuration()
                except usb.core.USBError:
                    dev.set_configuration()
                usb.util.claim_interface(dev, 0)
                return dev
            except (usb.core.USBError, NotImplementedError) as exc:
                last = str(exc)
                usb.util.dispose_resources(dev)
                stop_ptp_daemon(quiet=True)
        if time.monotonic() > deadline:
            hint = ""
            if ptp_daemon_running():
                hint = ("\nmacOS' camera daemon keeps grabbing the camera. Quit Photos, "
                        "Image Capture, Lightroom etc. and try again.")
            raise RuntimeError(f"Cannot open 04A9:{pid:04X} through libusb: {last}{hint}")
        time.sleep(0.5)


PTP_OK, PTP_SESSION_ALREADY_OPEN = 0x2001, 0x201E


def _drain(dev, ep=0x81):
    """Discard data a killed ptpcamerad may have left in the pipe."""
    for _ in range(8):
        try:
            dev.read(ep, 512, timeout=100)
        except usb.core.USBError:
            return


def _ptp(dev, code, tid, param):
    """One PTP operation with a single parameter and no data phase; returns response code."""
    dev.write(0x02, struct.pack("<IHHII", 16, 1, code, tid, param), timeout=5000)
    response = bytes(dev.read(0x81, 64, timeout=5000))
    if len(response) < 8:
        raise RuntimeError(f"Short PTP response to 0x{code:04X}: {hx(response)}")
    return struct.unpack_from("<H", response, 6)[0]


def switch_to_service_mode():
    """Send PTP 0x901F; return after the 3086 device appears on the bus."""
    dev = open_claimed(EOS5D_PID)
    print("Sending PTP service-mode command 0x901F...", flush=True)
    try:
        _drain(dev)
        tid = 0
        code = _ptp(dev, 0x1002, tid, 1)  # OpenSession
        if code == PTP_SESSION_ALREADY_OPEN:
            # ptpcamerad's session is still open in the camera (Windows avoids this
            # because the driver swap re-enumerates the device). Close and reopen.
            print("Camera still had a PTP session open; reopening it.", flush=True)
            tid += 1
            _ptp(dev, 0x1003, tid, 0)  # CloseSession
            tid += 1
            code = _ptp(dev, 0x1002, tid, 1)
        if code not in (PTP_OK, PTP_SESSION_ALREADY_OPEN):
            raise RuntimeError(f"OpenSession failed: 0x{code:04X}")
        try:
            dev.write(0x02, struct.pack("<IHHII", 16, 1, 0x901F, tid + 1, 3), timeout=5000)
            dev.read(0x81, 64, timeout=5000)
        except usb.core.USBError:
            pass  # the camera disconnects during the mode change
    finally:
        usb.util.dispose_resources(dev)

    print("Waiting for service-mode USB ID 04A9:3086...", flush=True)
    if wait_for_usb(DCP_PID, 25) is None:
        raise RuntimeError("Service-mode device 04A9:3086 did not appear on the USB bus.")


class Dcp:
    def __init__(self, dev):
        self.dev = dev
        self.seq = 0x40
        self.session = 0
        self.polls = REPLY_POLLS

    def status(self):
        st = bytes(self.dev.ctrl_transfer(0xC0, 0, 0, 0, 2, timeout=4000))
        if st != b"\x00\x00":
            raise RuntimeError(f"Camera status {hx(st)} (expected 00 00).")

    def send(self, frame):
        self.dev.ctrl_transfer(0x40, 0, 0x0E, 0, struct.pack("<I", len(frame)), timeout=3000)
        self.status()
        self.dev.write(BULK_OUT, frame, timeout=3000)

    def reply(self, seq, want_type):
        for _ in range(self.polls):
            try:
                pkt = bytes(self.dev.read(INT_IN, 64, timeout=2000))
            except usb.core.USBTimeoutError:
                continue
            if VERBOSE:
                print("   <", hx(pkt), flush=True)
            r = parse_reply(pkt)
            if r and r["seq"] == seq and r["type"] == want_type:
                return r
        raise RuntimeError("The camera did not answer.")

    def connect(self):
        self.dev.ctrl_transfer(0x40, 0, 0x10, 0, struct.pack("<I", 0), timeout=3000)
        self.status()
        self.send(handshake_frame())
        for name in (b"EVNTPROCEXE", b"MEMMNGR"):
            for frame in register_frames(name):
                self.send(frame)
        self.seq += 1
        self.send(open_session_frame(self.seq))
        r = self.reply(self.seq, 1)
        if len(r["payload"]) < 2:
            raise RuntimeError("No session id in the camera's answer.")
        self.session = struct.unpack_from("<H", r["payload"])[0]
        print(f"DCP session opened, id {self.session}.", flush=True)

    def call(self, name):
        self.seq += 1
        self.send(noarg_frame(self.session, self.seq, name))
        r = self.reply(self.seq, 2)
        status_code = int.from_bytes(r["payload"][:4], "little")
        if r["subtype"] != 1 or status_code != 0:
            raise RuntimeError(f"{name}: subtype {r['subtype']}, status 0x{status_code:X}.")

    def read(self, address, length):
        self.seq += 1
        self.send(monread_frame(self.session, self.seq, address, length))
        r = self.reply(self.seq, 2)
        p = r["payload"]
        if r["subtype"] != 3 or len(p) < 8:
            raise RuntimeError(f"Read 0x{address:X}: subtype {r['subtype']}, payload {hx(p)}.")
        status_code, n = struct.unpack_from("<II", p)
        data = p[8:8 + n]
        if status_code != 0 or len(data) != length:
            raise RuntimeError(f"Read 0x{address:X}: status 0x{status_code:X}, {len(data)} bytes.")
        return data

    def back_to_ptp(self):
        self.seq += 1
        try:
            self.send(noarg_frame(self.session, self.seq, "SetUSBToPTPMode"))
            print("Sent SetUSBToPTPMode.", flush=True)
        except usb.core.USBError as exc:
            # The camera may disconnect immediately after accepting this command.
            print(f"USB disconnected while returning to PTP mode: {exc}", flush=True)


# ============================================================================
# Main orchestration
# ============================================================================


def main():
    if sys.platform != "darwin":
        raise RuntimeError("This build is for macOS. Use the original Windows version on Windows.")

    _backend()
    print("Canon EOS 5D shutter-count reader (macOS)", flush=True)
    print(f"Python {sys.version.split()[0]}; libusb backend loaded", flush=True)

    dcp_dev = None
    dcp = None
    window = None
    mon_open = False
    back_attempted = False
    primary_error = None

    try:
        if find(DCP_PID) is not None:
            print("Camera is already in service mode (04A9:3086).", flush=True)
        else:
            if find(EOS5D_PID) is None:
                raise RuntimeError("Canon EOS 5D 04A9:3102 was not found on the USB bus.")
            print("Canon EOS 5D found (04A9:3102).", flush=True)
            stop_ptp_daemon()
            switch_to_service_mode()

        dcp_dev = open_claimed(DCP_PID)
        print("Service mode 04A9:3086 connected through libusb.", flush=True)
        dcp = Dcp(dcp_dev)

        dcp.connect()
        dcp.call("MonOpen")
        mon_open = True
        window = dcp.read(WINDOW_BASE, CHUNK) + dcp.read(WINDOW_BASE + CHUNK, CHUNK)
        dcp.call("MonClose")
        mon_open = False
        dcp.back_to_ptp()
        back_attempted = True

    except Exception as exc:
        primary_error = exc
        print(f"\nERROR: {exc}", flush=True)
        if VERBOSE:
            traceback.print_exc()

    finally:
        # Best-effort service cleanup, even if one of the reads failed.
        if dcp is not None and dcp.session and not back_attempted:
            dcp.polls = CLEANUP_POLLS
            if mon_open:
                try:
                    print("Attempting MonClose after an error...", flush=True)
                    dcp.call("MonClose")
                except Exception as exc:
                    print(f"MonClose cleanup failed: {exc}", flush=True)
            try:
                print("Attempting to return camera to PTP mode...", flush=True)
                dcp.back_to_ptp()
                back_attempted = True
            except Exception as exc:
                print(f"SetUSBToPTPMode cleanup failed: {exc}", flush=True)

        if dcp_dev is not None:
            try:
                usb.util.dispose_resources(dcp_dev)
            except Exception as exc:
                print(f"USB cleanup warning: {exc}", flush=True)

        # Nothing to restore on macOS: once the camera is back as 3102, launchd
        # brings ptpcamerad back and Photos / Image Capture see it as usual.
        if back_attempted:
            print("\nWaiting for the camera to return to normal mode...", flush=True)
            if wait_for_usb(EOS5D_PID, 15) is not None:
                print("Normal camera ID 04A9:3102 detected again.", flush=True)
            else:
                print("WARNING: camera did not reappear as 04A9:3102. "
                      "Switch it off and on if needed.", flush=True)

    if primary_error is not None:
        raise primary_error

    if window is None:
        raise RuntimeError("No shutter-counter data was read.")

    page, ring, live, count = decode(window)
    print("\nRaw window 0x938..0x94F:", hx(window))
    print(f"page = {page}, slot = {page % 8}, live = 0x{live:02X}, ring = {hx(ring)}")
    if window in (b"\xff" * len(window), b"\x00" * len(window)):
        raise RuntimeError("The counter memory reads as blank (all FF or all 00); "
                           "no shutter count can be derived from it.")
    if count >= COUNT_CEILING:
        raise RuntimeError(f"Decoded {count}, which is not a believable shutter count "
                           f"(limit {COUNT_CEILING}); the data above is shown for reference.")
    print(f"\nSHUTTER COUNT: {count}")
    if live == 0xFF:
        print("(live byte is 0xFF: accurate to within 255)")


def _run():
    try:
        main()
        return 0
    except Exception as exc:
        print(f"\nFATAL: {exc}", flush=True)
        if VERBOSE:
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(_run())
