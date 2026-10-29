"""Read-only Telegram analytics. No strategy or account mutations."""
import csv
import io
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

VERSION = '2.1 · Dashboard'
PERIODS = {'ALL': 'Всё время', 'TODAY': 'Сегодня (UTC)', '7D': '7 дней', '30D': '30 дней'}
MODES = {'15m': 'Скальпинг', '1h': 'Интрадей', '4h': 'Свинг'}


def stamp(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime('%d.%m %H:%M UTC') if ms else '—'


def select(trades, period='ALL', now=None):
    now = time.time() if now is None else now
    start = {'ALL': 0, 'TODAY': now - now % 86400, '7D': now - 7*86400, '30D': now - 30*86400}[period] * 1000
    return sorted((t for t in trades if start <= t['exit_ts'] <= now*1000), key=lambda t: (t['exit_ts'], t['id']))


def metrics(trades):
    values = [float(t['net_pnl']) for t in trades]
    wins, losses = [x for x in values if x > 0], [x for x in values if x < 0]
    streak = maximum = 0
    for x in values:
        streak = streak + 1 if x < 0 else 0
        maximum = max(maximum, streak)
    return dict(n=len(values), net=sum(values), winrate=100*len(wins)/len(values) if values else 0,
                pf=f'{sum(wins)/-sum(losses):.2f}' if losses else ('∞ (нет убытков)' if wins else '—'),
                avg=sum(values)/len(values) if values else 0,
                avg_win=sum(wins)/len(wins) if wins else 0,
                avg_loss=sum(losses)/len(losses) if losses else 0,
                streak=maximum, current_streak=streak,
                fees=sum(t['fees'] for t in trades), funding=sum(t['funding_paid'] for t in trades))


def analytics(trades, period='ALL', now=None):
    rows = select(trades, period, now)
    m = metrics(rows)
    lines = ['АНАЛИТИКА  /  '+PERIODS[period], 'Модельный счёт · USDT', '',
             f'Закрытых позиций: {m["n"]}', f'Чистый результат: {m["net"]:+.2f}',
             f'Прибыльных: {m["winrate"]:.1f}%' if rows else 'Прибыльных: —',
             f'Profit Factor: {m["pf"]}', f'Средний результат: {m["avg"]:+.2f}',
             f'Средняя прибыль / убыток: {m["avg_win"]:+.2f} / {m["avg_loss"]:+.2f}',
             f'Серия убытков, максимум / в конце периода: {m["streak"]} / {m["current_streak"]}',
             f'Комиссии: {m["fees"]:.2f} · Funding: {m["funding"]:+.2f}', '', 'ПО РЕЖИМАМ']
    for tf, name in MODES.items():
        group = metrics([t for t in rows if t['tf'] == tf])
        lines.append(f'{name}: {group["net"]:+.2f} · {group["n"]} сделок · PF {group["pf"]}')
    lines += ['', 'ПО НАПРАВЛЕНИЯМ']
    for side in ('LONG', 'SHORT'):
        group = metrics([t for t in rows if t['side'] == side])
        lines.append(f'{side}: {group["net"]:+.2f} · {group["n"]} сделок')
    lines += ['', 'Период — по времени полного закрытия. Частичные выходы объединены в одну позицию. Комиссии и funding уже учтены в результате; проскальзывание — в ценах исполнения.']
    return '\n'.join(lines)


def breakdown(trades, period='ALL', now=None):
    rows = select(trades, period, now)
    groups = defaultdict(list)
    for t in rows:
        groups[t['symbol']].append(t)
    lines = ['ДЕТАЛИ  /  '+PERIODS[period], 'Модельный счёт · USDT', '', 'ПО МОНЕТАМ']
    for symbol, group in sorted(groups.items(), key=lambda p: sum(t['net_pnl'] for t in p[1]), reverse=True):
        m = metrics(group)
        lines.append(f'{symbol}: {m["net"]:+.2f} · n={m["n"]} · PF {m["pf"]}')
    if not rows:
        lines.append('Нет закрытых позиций.')
    lines += ['', 'ПОСЛЕДНИЙ ВЫХОД']
    for reason, count in sorted(Counter(t['exit_reason'] for t in rows).items()):
        lines.append(f'{reason}: {count}')
    lines += [f'Достигли TP1: {sum(bool(t["tp1_hit"]) for t in rows)} из {len(rows)}', '', 'ПО ДНЯМ ЗАКРЫТИЯ (UTC)']
    days = defaultdict(float)
    for t in rows:
        days[datetime.fromtimestamp(t['exit_ts']/1000, timezone.utc).strftime('%Y-%m-%d')] += t['net_pnl']
    lines += [f'{day}: {net:+.2f}' for day, net in sorted(days.items())[-14:]]
    lines.append('Показаны последние 14 дней с закрытиями; весь журнал доступен в CSV.')
    return '\n'.join(lines)


def overview(state, cfg, equity):
    positions = list(state['positions'].values())
    open_realized = sum(p['gross_pnl']-p['fees']-p['funding_paid'] for p in positions)
    unrealized = equity-state['balance']
    dd = max(0, 1-equity/max(state['peak_equity'], 1e-12))*100
    risk = sum(p['planned_risk'] for p in positions)
    return '\n'.join(['AI CRYPTO  /  ОБЗОР', f'v{VERSION} · Симуляция · {cfg.market.upper()}', '',
        f'Капитал: {equity:,.2f} USDT', f'Старт: {cfg.initial_equity:,.2f} USDT',
        f'С начала: {(equity/cfg.initial_equity-1)*100:+.2f}%', '',
        f'Полностью закрытые позиции: {state["realized_net"]:+.2f} USDT',
        f'Учтённый результат ещё открытых: {open_realized:+.2f} USDT',
        f'Оценка закрытия остатка: {unrealized:+.2f} USDT', '',
        f'Просадка сейчас / максимум: {dd:.2f}% / {state["max_drawdown"]*100:.2f}%',
        f'Исходный риск открытых: {risk:.2f} USDT',
        f'Лимит риска: {cfg.max_total_risk*100:.2f}% капитала',
        f'Открыто: {len(positions)} · Ожидают входа: {len(state["pending"])}', '',
        'Сигналы: '+('включены' if state['enabled'] else 'пауза'),
        'Скальпинг: '+('включён' if state['scalp_enabled'] else 'пауза'),
        'Защитная пауза: '+(', '.join(x for x, flag in [('дневной лимит', state['day_paused']), ('просадка', state['dd_paused'])] if flag) or 'нет'), '',
        'Капитал — оценка по закрытым 1m свечам с расходами на выход. Просадка — по наблюдениям. Исходный риск не учитывает перенос стопа и не гарантирует предел убытка.'])


def ledger_csv(trades):
    stream = io.StringIO(newline='')
    fields = ['id','symbol','tf','side','entry_ts','exit_ts','entry','qty','net_pnl','net_r','fees','funding_paid','tp1_hit','exit_reason','ambiguous_bars']
    writer = csv.DictWriter(stream, fields, extrasaction='ignore')
    writer.writeheader()
    for row in sorted(trades, key=lambda t: (t['exit_ts'], t['id'])):
        # Prevent spreadsheet formula evaluation for externally sourced strings.
        writer.writerow({k: ("'"+v if isinstance(v, str) and v.startswith(('=', '+', '-', '@')) else v) for k,v in row.items() if k in fields})
    return ('\ufeff'+stream.getvalue()).encode('utf-8')
