"""Capture an interactive CLI without redirecting its terminal descriptors to pipes."""
import errno
import os
import pty
import select
import signal
import struct
import subprocess
import termios
import fcntl
import time


def run_terminal(argv, cwd=None, timeout=120, stop_when=None):
    master, slave = pty.openpty()
    process = None
    output = bytearray()
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 200, 0, 0))
        process = subprocess.Popen(argv, cwd=cwd, stdin=slave, stdout=slave,
                                   stderr=slave, start_new_session=True)
        os.close(slave)
        slave = None
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, timeout, output=bytes(output))
            ready, _, _ = select.select([master], [], [], min(remaining, 0.1))
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
                    chunk = b''  # Linux PTY end-of-file; macOS returns empty bytes.
                if not chunk:
                    break
                output.extend(chunk)
                if stop_when and stop_when(output.decode(errors='replace')):
                    return subprocess.CompletedProcess(argv, 0, output.decode(errors='replace'), '')
                if len(output) > 16 * 1024 * 1024:
                    raise subprocess.SubprocessError('terminal output exceeded 16 MB')
            elif process.poll() is not None:
                break
        try:
            code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise subprocess.TimeoutExpired(argv, timeout, output=bytes(output))
        return subprocess.CompletedProcess(argv, code, output.decode(errors='replace'), '')
    except OSError as error:
        if process is None:
            raise  # Exec failed: no cloud side effect is possible.
        raise subprocess.SubprocessError('terminal output could not be captured') from error
    finally:
        if process is not None:
            # Stop the local CLI and children on timeout/interruption; never retry.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        os.close(master)
        if slave is not None:
            os.close(slave)
