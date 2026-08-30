import time


def capture(interface: str, duration_seconds: float = 5.0, max_frames: int = 200) -> list[dict]:
    """Zeichnet für duration_seconds Sekunden CAN-Frames auf (blockierend)."""
    import can

    frames = []
    try:
        bus = can.interface.Bus(channel=interface, interface="socketcan")
    except OSError as exc:
        return [{"error": f"Konnte {interface} nicht öffnen: {exc}"}]

    end_time = time.time() + duration_seconds
    try:
        while time.time() < end_time and len(frames) < max_frames:
            msg = bus.recv(timeout=max(0.0, end_time - time.time()))
            if msg is None:
                continue
            frames.append(
                {
                    "id": f"0x{msg.arbitration_id:x}",
                    "dlc": msg.dlc,
                    "data": msg.data.hex(),
                    "timestamp": round(msg.timestamp, 3),
                }
            )
    finally:
        bus.shutdown()

    return frames
