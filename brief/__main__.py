import argparse
import os
import signal
import sys

from . import morning, oura
from .display import show
from .storage import STATE, config, load, morning_day


def main():
    parser = argparse.ArgumentParser(description='A quiet, once-a-morning terminal briefing.')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('open', help='Open your dashboard without starting Android')
    sub.add_parser('preview', help='Open a clearly labelled sample-data preview')
    sub.add_parser('connect', help='Connect your own Oura account with OAuth')
    sub.add_parser('fetch', help='Refresh the local API cache without starting Android')
    sub.add_parser('run', help='Run today’s once-only background sync and briefing')
    sub.add_parser('check', help='Check whether first-use conditions are met')
    sub.add_parser('status', help='Show setup and morning-trigger state')
    p = sub.add_parser('presence')
    p.add_argument('state', choices=['active', 'idle'])
    p = sub.add_parser('show')
    p.add_argument('--demo', action='store_true')
    p.add_argument('--once', action='store_true')
    p.add_argument('--automatic')
    p = sub.add_parser('terminal', help='Print the brief, then enter your normal interactive shell')
    p.add_argument('--demo', action='store_true')
    p.add_argument('--automatic')
    args = parser.parse_args()
    # A service stop must unwind the sync's finally block and release Bluetooth.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        if args.command == 'open':
            morning.open_window()
        elif args.command == 'preview':
            morning.open_window(demo=True)
        elif args.command == 'connect':
            oura.authorize()
        elif args.command == 'fetch':
            data = oura.fetch()
            print('Updated:', data['fetched_at'])
        elif args.command == 'run':
            morning.run()
        elif args.command == 'check':
            morning.maybe()
        elif args.command == 'presence':
            morning.presence(args.state == 'active')
        elif args.command == 'show':
            show(args.demo, args.once, args.automatic)
        elif args.command == 'terminal':
            show(args.demo, True, args.automatic)
            print()
            os.chdir(os.path.expanduser('~'))
            shell = os.environ.get('SHELL', '/bin/bash')
            os.execv(shell, [shell, '-i'])
        elif args.command == 'status':
            print('Oura connected:', oura.connected())
            print('Morning boundary:', f'{config()["boundary_hour"]:02d}:00 local time')
            print('Current morning:', morning_day(boundary=config()['boundary_hour']))
            print('State:', load(STATE/'morning.json'))
    except (oura.OuraError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


raise SystemExit(main())
