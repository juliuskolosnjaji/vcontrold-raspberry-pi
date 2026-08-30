import subprocess


def service_status(name: str) -> dict:
    try:
        active = subprocess.run(
            ["systemctl", "is-active", name], capture_output=True, text=True, timeout=5
        ).stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        active = "unbekannt"
    return {"name": name, "state": active}


def restart_service(name: str, timeout: int = 15) -> dict:
    """Startet einen systemd-Dienst neu. Gibt {"ok", "detail"} zurück."""
    try:
        result = subprocess.run(
            ["systemctl", "restart", name], capture_output=True, text=True, timeout=timeout
        )
        if result.returncode == 0:
            return {"ok": True, "detail": ""}
        return {"ok": False, "detail": result.stderr.strip() or f"exit code {result.returncode}"}
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return {"ok": False, "detail": str(exc)}


def service_log(name: str, lines: int = 50) -> str:
    try:
        result = subprocess.run(
            ["journalctl", "-u", name, "-n", str(lines), "--no-pager"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() or result.stderr.strip() or "(keine Log-Einträge)"
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return f"Fehler beim Lesen des Logs: {exc}"


def can_link_status(interface: str) -> str:
    try:
        result = subprocess.run(
            ["ip", "-details", "-statistics", "link", "show", interface],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() or result.stderr.strip() or f"{interface} nicht gefunden"
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return f"Fehler: {exc}"


def mqtt_connectivity(host: str, port: int, username: str | None, password: str | None) -> dict:
    import paho.mqtt.client as mqtt

    result = {"ok": False, "detail": ""}

    def on_connect(client, userdata, flags, rc):
        result["ok"] = rc == 0
        result["detail"] = f"rc={rc}"
        client.disconnect()

    client = mqtt.Client()
    if username:
        client.username_pw_set(username, password or None)
    client.on_connect = on_connect
    try:
        import time

        client.connect(host, port, keepalive=5)
        client.loop_start()
        time.sleep(2)
        client.loop_stop()
    except Exception as exc:
        result["detail"] = str(exc)
    finally:
        try:
            client.disconnect()
        except Exception:
            pass
    return result
