import contextlib
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import tempfile

HOME = Path.home()
ROOT = HOME / '.local/share/morning-brief'
CONFIG = Path(os.environ.get('XDG_CONFIG_HOME', HOME / '.config')) / 'morning-brief'
STATE = Path(os.environ.get('XDG_STATE_HOME', HOME / '.local/state')) / 'morning-brief'
DEFAULTS = {'name': 'Oliver', 'boundary_hour': 3, 'sync_timeout': 240,
            'unlinked_sync_timeout': 45, 'poll_interval': 30, 'lookback_days': 7}


def load(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {} if default is None else default


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def config():
    return DEFAULTS | load(CONFIG / 'config.json')


def morning_day(now=None, boundary=3):
    """Local calendar boundary, including DST; no UTC/24-hour approximation."""
    now = now or dt.datetime.now().astimezone()
    return (now.date() - dt.timedelta(days=now.hour < boundary)).isoformat()


@contextlib.contextmanager
def locked(name, blocking=True):
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (STATE / name).open('a') as stream:
        os.chmod(stream.name, 0o600)
        fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield


def claim_day(day):
    """Caller holds run.lock. Persist before fetching, so crashes cannot repeat it."""
    state = load(STATE / 'morning.json')
    if state.get('day') != day:
        state = {'day': day, 'sync_attempted': False, 'shown': False}
    first = not state.get('sync_attempted')
    if first:
        state['sync_attempted'] = True
        state['started_at'] = dt.datetime.now().astimezone().isoformat()
        state['sync_status'] = 'A previous attempt was interrupted; no second automatic update today.'
        save(STATE / 'morning.json', state)
    return state, first
