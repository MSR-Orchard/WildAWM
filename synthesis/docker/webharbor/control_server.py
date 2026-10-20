"""WebSyn control plane on :8101 (overlays the upstream image's control server)."""
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flask import Flask, jsonify

SITES = [
    'allrecipes', 'amazon', 'apple', 'arxiv', 'bbc_news', 'booking',
    'github', 'google_flights', 'google_map', 'google_search',
    'huggingface', 'wolfram_alpha', 'cambridge_dictionary',
    'coursera', 'espn',
]
BASE_PORT = 40000
WEBSYN_DIR = '/opt/WebSyn'
PID_DIR = Path('/tmp/websyn_pids')
PID_DIR.mkdir(parents=True, exist_ok=True)

_site_locks = {s: threading.Lock() for s in SITES}
_site_procs: dict[str, subprocess.Popen] = {}

REAP_GRACE_SECS = 5.0
HEAL_INTERVAL_SECS = float(os.environ.get('WEBSYN_HEAL_INTERVAL_SECS', '10'))
HEAL_READY_TIMEOUT_SECS = float(os.environ.get('WEBSYN_HEAL_READY_TIMEOUT_SECS', '30'))

app = Flask(__name__)


def site_port(site: str) -> int:
    return BASE_PORT + SITES.index(site)


def pid_path(site: str) -> Path:
    return PID_DIR / f'{site}.pid'


def read_pid(site: str):
    try:
        return int(pid_path(site).read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def is_alive(pid) -> bool:
    if not pid:
        return False
    try:
        with open(f'/proc/{pid}/stat', 'rb') as f:
            data = f.read()
        rparen = data.rindex(b')')
        state = data[rparen + 2:rparen + 3]
        return state not in (b'Z', b'X')
    except (FileNotFoundError, ProcessLookupError, ValueError):
        return False


def _listening_inodes(port: int) -> set[str]:
    """Return site-owned socket inodes listening on ``port`` (ignores Docker DNS aliases)."""
    want = f'{port:04X}'
    inodes: set[str] = set()
    for table in ('/proc/net/tcp', '/proc/net/tcp6'):
        try:
            lines = Path(table).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) < 10:
                continue
            local, state, inode = parts[1], parts[3], parts[9]
            if state != '0A':  # TCP_LISTEN
                continue
            hex_addr, _, hex_port = local.rpartition(':')
            if hex_port.upper() == want:
                if table.endswith('tcp') and hex_addr.upper() not in {'00000000', '0100007F'}:
                    continue
                inodes.add(inode)
    return inodes


def _pids_for_socket_inodes(inodes: set[str]) -> set[int]:
    if not inodes:
        return set()
    pids: set[int] = set()
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        fd_dir = proc / 'fd'
        try:
            fds = list(fd_dir.iterdir())
        except (FileNotFoundError, PermissionError):
            continue
        for fd in fds:
            try:
                target = os.readlink(fd)
            except (FileNotFoundError, PermissionError, OSError):
                continue
            if target.startswith('socket:[') and target[8:-1] in inodes:
                pids.add(int(proc.name))
                break
    return pids


def _kill_processes_on_port(port: int, *, exclude: set[int] | None = None) -> None:
    """Kill any process still listening on ``port`` (uses /proc; the image has no lsof)."""
    exclude = exclude or set()
    for _ in range(2):
        pids = _pids_for_socket_inodes(_listening_inodes(port)) - exclude
        if not pids:
            return
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        deadline = time.time() + REAP_GRACE_SECS
        while time.time() < deadline:
            if not (_pids_for_socket_inodes(_listening_inodes(port)) - exclude):
                return
            time.sleep(0.05)


def _port_accepts_connections(port: int) -> bool:
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=0.3):
            return True
    except OSError:
        return False


def kill_site(site: str, reap_grace: float = REAP_GRACE_SECS):
    pid = read_pid(site)
    if pid:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc = _site_procs.pop(site, None)
        if proc is not None:
            try:
                proc.wait(timeout=reap_grace)
            except subprocess.TimeoutExpired:
                pass
        deadline = time.time() + reap_grace
        while time.time() < deadline:
            if not is_alive(pid):
                break
            time.sleep(0.01)

    _kill_processes_on_port(site_port(site), exclude={os.getpid()})


def reset_db(site: str):
    inst = Path(WEBSYN_DIR) / site / 'instance'
    seed = Path(WEBSYN_DIR) / site / 'instance_seed'
    if inst.exists():
        shutil.rmtree(inst)
    shutil.copytree(seed, inst)


def start_site(site: str) -> int:
    port = site_port(site)
    _kill_processes_on_port(port, exclude={os.getpid()})
    log = open(f'/tmp/websyn_{site}.log', 'a', buffering=1)
    log.write(f'\n[control-server] respawn at {time.strftime("%F %T")}\n')
    proc = subprocess.Popen(
        ['python3', '/opt/site_runner.py', site, str(port)],
        stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    _site_procs[site] = proc
    pid_path(site).write_text(str(proc.pid))
    return proc.pid


def wait_ready(site: str, timeout: float = 30.0) -> bool:
    port = site_port(site)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_accepts_connections(port):
            try:
                urllib.request.urlopen(f'http://127.0.0.1:{port}/', timeout=2).read(1)
                return True
            except Exception:
                pass
        time.sleep(0.3)
    return False


def site_ready(site: str) -> bool:
    pid = read_pid(site)
    return bool(is_alive(pid) and _port_accepts_connections(site_port(site)))


def reset_one(site: str) -> dict:
    with _site_locks[site]:
        kill_site(site)
        reset_db(site)
        pid = start_site(site)
        ready = wait_ready(site)
    return {'site': site, 'pid': pid, 'ready': ready}


def restart_one(site: str) -> dict:
    with _site_locks[site]:
        kill_site(site)
        pid = start_site(site)
        ready = wait_ready(site)
    return {'site': site, 'pid': pid, 'ready': ready,
            'note': 'restart only, DB not reset'}


def _repair_unready_site(site: str) -> dict:
    with _site_locks[site]:
        if site_ready(site):
            return {'site': site, 'ready': True, 'note': 'already ready'}
        kill_site(site)
        reset_db(site)
        pid = start_site(site)
        ready = wait_ready(site, timeout=HEAL_READY_TIMEOUT_SECS)
    return {'site': site, 'pid': pid, 'ready': ready, 'note': 'watchdog reset'}


def _healer_loop() -> None:
    while True:
        for site in SITES:
            if site_ready(site):
                continue
            result = _repair_unready_site(site)
            app.logger.warning('websyn healer repaired site=%s ready=%s note=%s',
                               site, result.get('ready'), result.get('note'))
        time.sleep(HEAL_INTERVAL_SECS)


@app.route('/health')
def health():
    sites = {}
    all_ok = True
    for s in SITES:
        pid = read_pid(s)
        alive = site_ready(s)
        sites[s] = {'pid': pid, 'alive': alive, 'port': site_port(s)}
        if not alive:
            all_ok = False
    return jsonify({'ok': all_ok, 'sites': sites}), (200 if all_ok else 503)


@app.route('/reset/<site>', methods=['POST'])
def reset_site(site):
    if site not in SITES:
        return jsonify({'error': f'unknown site: {site}',
                        'valid_sites': SITES}), 404
    result = reset_one(site)
    return jsonify(result), (200 if result['ready'] else 503)


@app.route('/reset-all', methods=['POST'])
def reset_all():
    with ThreadPoolExecutor(max_workers=len(SITES)) as ex:
        results = list(ex.map(reset_one, SITES))
    out = {r['site']: r for r in results}
    ok = all(r['ready'] for r in results)
    return jsonify({'ok': ok, 'sites': out}), (200 if ok else 503)


@app.route('/restart/<site>', methods=['POST'])
def restart_site(site):
    if site not in SITES:
        return jsonify({'error': f'unknown site: {site}'}), 404
    result = restart_one(site)
    return jsonify(result), (200 if result['ready'] else 503)


if __name__ == '__main__':
    port = int(os.environ.get('CONTROL_PORT', 8101))
    if '--port' in sys.argv:
        port = int(sys.argv[sys.argv.index('--port') + 1])
    threading.Thread(target=_healer_loop, name='websyn-healer',
                     daemon=True).start()
    app.run(host='0.0.0.0', port=port, debug=False,
            use_reloader=False, threaded=True)
