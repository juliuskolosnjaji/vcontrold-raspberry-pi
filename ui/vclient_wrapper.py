import subprocess


def run_vclient(host: str, port: str, command: str, timeout: int = 15) -> dict:
    """Führt einen vclient-Befehl aus und gibt {"ok", "output"} zurück."""
    try:
        result = subprocess.run(
            ["vclient", "-h", host, "-p", str(port), "-c", command],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = (result.stdout or "") + (result.stderr or "")
        return {"ok": result.returncode == 0, "output": output.strip()}
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": f"Timeout nach {timeout}s"}
    except FileNotFoundError:
        return {"ok": False, "output": "vclient nicht gefunden. Ist vcontrold installiert?"}
