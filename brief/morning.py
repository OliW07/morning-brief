import datetime as dt
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time

from . import oura
from .storage import HOME, STATE, claim_day, config, load, locked, morning_day, save

COMMAND = str(HOME / '.local/bin/morning-brief')
ADB = str(HOME / '.local/share/oura-android/sdk/platform-tools/adb')


def command(*args, timeout=8):
    return subprocess.run(args, text=True, capture_output=True, timeout=timeout)


def desktop_available(require_activity=True):
    if not os.environ.get('WAYLAND_DISPLAY'):
        return False
    if require_activity and not load(STATE / 'presence.json', {'active': True}).get('active'):
        return False
    try:
        lids = list(Path('/proc/acpi/button/lid').glob('*/state'))
        if any('closed' in lid.read_text() for lid in lids):
            return False
        if command('pgrep', '-u', str(os.getuid()), '-x', 'hyprlock').returncode == 0:
            return False
        session = command('loginctl', 'show-user', str(os.getuid()), '-p', 'Display', '--value').stdout.strip()
        if not session:
            return False
        props = command('loginctl', 'show-session', session, '-p', 'Active', '-p', 'LockedHint').stdout
        if 'Active=yes' not in props or 'LockedHint=yes' in props:
            return False
        monitors = json.loads(command('hyprctl', 'monitors', '-j').stdout)
        return any(m.get('dpmsStatus') and not m.get('disabled') for m in monitors)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def eligible(now=None):
    now = now or dt.datetime.now().astimezone()
    settings = config()
    if now.hour < settings['boundary_hour']:
        return False
    state = load(STATE / 'morning.json')
    return not (state.get('day') == morning_day(now, settings['boundary_hour']) and state.get('shown'))


def maybe():
    if eligible() and desktop_available():
        subprocess.run(['systemctl', '--user', 'start', '--no-block', 'morning-brief.service'], check=True)


def presence(active):
    save(STATE / 'presence.json', {'active': active})
    if active:
        maybe()


class Emulator:
    """Close only the session we own; never close an existing user session."""
    def __init__(self):
        self.process = None
        self.directory = None
        self.control = None

    def start(self):
        if command(ADB, '-s', 'emulator-5580', 'get-state').stdout.strip() == 'device':
            if command('systemctl', 'is-active', 'oura-bluetooth.service').stdout.strip() != 'active':
                raise RuntimeError('The open Oura window has already returned Bluetooth to Fedora. Close it before the next sync; reopening Oura starts a new sync window.')
            result = command(ADB, '-s', 'emulator-5580', 'shell',
                             'am start -W -n com.ouraring.oura/.launcher.LauncherActivity', timeout=20)
            if result.returncode:
                raise RuntimeError('The existing Android session is not ready for Oura.')
            return
        self.directory = tempfile.TemporaryDirectory(prefix='morning-brief-', dir=os.environ.get('XDG_RUNTIME_DIR', '/tmp/opencode'))
        self.control = Path(self.directory.name) / 'control'
        env = dict(os.environ, MORNING_BRIEF_CONTROL=str(self.control))
        self.process = subprocess.Popen([str(HOME / '.local/bin/oura-android'), '--background'],
                                        env=env, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            if self.control.with_suffix('.ready').exists():
                return
            if self.process.poll() is not None:
                raise RuntimeError('Oura could not start silently. See ~/.local/share/oura-android/launcher.log.')
            time.sleep(1)
        raise RuntimeError('Android startup timed out.')

    def close(self):
        if self.process and self.process.poll() is None:
            try:
                # Signal the shell, not flock or the emulator, so cleanup runs.
                pid = int(self.control.read_text())
                os.kill(pid, signal.SIGTERM)
                self.process.wait(timeout=50)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                raise RuntimeError('Oura cleanup did not finish; Bluetooth may still be in use.') from None
        if self.directory:
            self.directory.cleanup()


def sync(day):
    emulator = Emulator()
    try:
        emulator.start()
        linked = oura.connected()
        settings = config()
        limit = settings['sync_timeout'] if linked else settings['unlinked_sync_timeout']
        deadline = time.monotonic() + limit
        error = ''
        while time.monotonic() < deadline:
            if linked:
                try:
                    data = oura.fetch()
                    if oura.today_ready(data, day):
                        return 'Today’s sleep and readiness are available in Oura Cloud.'
                except oura.OuraError as exc:
                    error = str(exc)
                    if 'denied access' in error or 'not connected' in error or 'authorization' in error.lower():
                        return error + ' Cloud sync was not verified.'
            time.sleep(min(settings['poll_interval'], max(0, deadline-time.monotonic())))
        if not linked:
            return 'Oura ran silently. Connect the API to verify cloud sync and show your data.'
        return error or 'Sync timed out: today’s complete sleep/readiness data has not reached Oura Cloud yet.'
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    finally:
        emulator.close()


def open_window(demo=False, automatic=None):
    args = ['kitty', '--class', 'morning-brief', '--title', 'Morning Brief',
            '--override', 'background=#131b19', '--override', 'foreground=#e6e1d3',
            '--override', 'background_opacity=0.35', '--override', 'dynamic_background_opacity=yes',
            '--override', 'hide_window_decorations=yes', '--override', 'window_padding_width=22',
            '--override', 'font_size=11.0',
            '--override', 'initial_window_width=112c', '--override', 'initial_window_height=44c',
            COMMAND, 'terminal']
    if demo:
        args.append('--demo')
    if automatic:
        args += ['--automatic', automatic]
    # The viewer must outlive the oneshot sync service's cgroup.
    return subprocess.Popen(['systemd-run', '--user', '--quiet', '--collect',
                             f'--unit=morning-brief-window-{time.time_ns()}', *args],
                            stdin=subprocess.DEVNULL, close_fds=True)


def run():
    if not eligible() or not desktop_available():
        return
    try:
        with locked('run.lock', blocking=False):
            day = morning_day(boundary=config()['boundary_hour'])
            state, first = claim_day(day)
            if state.get('shown'):
                return
            if first:
                try:
                    if oura.connected():
                        state['sync_status'] = sync(day)
                    else:
                        state['sync_status'] = 'Setup required: automatic Oura sync starts after you connect the API. Bluetooth was not taken over.'
                except RuntimeError as exc:
                    state['sync_status'] = str(exc)
                state['finished_at'] = dt.datetime.now().astimezone().isoformat()
                save(STATE / 'morning.json', state)
            if not desktop_available(require_activity=False):
                return  # Show on unlock without a second emulator launch.
            open_window(automatic=day)
            # Wait for first render to acknowledge; don't race a second trigger.
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if load(STATE / 'morning.json').get('shown'):
                    break
                time.sleep(0.5)
    except BlockingIOError:
        pass


def acknowledge(day):
    with locked('ack.lock'):
        state = load(STATE / 'morning.json')
        if state.get('day') == day:
            state['shown'] = True
            save(STATE / 'morning.json', state)
