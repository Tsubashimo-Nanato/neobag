"""Opt-in Linux terminal-driver check: keyboard record and external SIGINT save."""
from __future__ import annotations

import argparse
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import sqlite3
import struct
import subprocess
import sys
import termios
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, help="Test an installed neobag launcher instead of this Python environment")
    args = parser.parse_args()
    if not os.environ.get("ROS_DOMAIN_ID") or os.environ["ROS_DOMAIN_ID"] == "0":
        raise SystemExit("Set an unused nonzero ROS_DOMAIN_ID.")
    evidence = Path("evidence")
    name = datetime.now().strftime("terminal_%Y%m%d_%H%M%S")
    bag = evidence / "bags" / name
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 36, 120, 0, 0))
    env = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor")
    command = [str(args.executable.expanduser().resolve())] if args.executable else [sys.executable, "-m", "neobag"]
    proc = subprocess.Popen([*command, "--output", str(bag.parent.resolve()), "--name", name],
                            stdin=slave, stdout=slave, stderr=slave, start_new_session=True, env=env)
    os.close(slave)
    transcript = bytearray()

    def collect(seconds):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            if select.select([master], [], [], min(0.1, max(0, until-time.monotonic())))[0]:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    break
                if not data:
                    break
                transcript.extend(data)

    try:
        collect(4)
        assert proc.poll() is None, transcript[-4000:]
        # Initial focus is search. Tab to category, then All, Enter selects.
        os.write(master, b"\t\t\r")
        collect(0.7)
        os.write(master, b"\x13")  # Ctrl+S: start
        collect(4)
        assert bag.is_dir(), "Keyboard start did not produce bag output"
        os.kill(proc.pid, signal.SIGINT)
        collect(8)
        proc.wait(timeout=10)
        assert proc.returncode == 0, proc.returncode
        assert (bag / "metadata.yaml").is_file(), "SIGINT did not finalize metadata"
        with sqlite3.connect(str(next(bag.glob("*.db3")))) as db:
            count = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        result = {"domain": os.environ["ROS_DOMAIN_ID"], "bag": str(bag.resolve()), "command": command,
                  "terminal": "Linux PTY 120x36", "selection": "Tab/Tab/Enter -> All",
                  "start": "Ctrl+S", "stop": "external SIGINT", "exit_code": proc.returncode,
                  "metadata_exists": True, "messages": count}
        (evidence / "terminal_smoke.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, indent=2))
    finally:
        if proc.poll() is None:
            os.kill(proc.pid, signal.SIGTERM)
            collect(10)
            proc.wait(timeout=35)
        os.close(master)
        (evidence / f"{name}.ansi").write_bytes(transcript)


if __name__ == "__main__":
    main()
