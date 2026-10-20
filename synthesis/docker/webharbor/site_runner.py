"""Supervisor for one Flask site; the control plane kills its process group on reset."""
import os
import signal
import subprocess
import sys


def main():
    site = sys.argv[1]
    port = int(sys.argv[2])

    try:
        os.setsid()
    except OSError:
        pass

    child = subprocess.Popen(
        [
            'python3',
            '-c',
            "from app import app; "
            f"app.run(host='127.0.0.1', port={port}, "
            "debug=False, use_reloader=False, threaded=True)",
        ],
        cwd=f'/opt/WebSyn/{site}',
    )

    def shutdown(signum, frame):
        try:
            child.kill()
        except ProcessLookupError:
            pass
        os._exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    rc = child.wait()
    sys.exit(rc)


if __name__ == '__main__':
    main()
