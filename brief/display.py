import datetime as dt

from rich.align import Align
from rich.console import Console, Group
from rich.padding import Padding
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from . import oura
from .storage import STATE, config, load

GOLD = '#d7a168'
GREEN = '#91b8a2'
INK = '#e6e1d3'
MUTED = '#91a09b'
LINE = '#334b43'


def sample_data(today):
    day = today.isoformat()
    yesterday = (today-dt.timedelta(days=1)).isoformat()
    return {
        'daily_sleep': [{'day': day, 'score': 86}],
        'daily_readiness': [{'day': (today-dt.timedelta(days=6-i)).isoformat(), 'score': v}
                            for i, v in enumerate([74, 79, 77, 82, 78, 84, 88])],
        'sleep': [{'day': day, 'type': 'long_sleep', 'total_sleep_duration': 27900,
                   'deep_sleep_duration': 5520, 'rem_sleep_duration': 6600,
                   'light_sleep_duration': 15780, 'efficiency': 93,
                   'average_hrv': 52, 'lowest_heart_rate': 48,
                   'bedtime_start': yesterday+'T23:05:00+13:00',
                   'bedtime_end': day+'T07:25:00+13:00'}],
        'daily_activity': [{'day': yesterday, 'score': 81, 'steps': 8432,
                            'active_calories': 426}],
        'fetched_at': dt.datetime.now().astimezone().isoformat(),
    }


def duration(seconds):
    if seconds is None:
        return '—'
    minutes = round(seconds / 60)
    return f'{minutes//60}h {minutes%60:02d}m'


def numeric(value, suffix=''):
    return '—' if value is None else f'{value:g}{suffix}'


def day_label(day, today):
    if not day:
        return 'No data yet'
    return 'Today' if day == today else f'Last available · {day}'


def score_card(label, record, today, caption=None):
    score = record.get('score')
    color = GREEN if score is not None and score >= 70 else GOLD
    text = Text()
    text.append(label.upper()+'\n', style=f'bold {MUTED}')
    text.append('\n'+('—' if score is None else str(score)), style=f'bold {color}')
    text.append(' / 100\n', style=MUTED)
    filled = round(score/100*18) if score is not None else 0
    text.append('━'*filled, style=color)
    text.append('━'*(18-filled)+'\n', style=LINE)
    text.append(caption or day_label(record.get('day'), today), style=MUTED)
    return Padding(text, (1, 2))


def dashboard(data, error='', demo=False, now=None):
    now = now or dt.datetime.now().astimezone()
    today = now.date().isoformat()
    settings = config()
    sleep_score = oura.latest(data.get('daily_sleep', []), today)
    readiness = oura.latest(data.get('daily_readiness', []), today)
    sleep = oura.main_sleep(data.get('sleep', []), sleep_score.get('day', today))
    yesterday = (now.date()-dt.timedelta(days=1)).isoformat()
    activity = oura.latest(data.get('daily_activity', []), yesterday, exact=True)

    sun = Text('        ·\n    \\   |   /\n     .─────.\n  ──(       )──\n ━━━━━━━━━━━━━━━\n    M O R N I N G', style=GOLD)
    header = Table.grid(expand=True, padding=(0, 3))
    header.add_column(width=20)
    header.add_column(ratio=1)
    greeting = Text('M O R N I N G   B R I E F\n\n', style=f'bold {GOLD}')
    greeting.append(f'Hello, {settings["name"]}.\n', style=f'bold {INK}')
    greeting.append(now.strftime('%A, %d %B  •  %H:%M %Z')+'\n', style=MUTED)
    greeting.append('Your day, at a glance.', style=INK)
    header.add_row(sun, greeting)

    cards = Table.grid(expand=True, padding=(0, 1))
    for _ in range(3):
        cards.add_column(ratio=1)
    cards.add_row(score_card('Sleep', sleep_score, today), score_card('Readiness', readiness, today),
                  score_card('Activity', activity, today,
                             'Yesterday' if activity.get('day') == yesterday else None))

    detail = Table.grid(expand=True, padding=(0, 2))
    detail.add_column(style=MUTED)
    detail.add_column(style=INK, justify='right')
    detail.add_column(style=MUTED)
    detail.add_column(style=INK, justify='right')
    detail.add_row('Time asleep', duration(sleep.get('total_sleep_duration')),
                   'Sleep efficiency', numeric(sleep.get('efficiency'), '%'))
    detail.add_row('Deep sleep', duration(sleep.get('deep_sleep_duration')),
                   'Average HRV', numeric(sleep.get('average_hrv'), ' ms'))
    detail.add_row('REM sleep', duration(sleep.get('rem_sleep_duration')),
                   'Lowest heart rate', numeric(sleep.get('lowest_heart_rate'), ' bpm'))
    detail.add_row('Yesterday’s steps', f'{activity["steps"]:,}' if activity.get('steps') is not None else '—',
                   'Active energy', numeric(activity.get('active_calories'), ' kcal'))
    stages = Text('\n')
    total = sleep.get('total_sleep_duration') or 0
    for field, label, color in [('deep_sleep_duration', 'Deep', '#719f99'),
                                 ('rem_sleep_duration', 'REM', GOLD),
                                 ('light_sleep_duration', 'Light', '#557365')]:
        value = sleep.get(field)
        if value is not None and total:
            stages.append('━'*max(1, round(value/total*54)), style=color)
    stages.append('\n', style=MUTED)
    stages.append('DEEP  ', style='#719f99')
    stages.append('   REM  ', style=GOLD)
    stages.append('   LIGHT', style='#557365')
    details = Group(Rule(Text('N I G H T   N O T E S', style=GREEN), style=LINE),
                    Padding(Group(detail, stages), (1, 2)),
                    Text(day_label(sleep.get('day'), today), style=MUTED, justify='right'))

    trend = Text('READINESS  ', style=MUTED)
    values = {r.get('day'): r.get('score') for r in data.get('daily_readiness', [])}
    for i in range(6, -1, -1):
        value = values.get((now.date()-dt.timedelta(days=i)).isoformat())
        trend.append('·' if value is None else '▁▂▃▄▅▆▇█'[min(7, int(value/100*8))], style=GREEN)
        trend.append(' ')
    trend.append('  past 7 days', style=MUTED)

    if demo:
        status = 'DESIGN PREVIEW · Sample values, not your health data. No API calls.'
    elif error:
        status = error
    elif not oura.connected():
        status = 'Connect Oura to bring this to life. Activate membership, then run morning-brief connect.'
    elif not oura.today_ready(data, today):
        status = 'Waiting for today’s sleep/readiness in Oura Cloud. Older records retain their dates above.'
    else:
        status = 'Today’s sleep and readiness are available in Oura Cloud.'
    fetched = data.get('fetched_at')
    if fetched and not demo:
        try:
            stamp = dt.datetime.fromisoformat(fetched).astimezone().strftime('%d %b %H:%M')
            status += f'\nLast successful API fetch: {stamp}.'
        except ValueError:
            pass
    morning = load(STATE/'morning.json')
    if not demo and morning.get('day') == today and morning.get('sync_status'):
        status += '\n'+morning['sync_status']
    next_up = Text('W E A T H E R   ', style=MUTED)
    next_up.append('Not connected', style=LINE)
    next_up.append('      ·      I N B O X   ', style=MUTED)
    next_up.append('Not connected', style=LINE)
    footer = Text('Refresh: morning-brief show    Connect: morning-brief connect', style=MUTED)
    return Group(header, Text(''), cards, details, trend, Text(''),
                 Text(status, style=GOLD), Text(''),
                 next_up, Text(''), footer)


def show(demo=False, once=False, automatic=None):
    from .morning import acknowledge
    console = Console()
    data = sample_data(dt.date.today()) if demo else load(STATE/'oura-cache.json')
    error = ''
    if not demo and not automatic and oura.connected():
        try:
            data = oura.fetch()
        except oura.OuraError as exc:
            error = str(exc)
    console.print(Align.center(Padding(dashboard(data, error, demo), (1, 2)), width=112))
    if automatic:
        acknowledge(automatic)
