"""AI Crypto 3: causal 1h trend / 15m pullback / 5m trigger, LONG and SHORT.

Public Binance Spot data is a reference for manually checking futures ideas.
PAPER ONLY: no order API, exchange credentials, funding feed or profit promise.
Start: python bot.py. TELEGRAM_TOKEN remains in the existing environment.
ADMIN_CHAT_IDS defaults to the owner's previously supplied Telegram ID.
MARKET from old releases cannot change the public spot data source.
The new strategy uses paper_pullback_v3.sqlite3, leaving old DBs untouched.
DATA_DIR must be persistent to survive server replacements. Free Render storage
is ephemeral; this release does not purchase or silently provision resources.

Only closed candles are used. Fills are scheduled for a future full minute before
its opening price is observed. Closed 1m bars settle trades; stop wins an ambiguous
stop/target candle. One full exit at 2x stop distance, before execution costs.
Telegram menus edit one message. Signals, outcomes and requested CSV are separate.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import math
import os
from pathlib import Path
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any

# Telegram presentation: plain words first, technical terms only in the guide.
import csv
import io
import html
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone

BOT_VERSION = '3.0.0 · Тренд и откат'
STRATEGY_ID = 'pullback_1h_15m_5m_v1'
PERIODS = {'ALL': 'Всё время', 'TODAY': 'Сегодня · UTC', '7D': 'Последние 7 дней', '30D': 'Последние 30 дней'}
MODES = {'5m': 'Тренд → откат → вход · 1ч / 15м / 5м'}
SIDES = {'LONG': 'На рост · LONG', 'SHORT': 'На падение · SHORT'}
EXITS = {'SL': 'Защитный стоп', 'BE': 'Защитный стоп после первой цели',
         'TP': 'Цель достигнута',
         'TP1': 'Первая цель', 'TP2': 'Вторая цель', 'TIMEOUT': 'Истекло время ожидания',
         'END_OF_DATA': 'Конец данных проверки'}
REASONS = {
    'trend_unclear': 'Ждём понятное направление на часовом графике',
    'pullback_wait': 'Направление есть; ждём откат на 15 минутах',
    'structure_broken': 'Откат сломал предыдущее движение',
    'trigger_wait': 'Откат найден; ждём подтверждение на 5 минутах',
    'move_too_large': 'Цена уже слишком резко ушла вперёд',
    'stop_distance': 'Не подходит расстояние до защитного стопа',
    'costs_too_high': 'Расходы слишком велики относительно движения до стопа',
    'target_obstacle': 'До цели мешает ближайший разворотный уровень',
    'setup_used': 'Этот откат уже дал сигнал; ждём новую ситуацию',
    'correlated_budget': 'Лимит риска похожих движений уже занят',
    'insufficient_history': 'Пока недостаточно истории цен',
    'range_or_volume_anomaly': 'Слишком резкое движение цены или необычный объём торгов',
    'adx': 'Движение рынка пока недостаточно выражено',
    'rsi_overbought': 'Цена уже сильно выросла — вход может быть запоздалым',
    'rsi_oversold': 'Цена уже сильно упала — вход может быть запоздалым',
    'trend_conflict': 'Направление цены и средних значений не совпадает',
    'insufficient_independent_neighbors': 'Не найдено достаточно независимых похожих ситуаций',
    'neighbor_entry_gaps': 'В похожих ситуациях цена слишком далеко уходила до входа',
    'knn_net_outcomes': 'Похожие ситуации в прошлом дали слишком слабый результат после расходов',
    'spread': 'Слишком большая разница между ценой покупки и продажи',
    'manual_pause': 'Поиск приостановлен вручную',
    'account_loss_limit': 'Сработал защитный лимит потерь',
    'timeframe_loss_streak': 'Перерыв в этом режиме после серии убытков',
    'cooldown': 'Выдерживается перерыв после предыдущей сделки по этой монете',
    'symbol_already_open': 'По этой монете уже есть открытая сделка',
    'position_limit': 'Уже открыто максимально разрешённое число сделок',
    'direction_limit': 'Достаточно сделок в одном направлении',
    'symbol_pending': 'По этой монете уже ожидается вход',
    'stale_signal': 'Проверка завершилась слишком поздно для свежего входа',
    'entry_gap': 'Цена слишком далеко ушла от намеченного входа',
    'portfolio_budget': 'Недостаточно свободного лимита риска или объёма',
    'position_too_small': 'Расчётный размер сделки слишком мал',
    'missed_scheduled_entry': 'Нет данных для назначенной минуты входа',
    'spot_short_disabled': 'Старая спотовая проверка: вход на падение был запрещён',
    'no_signal': 'Условия входа не совпали',
}


def stamp(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime('%d.%m %H:%M UTC') if ms else 'ещё не было'


def select_trades(trades, period='ALL', now=None):
    now = time.time() if now is None else now
    start = {'ALL': 0, 'TODAY': now - now % 86400, '7D': now - 7*86400, '30D': now - 30*86400}[period] * 1000
    return sorted((t for t in trades if start <= t['exit_ts'] <= now*1000), key=lambda t: (t['exit_ts'], t['id']))


def trade_metrics(trades):
    pnl = [float(t['net_pnl']) for t in trades]
    wins, losses = [x for x in pnl if x > 0], [x for x in pnl if x < 0]
    streak = longest = 0
    for x in pnl:
        streak = streak + 1 if x < 0 else 0
        longest = max(longest, streak)
    return dict(n=len(pnl), net=sum(pnl), wins=len(wins), losses=len(losses), flat=sum(x == 0 for x in pnl),
                winrate=100*len(wins)/len(pnl) if pnl else 0,
                pf=sum(wins)/-sum(losses) if losses else None,
                avg=sum(pnl)/len(pnl) if pnl else 0,
                avg_win=sum(wins)/len(wins) if wins else 0,
                avg_loss=sum(losses)/len(losses) if losses else 0,
                streak=longest, current_streak=streak,
                fees=sum(t['fees'] for t in trades), funding=sum(t['funding_paid'] for t in trades))


def overview(state, cfg, equity):
    positions = list(state['positions'].values())
    change = equity-cfg.initial_equity
    now = int(time.time()*1000)
    recent = [v for v in state.get('candidates', {}).values() if now-v['at'] < 7*MINUTE]
    pauses = []
    if not state['enabled']: pauses.append('поиск выключен')
    if state['day_paused']: pauses.append('дневная защита')
    if state['dd_paused']: pauses.append('защита от снижения счёта')
    if state['tf_pause_until'].get('5m', 0) > now: pauses.append('перерыв после убытков')
    status = '⏸ '+', '.join(pauses) if pauses else '🟢 Поиск включён'
    if not pauses and not recent: status = '🟡 Ожидаю свежую проверку рынка'
    if state.get('data_issues'): status = '🟠 Есть проблемы с данными · см. «Связь»'
    lines = ['◈ AI CRYPTO', 'Тренд и откат · сигналы для фьючерсов', status, '',
             'СЧЁТ В МОДЕЛИ', f'{equity:,.2f} USDT  ·  {change:+.2f} от старта',
             f'Закрыто сделок: {state["closed_count"]}  ·  Итог: {state["realized_net"]:+.2f} USDT', '',
             'СЕЙЧАС', f'Открыто {len(positions)} / {cfg.max_positions}  ·  Ожидают входа {len(state["pending"])}',
             f'Монет в обзоре: {len(state.get("universe", []))}  ·  Проверка каждые 5 минут',
             f'Направление найдено: {sum(v["stage"] >= 1 for v in recent)}',
             f'Откат найден: {sum(v["stage"] >= 2 for v in recent)}', '',
             '1 час · направление   →   15 мин · откат   →   5 мин · вход',
             'Последняя проверка: '+stamp(state.get('last_scan_close', 0)), '',
             'Цены: Binance Spot. LONG и SHORT разрешены.',
             'Деньги учебные. Перед входом сверь уровни с фьючерсом.',
             f'v{BOT_VERSION}']
    return '\n'.join(lines)

def analytics(trades, period='ALL'):
    rows = select_trades(trades, period)
    m = trade_metrics(rows)
    lines = ['📊 МОИ РЕЗУЛЬТАТЫ', PERIODS[period]+' · учебный счёт', '',
             f'Итог после расходов: {m["net"]:+.2f} USDT',
             f'Закрыто: {m["n"]}  ·  Прибыльных: {m["wins"]}  ·  Убыточных: {m["losses"]}']
    if rows:
        ratio = f'{m["pf"]:.2f}' if m['pf'] is not None else 'пока нет убытков для сравнения'
        avg_r = sum(t.get('net_r', 0) for t in rows)/len(rows)
        lines += ['', 'КАЧЕСТВО СДЕЛОК', f'Доля прибыльных: {m["winrate"]:.1f}%',
                  f'Средний результат: {m["avg"]:+.2f} USDT',
                  f'Средняя прибыль / убыток: {m["avg_win"]:+.2f} / {m["avg_loss"]:+.2f} USDT',
                  f'Прибыль на 1 USDT убытка: {ratio}',
                  f'Средний итог на 1 USDT планового риска: {avg_r:+.2f}',
                  f'Самая длинная серия убытков: {m["streak"]}',
                  f'Неясный порядок стопа и цели: {sum(t.get("ambiguous_bars", 0) for t in rows)} свечей',
                  'В таких свечах модель считает сначала стоп.']
    else:
        lines += ['', 'Первая статистика появится после закрытия сделок.',
                  'Пустая история не означает, что бот выключен: открой «Что ждём».']
    lines += ['', 'РОСТ И ПАДЕНИЕ']
    for side, name in SIDES.items():
        g = trade_metrics([t for t in rows if t['side'] == side])
        lines.append(f'{name}: {g["net"]:+.2f} USDT · сделок {g["n"]}')
    lines += ['', f'Учтено комиссий: {m["fees"]:.2f} USDT',
              'Ухудшение цены исполнения тоже учтено. Платежи за удержание фьючерсов не учтены.',
              'Это модель по спотовым ценам, а не доходность реального фьючерсного счёта.',
              'Период — по времени полного закрытия сделки. Время: UTC.']
    return '\n'.join(lines)

def breakdown(trades, period='ALL'):
    rows = select_trades(trades, period)
    groups = defaultdict(list)
    for t in rows: groups[t['symbol']].append(t)
    lines=['🧩 ПОДРОБНОСТИ · '+PERIODS[period], 'Модель по спотовым ценам · суммы в USDT', '', '🪙 ПО МОНЕТАМ']
    for symbol, group in sorted(groups.items(), key=lambda item: sum(t['net_pnl'] for t in item[1]), reverse=True):
        m=trade_metrics(group)
        lines.append(f'{symbol}: {m["net"]:+.2f} · сделок {m["n"]}')
    if not rows: lines.append('Завершённых сделок пока нет.')
    lines += ['', '🚪 ПРИЧИНЫ ЗАКРЫТИЯ']
    for reason, count in sorted(Counter(t['exit_reason'] for t in rows).items()):
        lines.append(f'{EXITS.get(reason, "Другая причина")}: {count}')
    lines += [f'Достигли цели: {sum(bool(t["tp1_hit"]) for t in rows)} из {len(rows)}', '', 'ПО ДНЯМ ЗАКРЫТИЯ · UTC']
    days=defaultdict(float)
    for t in rows: days[datetime.fromtimestamp(t['exit_ts']/1000,timezone.utc).strftime('%d.%m.%Y')] += t['net_pnl']
    for day, net in sorted(days.items(), key=lambda p: datetime.strptime(p[0], '%d.%m.%Y'), reverse=True):
        lines.append(f'{day}: {net:+.2f}')
    return '\n'.join(lines)


def risk_report(s, cfg):
    equity=account_equity(s)
    dd=max(0,1-equity/max(s['peak_equity'],1e-12))*100
    used=sum(p['planned_risk'] for p in s['positions'].values())
    return '\n'.join(['🛡 ЗАЩИТА СЧЁТА', 'Все суммы относятся к учебному счёту.', '',
        f'Планируемый риск одной сделки: до {cfg.risk_fraction*100:.2f}% счёта',
        f'Общий лимит планируемого риска: {cfg.max_total_risk*100:.2f}%',
        f'Лимит похожих движений: {cfg.correlated_risk_cap*100:.2f}%',
        f'Исходный риск открытых сделок: {used:.2f} USDT',
        f'Максимум открытых сделок: {cfg.max_positions}',
        f'Максимум в одном направлении: {cfg.max_same_direction}', '',
        f'Снижение от максимума счёта сейчас: {dd:.2f}%',
        f'Самое большое наблюдавшееся снижение: {s["max_drawdown"]*100:.2f}%',
        f'Пауза при потере за день: {cfg.daily_loss_limit*100:.2f}%',
        f'Пауза при снижении от максимума: {cfg.drawdown_limit*100:.2f}%', '',
        f'Расчётная комиссия за каждое исполнение: {cfg.fee_bps/100:.3f}%',
        f'Поправка на худшую цену исполнения: {cfg.slippage_bps/100:.3f}%',
        'Это настройки модели; они могут отличаться от твоего тарифа на бирже.', '',
        'Защитный стоп ограничивает планируемую потерю. При резком скачке цены фактический убыток может быть больше.',
        'Дневная пауза действует до следующего дня UTC. Пауза после общего снижения не снимается кнопкой включения поиска.'])


def live_report(s):
    lines = ['📌 АКТИВНЫЕ СДЕЛКИ', 'Учебный учёт · цены Binance Spot']
    for raw in s['positions'].values():
        p = Position(**raw)
        lines += ['', f'{"🟢" if p.side == "LONG" else "🔴"} {p.symbol} · {SIDES[p.side]}']
        lines += level_lines(p.entry, p.stop, p.tp1, p.tp2)
        lines += ['Обновлено: '+stamp(p.last_bar_ts+MINUTE if p.last_bar_ts >= 0 else p.entry_ts)]
    if not s['positions']:
        lines += ['', 'Сейчас открытых сделок нет.', 'В разделе «Что ждём» виден текущий этап поиска.']
    if s['pending']:
        lines += ['', 'ОЖИДАЮТ ВХОДА']
        for p in s['pending'].values():
            lines.append(f'{p["symbol"]} · {SIDES[p["side"]]} · {stamp(p["entry_ts"])}')
    lines += ['', 'Выбери сделку ниже, чтобы скопировать уровни.', 'Цены фьючерса могут отличаться.']
    return '\n'.join(lines)

def history_report(trades):
    lines=['📚 ИСТОРИЯ СДЕЛОК', 'Сначала последние закрытия · время UTC']
    if not trades: lines += ['', 'Завершённых сделок пока нет.']
    for t in sorted(trades,key=lambda t:(t['exit_ts'],t['id']),reverse=True)[:100]:
        lines += ['', f'{t["symbol"]} · {SIDES.get(t["side"],t["side"])}',
                  f'{MODES.get(t["tf"],t["tf"])} · {stamp(t["exit_ts"])}',
                  f'{EXITS.get(t["exit_reason"],"Другая причина")} · {t["net_pnl"]:+.2f} USDT после расходов']
    lines += ['', 'Показаны до 100 последних сделок. Полную таблицу можно скачать в разделе «Результаты».']
    return '\n'.join(lines)


def why_report(s):
    now = int(time.time()*1000)
    recent = sorted(((symbol, value) for symbol, value in s.get('candidates', {}).items()
                     if now-value['at'] < 7*MINUTE), key=lambda pair: (-pair[1]['stage'], pair[0]))
    lines = ['🔭 ЧТО СЕЙЧАС ЖДЁМ', 'Проверка каждые 5 минут · только завершённые свечи', '']
    if not s['enabled']: lines += ['⏸ Поиск выключен в настройках.', '']
    if s['day_paused'] or s['dd_paused']: lines += ['🛡 Сработала защита от потерь. См. «Риск».', '']
    lines += [f'Проверено свежих ситуаций: {len(recent)} / {len(s.get("universe", []))}',
              f'1 · Направление есть: {sum(v["stage"] >= 1 for _,v in recent)}',
              f'2 · Откат найден: {sum(v["stage"] >= 2 for _,v in recent)}',
              f'3 · Подтверждение входа есть: {sum(v["stage"] >= 3 for _,v in recent)}', '',
              'Последний шаг ещё проверяет расходы, место до цели и свободный риск.']
    for symbol, value in recent[:12]:
        outcome = value.get('outcome', '')
        label = 'Вход назначен' if outcome == 'scheduled' else REASONS.get(outcome, 'Проверяю условия')
        lines += ['', symbol+' · '+(SIDES.get(value.get('side'), 'Направление не выбрано')), label]
    if not recent: lines += ['', 'Свежая проверка ещё не закончена. Состояние соединения — в «Связи».']
    lines += ['', 'С НАЧАЛА ЭТОЙ СТРАТЕГИИ',
              f'Проверок: {s["evaluated"].get("5m", 0)} · Назначено входов: {s["scheduled"].get("5m", 0)}']
    for reason, count in sorted(s['rejects'].get('5m', {}).items(), key=lambda pair:-pair[1])[:8]:
        lines.append(f'{REASONS.get(reason, reason)}: {count}')
    lines += ['', 'Счётчики показывают первую причину пропуска; это не оценка эффективности фильтров.',
              'Ожидание отката — рабочий этап поиска. Частота сигналов зависит от рынка.']
    return '\n'.join(lines)


def strategy_report():
    return """📘 КАК ВЫБИРАЕМ СДЕЛКУ

1 · НАПРАВЛЕНИЕ · 1 ЧАС
Цена выше средней за 200 свечей, средняя за 50 свечей выше неё и растёт — ищем рост. Для падения правила зеркальные.

2 · ОТКАТ · 15 МИНУТ
После движения вперёд цена возвращается к средним за 20–50 свечей. Предыдущая точка разворота должна сохраниться.

3 · ВХОД · 5 МИНУТ
Завершённая свеча проходит максимум трёх предыдущих свечей при росте или минимум при падении. Резкий уже ушедший импульс пропускаем.

СТОП И ЦЕЛЬ
Стоп — за краем отката с небольшим запасом. Одна цель — вдвое дальше стопа от цены входа. Комиссии уменьшают фактическое соотношение прибыли к риску. Если до цели мешает известный уровень, входа нет.

ПОВТОРЫ И ПОХОЖИЕ МОНЕТЫ
Один откат даёт не больше одного сигнала. Похожие движения нескольких монет делят общий лимит риска.

ПРОШЛЫЕ ПРИМЕРЫ
Сохраняем признаки каждой сделки. После накопления независимых завершённых примеров показываем сравнение с ними; оно не запрещает вход и не является вероятностью успеха.

ПРАВИЛА НОВЫЕ
Доходность ещё нужно подтвердить на независимой истории и в наблюдении. Бот не создаёт сделки ради количества и не обещает прибыль."""


GUIDE = """❓ ПРОСТО О БОТЕ

Бот ищет сигналы для ручных сделок на фьючерсах. Он читает публичные спотовые цены Binance; ордера на биржу не отправляет.

🟢 На рост (LONG) — идея заработать на росте цены.
🔴 На падение (SHORT) — идея заработать на снижении.
Вход — цена начала сделки. Стоп — выход при неблагоприятном движении. Цель — выход с прибылью, если цена её достигнет.

📋 КОПИРОВАНИЕ
В карточке кнопки «Вход», «Стоп» и «Цель» копируют только число. «Все уровни» — готовую заметку. Числа в тексте тоже можно копировать. Чтобы сохранить сигнал, перешли его в «Избранное».

ПРЕДВАРИТЕЛЬНЫЙ СИГНАЛ И УЧЕБНЫЙ ВХОД
Сигнал назначает вход на будущую полную минуту. После её завершения модель берёт цену открытия с поправкой на исполнение. Если цена успела далеко уйти, вход отменяется. Сверяй время: сообщение о расчётном входе не является текущей котировкой.

СДЕЛКИ — открытые позиции и их уровни.
РЕЗУЛЬТАТЫ — итог после расходов, история и выгрузка CSV.
ЧТО ЖДЁМ — на каком шаге остановился поиск.
СВЯЗЬ — свежесть данных и ошибки.
КАК ВЫБИРАЕМ — правила стратегии простыми словами.
РИСК — ограничения и защитные паузы.

Цены спота и фьючерса похожи, но могут отличаться. Перед ручным входом проверяй выбранный контракт и его цену. Статистика учебная; комиссии и ухудшение исполнения учтены, фьючерсные платежи за удержание — нет.

Меню обновляется в одном сообщении. Новые сигналы, закрытия и запрошенный CSV приходят отдельно. Время везде UTC."""

def historical_check_report():
    path = Path(__file__).with_name('validation.json')
    if not path.exists():
        return '📐 ПРОВЕРКА НА ИСТОРИИ\nСохранённого отчёта рядом с bot.py нет. Текущие результаты доступны в «Результатах».'
    result = json.loads(path.read_text(encoding='utf-8'))
    if result.get('strategy') != STRATEGY_ID:
        return '📐 ПРОВЕРКА НА ИСТОРИИ\nСохранённый отчёт относится к другой стратегии.'
    assumptions = result['assumptions']
    return '\n'.join(['📐 ПРОВЕРКА НА ИСТОРИИ',
        result['start'][:10]+' — '+result['end_exclusive'][:10]+' (последняя дата не включена)',
        ', '.join(result['symbols']), '',
        f'Назначено сигналов: {result["signals"]}',
        f'Закрыто сделок: {result["trades"]} · Прибыльных: {result["wins"]}',
        f'Итог закрытых: {result["net_pnl"]:+.2f} USDT',
        f'Максимальное наблюдавшееся снижение: {100*result["max_observed_drawdown"]:.2f}%',
        f'Комиссия модели за сторону: {assumptions["fee_bps"]/100:.3f}%', '',
        'Короткая ретроспективная проверка, не доказательство прибыльности.',
        'Свечи спотовые. Спред и ухудшение исполнения заданы допущением, платежи фьючерсов не учтены.',
        'Эти результаты отделены от текущего учебного счёта.'])


def ledger_csv(trades):
    stream=io.StringIO(newline='')
    fields=['id','strategy_id','setup_id','symbol','tf','side','entry_ts','exit_ts','entry',
            'initial_stop','tp2','qty','planned_risk','net_pnl','net_r','fees','funding_paid',
            'tp1_hit','exit_reason','ambiguous_bars']
    writer=csv.DictWriter(stream,fields,extrasaction='ignore')
    writer.writeheader()
    for row in sorted(trades,key=lambda t:(t['exit_ts'],t['id'])):
        writer.writerow({k:("'"+v if isinstance(v,str) and v.startswith(('=','+','-','@')) else v) for k,v in row.items() if k in fields})
    return ('\ufeff'+stream.getvalue()).encode('utf-8')


def text_pages(text, limit=3100):
    # Keep complete lines together; Telegram counts UTF-16 units, not Python chars.
    pages, chunk, size = [], '', 0
    for line in text.splitlines(keepends=True):
        units = len(line.encode('utf-16-le')) // 2
        if units <= limit:
            if size + units > limit:
                pages.append(chunk)
                chunk, size = '', 0
            chunk += line
            size += units
        else:
            for char in line:
                units = len(char.encode('utf-16-le')) // 2
                if size + units > limit:
                    pages.append(chunk)
                    chunk, size = '', 0
                chunk += char
                size += units
    if chunk:
        pages.append(chunk)
    return pages or ['Нет данных.']


def panel_html(text):
    # Escape first; only our plain-text numeric code markers become HTML.
    lines = text.splitlines()
    rendered = []
    for i, line in enumerate(lines):
        safe = html.escape(line)
        safe = re.sub(r'`([A-Za-z0-9_.+\-]+)`', r'<code>\1</code>', safe)
        if i == 0 or (line.isupper() and len(line) > 3):
            safe = '<b>' + safe + '</b>'
        rendered.append(safe)
    return '\n'.join(rendered)


def price(value):
    # Fixed decimal text copies directly into order forms, never scientific notation.
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError('Invalid display price')
    return format(value, '.12f').rstrip('0').rstrip('.')


def level_lines(entry, stop, tp1, tp2=None):
    if tp2 is None or math.isclose(tp1, tp2):
        return [f'Вход: `{price(entry)}`', f'Стоп: `{price(stop)}`', f'Цель: `{price(tp1)}`']
    return [f'Вход: `{price(entry)}`', f'Стоп: `{price(stop)}`',
            f'Цель 1: `{price(tp1)}`', f'Цель 2: `{price(tp2)}`']

def copy_button(label, value):
    text = str(value)
    if not 1 <= len(text) <= 256:
        raise ValueError('Copy button text exceeds Telegram limit')
    return {'text': label, 'copy_text': {'text': text}}


def copy_levels_keyboard(p):
    ticket = f'{p["symbol"]} {p["side"]}\nВход: {price(p["entry"])}\nСтоп: {price(p["stop"])}\nЦель: {price(p["tp2"])}'
    return [[copy_button('📋 Вход', price(p['entry'])), copy_button('📋 Стоп', price(p['stop']))],
            [copy_button('📋 Цель', price(p['tp2'])), copy_button('📋 Все уровни', ticket)]]

def trade_card(raw):
    p = Position(**raw)
    net = p.net_pnl+p.remaining*(direction(p.side)*(p.mark*(1-direction(p.side)*p.slip_rate)-p.entry)
                                -p.mark*(1-direction(p.side)*p.slip_rate)*p.fee_rate)
    lines = ['◈ '+p.symbol, SIDES[p.side]+' · учебная сделка', '']
    lines += level_lines(p.entry, p.stop, p.tp1, p.tp2)
    lines += ['', f'Последняя цена: `{price(p.mark or p.entry)}`', f'При закрытии сейчас: {net:+.2f} USDT',
              f'Планируемый риск: {p.planned_risk:.2f} USDT',
              f'Выход по времени: {stamp(p.deadline)}',
              'Обновлено: '+stamp(p.last_bar_ts+MINUTE if p.last_bar_ts >= 0 else p.entry_ts)]
    score = p.neighbor_score
    if score.get('samples'):
        lines += ['', f'Похожие завершённые примеры: {score["samples"]}',
                  f'Их средний результат на 1 USDT риска: {score["mean_r"]:+.2f}',
                  'Справка по прошлым сделкам; не вероятность успеха.']
    else:
        lines += ['', 'Прошлые примеры: пока накапливаем историю.']
    lines += ['', 'Цены Binance Spot. Сверь уровни с выбранным фьючерсом.', 'Кнопки ниже копируют числа.']
    return '\n'.join(lines)

def signal_message(symbol, tf, pending, cfg):
    preview = make_setup_position(symbol, pending, pending['reference'], pending['entry_ts'], 1., cfg)
    lines = ['🔔 НОВЫЙ СИГНАЛ', symbol+' · '+SIDES[pending['side']],
             'Предварительные уровни · цены Binance Spot', '']
    lines += level_lines(preview.entry, preview.stop, preview.tp1, preview.tp2)
    lines += ['', 'Почему: часовой тренд, откат на 15 минутах и подтверждение на 5 минутах.',
              f'Учебный вход назначен на {stamp(pending["entry_ts"])}',
              'Если цена уйдёт далеко, вход отменится. Итоговые уровни будут после расчёта исполнения.',
              'Перед ручным входом проверь цену фьючерса.']
    return '\n'.join(lines)

def button(text, action):
    return {'text':text, 'callback_data':action}


def panel_keyboard(action='MENU', page=0, pages=1, state=None):
    state=state or {}
    rows=[]
    if pages>1:
        nav=[]
        if page>0: nav.append(button('‹ Назад',f'PAGE|{action}|{page-1}'))
        if page+1<pages: nav.append(button('Далее ›',f'PAGE|{action}|{page+1}'))
        rows.append(nav)
    if action.startswith('TRADE|'):
        raw = state.get('positions', {}).get(action.split('|', 1)[1])
        if raw:
            rows += copy_levels_keyboard(raw)
            rows.append([button('💬 Отдельное сообщение с уровнями', 'SAVE|'+action.split('|', 1)[1])])
        rows.append([button('📌 Все сделки', 'SHOW_LIVE_TRADES')])
    elif action == 'SHOW_LIVE_TRADES':
        cards = list(state.get('positions', {}).items())
        offset = page * 5
        for pid, raw in cards[offset:offset+5]:
            rows.append([button(f'📋 {raw["symbol"]} · {raw["side"]} · {raw["tf"]}', 'TRADE|'+pid)])
        if len(cards) > 5:
            rows.append([button('📑 Карточки: следующая группа', f'PAGE|{action}|{(page+1) % ((len(cards)+4)//5)}')])
    elif action.startswith(('AN_','DETAILS_')):
        prefix='DETAILS_' if action.startswith('DETAILS_') else 'AN_'
        period=action[len(prefix):]
        rows += [[button(('✓ ' if period==p else '')+name,prefix+p) for p,name in [('TODAY','Сегодня'),('7D','7 дней'),('30D','30 дней')]],
                 [button('Всё время',prefix+'ALL'),button('Подробности' if prefix=='AN_' else 'Итоги',('DETAILS_' if prefix=='AN_' else 'AN_')+period)],
                 [button('📥 Скачать сделки · CSV','EXPORT_CSV')],
                 [button('📐 Проверка на истории','CHECK')]]
    elif action=='CONTROLS':
        rows += [[button('⏸ Остановить поиск' if state.get('enabled',True) else '▶️ Включить поиск',
                         'SIGNALS_OFF' if state.get('enabled',True) else 'SIGNALS_ON')],
                 [button('🛡 Защита','RISK')]]
    elif action=='MENU':
        rows += [[button('📌 Сделки','SHOW_LIVE_TRADES'),button('📊 Результаты','AN_ALL')],
                 [button('🔭 Что ждём','SHOW_REJECTS'),button('📚 История','SHOW_HISTORY')],
                 [button('📡 Связь','STATUS'),button('⚙️ Управление','CONTROLS')],
                 [button('📘 Как выбираем','STRATEGY'),button('🛡 Риск','RISK')],
                 [button('❓ Помощь','HELP')]]
    if action!='MENU': rows.append([button('🏠 Главная','MENU'),button('🔄 Обновить',f'PAGE|{action}|{page}')])
    else: rows.append([button('🔄 Обновить','MENU')])
    return {'inline_keyboard':rows}


def keyboard():
    return panel_keyboard()


import numpy as np
import pandas as pd

LOG = logging.getLogger('paper_bot')
MINUTE = 60_000
TF_MS = {'5m': 5 * MINUTE, '15m': 15 * MINUTE, '1h': 60 * MINUTE, '4h': 240 * MINUTE}
STRATEGY_TFS = ('1h', '15m', '5m')
SYMBOLS = ('BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT',
           'DOGEUSDT', 'ADAUSDT', 'AVAXUSDT', 'DOTUSDT', 'LINKUSDT')
# A known crypto-only pool excludes stablecoins, leveraged tokens and tokenized
# stocks. Rank eligible members by today's public spot volume, not by returns.
CRYPTO_POOL = frozenset(base+'USDT' for base in (
    'BTC ETH SOL BNB XRP DOGE ADA AVAX DOT LINK LTC BCH NEAR UNI AAVE SUI HYPE '
    'ZEC TRX TON XLM HBAR ICP FIL ATOM APT ARB OP INJ SEI TIA WLD ENA ONDO '
    'ZRO RENDER TAO FET PEPE SHIB BONK WIF FLOKI CRV LDO PENDLE JUP JTO '
    'PYTH RUNE ETC ALGO VET SAND GALA AXS IMX GRT STX AR MKR COMP DYDX '
    'SNX FTM POL ENS QNT KAS BERA IP TRUMP PENGU MORPHO EIGEN'
).split())
FEATURES = ('body_atr', 'range_atr', 'volume_ratio', 'ema_distance', 'rsi_scaled')


@dataclass(frozen=True)
class Config:
    market: str = 'spot_proxy'
    initial_equity: float = 10_000.0
    risk_fraction: float = 0.0025
    max_total_risk: float = 0.0075
    max_position_notional: float = 0.25
    max_gross_notional: float = 1.0
    max_positions: int = 3
    max_same_direction: int = 2
    daily_loss_limit: float = 0.02
    drawdown_limit: float = 0.08
    loss_streak_limit: int = 3
    # Futures fee scenario (Bybit VIP0 taker base rate), not the user's verified tariff.
    fee_bps: float = 5.5
    slippage_bps: float = 2.0
    max_spread_bps: float = 10.0
    stop_atr: float = 1.5
    tp1_r: float = 1.0
    tp2_r: float = 2.0
    tp1_fraction: float = 0.5
    horizon: int = 48
    history_limit: int = 2500
    warmup: int = 250
    neighbors: int = 15
    min_adx: float = 20.0
    min_neighbor_mean_r: float = 0.10
    min_neighbor_win_fraction: float = 0.55
    max_entry_gap_atr: float = 0.25
    strategy_id: str = STRATEGY_ID
    universe_size: int = 25
    min_quote_volume: float = 10_000_000.0
    feature_history: int = 350
    target_r: float = 2.0
    max_cost_fraction: float = 0.25
    stop_buffer_atr: float = 0.15
    correlated_risk_cap: float = 0.0035
    correlation_threshold: float = 0.75
    signal_lifetime_ms: int = 90_000

    def __post_init__(self):
        if self.market not in ('spot', 'usdm', 'spot_proxy'):
            raise ValueError('MARKET must be spot, usdm or spot_proxy')
        for name in ('initial_equity', 'risk_fraction', 'max_total_risk',
                     'max_position_notional', 'max_gross_notional', 'stop_atr',
                     'tp1_r', 'tp2_r', 'daily_loss_limit', 'drawdown_limit'):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f'{name} must be finite and positive')
        for name in ('fee_bps', 'slippage_bps', 'max_spread_bps', 'max_entry_gap_atr'):
            if not math.isfinite(getattr(self, name)) or not 0 <= getattr(self, name) < 1000:
                raise ValueError(f'Invalid {name}')
        if not 0 < self.tp1_fraction < 1 or self.tp2_r <= self.tp1_r:
            raise ValueError('Invalid take-profit settings')
        if not 0 < self.min_neighbor_win_fraction <= 1:
            raise ValueError('Invalid KNN threshold')
        if not math.isfinite(self.min_neighbor_mean_r) or not math.isfinite(self.min_adx):
            raise ValueError('Invalid KNN/ADX settings')
        if self.risk_fraction > self.max_total_risk or self.max_gross_notional > 1:
            raise ValueError('Risk budget invalid; gross leverage above 1x is disabled')
        if self.max_position_notional > self.max_gross_notional:
            raise ValueError('Position cap exceeds gross cap')
        if not 0 < self.daily_loss_limit < 1 or not 0 < self.drawdown_limit < 1:
            raise ValueError('Loss limits must be fractions between 0 and 1')
        if any(getattr(self, n) < 1 for n in ('max_positions', 'max_same_direction',
                                             'loss_streak_limit', 'horizon', 'neighbors')):
            raise ValueError('Counts must be positive')
        if self.warmup < 210 or self.history_limit < self.warmup + self.horizon * self.neighbors:
            raise ValueError('Not enough history for non-overlapping KNN outcomes')
        if not 5 <= self.universe_size <= 30 or self.feature_history < 250:
            raise ValueError('Invalid universe/history size')
        if not 0 < self.correlated_risk_cap <= self.max_total_risk:
            raise ValueError('Invalid correlated risk budget')
        for name in ('target_r', 'max_cost_fraction', 'stop_buffer_atr', 'min_quote_volume'):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(f'Invalid {name}')

    @classmethod
    def from_env(cls):
        mapping = {'initial_equity': 'INITIAL_EQUITY',
                   'risk_fraction': 'RISK_PER_TRADE', 'fee_bps': 'FEE_BPS',
                   'slippage_bps': 'SLIPPAGE_BPS'}
        # Data source and permitted signal directions are independent.
        defaults = cls(market='spot_proxy')
        values = {}
        for name, key in mapping.items():
            if key in os.environ:
                values[name] = str(os.environ[key]) if name == 'market' else float(os.environ[key])
        return dataclasses.replace(defaults, **values)


def frame_from_klines(rows, now_ms: int) -> pd.DataFrame:
    columns = ['ts', 'open', 'high', 'low', 'close', 'volume', 'close_ts',
               'quote_volume', 'trades', 'buy_volume', 'buy_quote_volume', 'ignore']
    df = pd.DataFrame(rows, columns=columns)
    if df.empty:
        return df
    for name in ('ts', 'close_ts', 'open', 'high', 'low', 'close', 'volume'):
        df[name] = pd.to_numeric(df[name], errors='raise')
    df = df.loc[df.close_ts < now_ms].sort_values('ts').drop_duplicates('ts').reset_index(drop=True)
    validate_bars(df)
    return df


def validate_bars(df: pd.DataFrame, interval_ms: int | None = None):
    if df.empty:
        return
    required = ['ts', 'open', 'high', 'low', 'close', 'volume', 'close_ts']
    values = df[required].to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise ValueError('Non-finite OHLCV data')
    if ((df[['open', 'high', 'low', 'close']] <= 0).any().any()
            or (df.volume < 0).any() or (df.close_ts < df.ts).any()
            or (df.high < df[['open', 'close', 'low']].max(axis=1)).any()
            or (df.low > df[['open', 'close', 'high']].min(axis=1)).any()
            or not df.ts.is_monotonic_increasing or df.ts.duplicated().any()):
        raise ValueError('Invalid OHLCV data/order')
    if interval_ms is not None:
        if (len(df) > 1 and not (df.ts.diff().dropna() == interval_ms).all()
                or not (df.close_ts == df.ts + interval_ms - 1).all()):
            raise ValueError('Missing or misaligned candles; do not silently fill gaps')


def build_feature_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Every feature depends exclusively on the current/past CLOSED candles."""
    d = df.copy()
    close = d.close
    d['ema200'] = close.ewm(span=200, adjust=False).mean()
    d['ema50'] = close.ewm(span=50, adjust=False).mean()
    d['ema20'] = close.ewm(span=20, adjust=False).mean()
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    d['rsi'] = (100 - 100 / (1 + gain / loss.replace(0, np.nan))).where(loss > 0, 100.0)
    d.loc[(gain == 0) & (loss == 0), 'rsi'] = 50.0
    tr = pd.concat([d.high - d.low, (d.high - close.shift()).abs(),
                    (d.low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/14, adjust=False).mean()
    d['atr'] = atr
    up, down = d.high.diff(), -d.low.diff()
    plus = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=d.index)
    minus = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=d.index)
    plus_di = 100 * plus.ewm(alpha=1/14, adjust=False).mean() / atr.replace(0, np.nan)
    minus_di = 100 * minus.ewm(alpha=1/14, adjust=False).mean() / atr.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    d['adx'] = dx.fillna(0).ewm(alpha=1/14, adjust=False).mean()
    avg_volume = d.volume.shift(1).rolling(20, min_periods=20).mean()
    avg_range = (d.high - d.low).shift(1).rolling(50, min_periods=50).mean()
    d['volume_ratio'] = d.volume / avg_volume.replace(0, np.nan)
    d['anomaly'] = ((d.high - d.low) / avg_range.replace(0, np.nan) > 2.5) | (d.volume_ratio > 4.5)
    safe_atr = atr.replace(0, np.nan)
    d['body_atr'] = (close - d.open) / safe_atr
    d['range_atr'] = (d.high - d.low) / safe_atr
    d['ema_distance'] = (close - d.ema200) / safe_atr
    d['rsi_scaled'] = d.rsi / 100
    d['slope'] = (d.ema200 - d.ema200.shift(20)) / safe_atr
    return d


def direction(side: str) -> int:
    if side not in ('LONG', 'SHORT'):
        raise ValueError('Invalid side')
    return 1 if side == 'LONG' else -1


@dataclass
class Position:
    id: str
    symbol: str
    tf: str
    side: str
    entry_ts: int
    entry: float
    qty: float
    remaining: float
    stop: float
    initial_stop: float
    tp1: float
    tp2: float
    deadline: int
    planned_risk: float
    fee_rate: float
    slip_rate: float
    partial_fraction: float
    last_bar_ts: int = -1
    mark: float = 0.0
    tp1_hit: bool = False
    gross_pnl: float = 0.0
    fees: float = 0.0
    funding_paid: float = 0.0
    funding_through: int = -1
    exit_ts: int | None = None
    exit_reason: str | None = None
    ambiguous_bars: int = 0
    fills: list = field(default_factory=list)
    exit_mode: str = 'partial'
    strategy_id: str = 'legacy'
    setup_id: str = ''
    signal_features: dict = field(default_factory=dict)
    neighbor_score: dict = field(default_factory=dict)

    @property
    def net_pnl(self):
        return self.gross_pnl - self.fees - self.funding_paid


def make_position(symbol, tf, side, raw_open, atr, ts, qty, cfg: Config) -> Position:
    sign = direction(side)
    if tf not in TF_MS or any(not math.isfinite(x) or x <= 0 for x in (raw_open, atr, qty)):
        raise ValueError('Invalid entry/ATR/quantity/timeframe')
    if cfg.market == 'spot' and side == 'SHORT':
        raise ValueError('Spot short disabled: no borrow/margin model')
    slip, fee = cfg.slippage_bps / 10000, cfg.fee_bps / 10000
    entry = raw_open * (1 + sign * slip)
    risk_distance = cfg.stop_atr * atr
    stop = entry - sign * risk_distance
    tp1, tp2 = entry + sign * cfg.tp1_r * risk_distance, entry + sign * cfg.tp2_r * risk_distance
    if min(stop, tp1, tp2) <= 0:
        raise ValueError('Non-positive exit level')
    slipped_stop = stop * (1 - sign * slip)
    risk_unit = sign * (entry - slipped_stop) + fee * (entry + slipped_stop)
    p = Position(str(uuid.uuid4()), symbol, tf, side, int(ts), entry, qty, qty,
                 stop, stop, tp1, tp2, int(ts + cfg.horizon * TF_MS[tf] - 1),
                 qty * risk_unit, fee, slip, cfg.tp1_fraction, mark=raw_open,
                 funding_through=int(ts), fees=entry * qty * fee)
    p.fills.append({'kind': 'ENTRY', 'ts': int(ts), 'price': entry, 'qty': qty,
                    'fee': p.fees, 'model': 'adverse_slippage'})
    return p


def exit_fill(p: Position, raw_price: float, qty: float, ts: int, reason: str):
    sign = direction(p.side)
    price = raw_price * (1 - sign * p.slip_rate)
    qty = min(qty, p.remaining)
    gross = sign * (price - p.entry) * qty
    fee = price * qty * p.fee_rate
    p.gross_pnl += gross
    p.fees += fee
    p.remaining = max(0.0, p.remaining - qty)
    p.fills.append({'kind': reason, 'ts': int(ts), 'price': price, 'qty': qty, 'fee': fee})
    if p.remaining <= p.qty * 1e-9:
        p.remaining = 0.0
        p.exit_reason, p.exit_ts = reason, int(ts)


def cost_aware_be_stop(p: Position) -> float:
    """Raw trigger that covers entry+exit fees/slip on the remaining leg.

    Funding and gaps cannot be guaranteed by a stop at this level.
    """
    f, s = p.fee_rate, p.slip_rate
    if p.side == 'LONG':
        return p.entry * (1 + f) / ((1 - f) * (1 - s))
    return p.entry * (1 - f) / ((1 + f) * (1 + s))


def step_position(p: Position, bar: dict, funding: list | None = None):
    """Single event implementation shared by paper monitor, labels, backtest."""
    ts, end = int(bar['ts']), int(bar['close_ts'])
    if p.remaining == 0 or ts < p.entry_ts or ts <= p.last_bar_ts:
        return
    # For an offline/missing-data gap, the caller must backfill, not skip bars.
    sign = direction(p.side)
    for event in sorted(funding or [], key=lambda e: int(e['fundingTime'])):
        t = int(event['fundingTime'])
        if p.funding_through < t <= end and p.entry_ts < t:
            rate, mark = float(event['fundingRate']), float(event['markPrice'])
            if not math.isfinite(rate) or not math.isfinite(mark) or mark <= 0:
                raise ValueError('Invalid funding event')
            amount = sign * p.remaining * mark * rate
            p.funding_paid += amount
            p.fills.append({'kind': 'FUNDING', 'ts': t, 'amount': amount})
            p.funding_through = t
    p.funding_through = max(p.funding_through, end)
    o, h, l, c = (float(bar[k]) for k in ('open', 'high', 'low', 'close'))
    p.mark, p.last_bar_ts = c, ts
    stop_hit = l <= p.stop if sign == 1 else h >= p.stop
    target_hit = h >= p.tp1 if sign == 1 else l <= p.tp1
    if stop_hit:
        if target_hit and not p.tp1_hit:
            p.ambiguous_bars += 1
        # A stop gapped at open fills at the worse open, never magically at stop.
        raw = min(o, p.stop) if sign == 1 else max(o, p.stop)
        exit_fill(p, raw, p.remaining, end, 'BE' if p.tp1_hit else 'SL')
        return
    if p.exit_mode == 'single':
        if target_hit:
            p.tp1_hit = True
            exit_fill(p, p.tp1, p.remaining, end, 'TP')
        elif end >= p.deadline:
            exit_fill(p, c, p.remaining, end, 'TIMEOUT')
        return
    if not p.tp1_hit and target_hit:
        exit_fill(p, p.tp1, p.qty * p.partial_fraction, end, 'TP1')
        p.tp1_hit = True
        p.stop = cost_aware_be_stop(p)  # inspected only on a subsequent bar
    tp2_hit = h >= p.tp2 if sign == 1 else l <= p.tp2
    if tp2_hit:
        exit_fill(p, p.tp2, p.remaining, end, 'TP2')
    elif end >= p.deadline:
        exit_fill(p, c, p.remaining, end, 'TIMEOUT')


def eligible_history_indices(current_idx: int, cfg: Config):
    # The next-bar entry AND the whole outcome window are strictly before query.
    start = max(cfg.warmup, current_idx - cfg.history_limit + 1)
    return np.arange(start, max(start, current_idx - cfg.horizon), dtype=int)


def choose_neighbors(df: pd.DataFrame, current_idx: int, cfg: Config):
    indices = eligible_history_indices(current_idx, cfg)
    if len(indices) < cfg.neighbors:
        return []
    x = df.loc[indices, list(FEATURES)].to_numpy(dtype=float)
    query = df.loc[current_idx, list(FEATURES)].to_numpy(dtype=float)
    good = np.isfinite(x).all(axis=1)
    x, indices = x[good], indices[good]
    if len(x) < cfg.neighbors or not np.isfinite(query).all():
        return []
    median = np.median(x, axis=0)
    scale = np.percentile(x, 75, axis=0) - np.percentile(x, 25, axis=0)
    scale = np.maximum(scale, 1e-6)
    train = np.clip((x - median) / scale, -10, 10)
    target = np.clip((query - median) / scale, -10, 10)
    distances = np.sqrt(np.mean((train - target) ** 2, axis=1))
    selected = []
    # Avoid counting many adjacent candles with overlapping outcomes as evidence.
    for order in np.argsort(distances, kind='stable'):
        i = int(indices[order])
        if all(abs(i - other) >= cfg.horizon for other in selected):
            selected.append(i)
            if len(selected) == cfg.neighbors:
                return selected
    return []


def historical_r(df, i, side, tf, cfg):
    atr, reference = float(df.iloc[i].atr), float(df.iloc[i].close)
    first = df.iloc[i + 1]
    if abs(float(first.open) - reference) > cfg.max_entry_gap_atr * atr:
        return None
    p = make_position('HISTORY', tf, side, float(first.open), atr, int(first.ts), 1.0, cfg)
    for j in range(i + 1, i + cfg.horizon + 1):
        step_position(p, df.iloc[j].to_dict())
        if p.remaining == 0:
            break
    # Futures KNN scores exclude funding; their score is NOT a probability/EV
    # estimate for executable futures positions. The spot_proxy profile excludes funding and cannot represent actual futures PnL.
    return p.net_pnl / p.planned_risk


def predict_knn(df, current_idx, tf, cfg: Config):
    row = df.iloc[current_idx]
    if current_idx < cfg.warmup or not np.isfinite(row[list(FEATURES)].to_numpy(dtype=float)).all():
        return None, 'insufficient_history'
    if bool(row.anomaly):
        return None, 'range_or_volume_anomaly'
    if not math.isfinite(row.adx) or row.adx < cfg.min_adx:
        return None, 'adx'
    if row.close > row.ema200 and row.ema50 > row.ema200 and row.slope > 0:
        side = 'LONG'
        if row.rsi > 70:
            return None, 'rsi_overbought'
    elif row.close < row.ema200 and row.ema50 < row.ema200 and row.slope < 0:
        side = 'SHORT'
        if cfg.market == 'spot':
            return None, 'spot_short_disabled'
        if row.rsi < 30:
            return None, 'rsi_oversold'
    else:
        return None, 'trend_conflict'
    neighbors = choose_neighbors(df, current_idx, cfg)
    if not neighbors:
        return None, 'insufficient_independent_neighbors'
    scores = [historical_r(df, i, side, tf, cfg) for i in neighbors]
    scores = [s for s in scores if s is not None and math.isfinite(s)]
    if len(scores) < cfg.neighbors:
        return None, 'neighbor_entry_gaps'
    mean_r, wins = float(np.mean(scores)), sum(x > 0 for x in scores) / len(scores)
    if mean_r < cfg.min_neighbor_mean_r or wins < cfg.min_neighbor_win_fraction:
        return None, 'knn_net_outcomes'
    return {'side': side, 'atr': float(row.atr), 'reference': float(row.close),
            'signal_ts': int(row.close_ts), 'neighbor_mean_r': mean_r,
            'neighbor_win_fraction': wins, 'neighbors': len(scores)}, ''


def confirmed_swings(frame, kind, width=2):
    """A pivot is usable only after `width` candles on its right have CLOSED."""
    values = frame[kind].to_numpy(dtype=float)
    result = []
    for i in range(width, len(values)-width):
        window = values[i-width:i+width+1]
        extreme = window.max() if kind == 'high' else window.min()
        if values[i] == extreme and np.count_nonzero(window == extreme) == 1:
            result.append((i, float(values[i])))
    return result


def evaluate_setup(frames, asof, cfg):
    """Pure, causal 1h → 15m → 5m decision. All inputs are trimmed BEFORE features.

    Returns signal, first rejection, and a human-readable dashboard stage.
    Thresholds are research hypotheses, not optimized performance claims.
    """
    context = {'stage': 0, 'side': '', 'at': int(asof)}
    data = {}
    for tf in STRATEGY_TFS:
        raw = frames[tf].loc[frames[tf].close_ts <= asof].copy().reset_index(drop=True)
        expected = ((asof+1)//TF_MS[tf])*TF_MS[tf]-1
        if raw.empty or int(raw.iloc[-1].close_ts) != expected:
            raise ValueError(f'Stale {tf} candles')
        validate_bars(raw, TF_MS[tf])
        if len(raw) < (250 if tf == '1h' else 80):
            return None, 'insufficient_history', context
        data[tf] = build_feature_frame(raw)
    hour, pull, entry = (data[tf] for tf in STRATEGY_TFS)
    h, p, e = hour.iloc[-1], pull.iloc[-1], entry.iloc[-1]
    if not all(math.isfinite(float(x)) and x > 0 for x in (h.atr, p.atr, e.atr)):
        return None, 'insufficient_history', context
    slope = h.ema50 - hour.iloc[-4].ema50
    if h.close > h.ema200 and h.ema50 > h.ema200 and slope > .05*h.atr:
        side, sign = 'LONG', 1
    elif h.close < h.ema200 and h.ema50 < h.ema200 and slope < -.05*h.atr:
        side, sign = 'SHORT', -1
    else:
        return None, 'trend_unclear', context
    context.update(stage=1, side=side)

    # A recent touch of the EMA20–50 zone must follow a visible impulse.
    # The setup identity uses the impulse candle, so repeated touches of the
    # same pullback cannot create an unlimited series of entry attempts.
    touches = []
    for i in range(len(pull)-6, len(pull)):
        row = pull.iloc[i]
        lower, upper = min(row.ema20, row.ema50), max(row.ema20, row.ema50)
        if row.low <= upper + .15*row.atr and row.high >= lower - .15*row.atr:
            before = pull.iloc[max(0, i-12):i]
            peak_idx = int(before.high.idxmax() if sign == 1 else before.low.idxmin())
            peak = pull.iloc[peak_idx]
            extension = peak.high-max(peak.ema20, peak.ema50) if sign == 1 else min(peak.ema20, peak.ema50)-peak.low
            if extension >= .8*peak.atr and peak_idx < i:
                touches.append((i, peak_idx))
    if not touches:
        return None, 'pullback_wait', context
    touch_idx, peak_idx = touches[-1]
    touch = pull.iloc[touch_idx]
    # Use only pivots that were already confirmed before the impulse.
    structure = confirmed_swings(pull.iloc[max(0, peak_idx-48):peak_idx+1], 'low' if sign == 1 else 'high')
    if not structure:
        return None, 'pullback_wait', context
    structure_price = structure[-1][1]
    retracement = pull.iloc[peak_idx+1:]
    extreme = float(retracement.low.min() if sign == 1 else retracement.high.max())
    if sign*(extreme-structure_price) <= 0:
        return None, 'structure_broken', context
    context.update(stage=2, pullback_at=int(touch.close_ts))
    trigger = float(entry.iloc[-4:-1].high.max() if sign == 1 else entry.iloc[-4:-1].low.min())
    if e.ts <= touch.close_ts or sign*(e.close-trigger) <= 0 or sign*(e.close-e.open) <= 0:
        return None, 'trigger_wait', context
    if e.high-e.low > 3*e.atr or sign*(e.close-trigger) > 1.5*e.atr:
        return None, 'move_too_large', context
    context['stage'] = 3
    after_touch = entry.loc[entry.ts > touch.close_ts]
    extreme = min(extreme, float(after_touch.low.min())) if sign == 1 else max(extreme, float(after_touch.high.max()))
    stop = extreme - sign*cfg.stop_buffer_atr*p.atr
    reference = float(e.close)
    distance = sign*(reference-stop)
    if not .5*e.atr <= distance <= 4*p.atr or stop <= 0:
        return None, 'stop_distance', context
    # Confirmed resistance/support is known at the signal time; never use a
    # future pivot to filter an otherwise losing historical trade.
    obstacles = [value for _, value in confirmed_swings(pull.tail(96).reset_index(drop=True), 'high' if sign == 1 else 'low')
                 if sign*(value-reference) > .1*e.atr]
    obstacle = min(obstacles) if sign == 1 and obstacles else max(obstacles) if obstacles else None
    features = {key: float(e[key]) for key in FEATURES}
    if not all(math.isfinite(value) for value in features.values()):
        return None, 'insufficient_history', context
    signal = dict(side=side, atr=float(e.atr), reference=reference, stop=float(stop),
                  obstacle=obstacle, obstacle_buffer=float(.1*e.atr), signal_ts=int(e.close_ts),
                  setup_id=f'{side}:{int(pull.iloc[peak_idx].ts)}', strategy_id=cfg.strategy_id,
                  signal_features=features, neighbor_score={},
                  explanation='Часовой тренд · откат к средним ценам · пробой трёх свечей на 5 минутах')
    try:
        make_setup_position('PREVIEW', signal, reference, int(asof)+1, 1., cfg)
    except ValueError as error:
        return None, str(error), context
    return signal, '', context


def make_setup_position(symbol, setup, raw_open, ts, qty, cfg):
    sign = direction(setup['side'])
    stop = float(setup['stop'])
    if any(not math.isfinite(x) or x <= 0 for x in (raw_open, stop, qty)):
        raise ValueError('stop_distance')
    slip, fee = cfg.slippage_bps/10000, cfg.fee_bps/10000
    entry = raw_open*(1+sign*slip)
    distance = sign*(entry-stop)
    if distance <= 0 or distance < .5*setup['atr']:
        raise ValueError('stop_distance')
    target = entry+sign*cfg.target_r*distance
    if target <= 0:
        raise ValueError('stop_distance')
    cost = (entry+stop)*fee + stop*slip + raw_open*slip
    if cost > cfg.max_cost_fraction*distance:
        raise ValueError('costs_too_high')
    obstacle = setup.get('obstacle')
    if obstacle is not None and sign*(obstacle-target) < setup.get('obstacle_buffer', 0.):
        raise ValueError('target_obstacle')
    slipped_stop = stop*(1-sign*slip)
    risk_unit = sign*(entry-slipped_stop)+fee*(entry+slipped_stop)
    p = Position(str(uuid.uuid4()), symbol, '5m', setup['side'], int(ts), entry, qty, qty,
                 stop, stop, target, target, int(ts+cfg.horizon*TF_MS['5m']-1),
                 qty*risk_unit, fee, slip, 0., mark=raw_open, funding_through=int(ts), fees=entry*qty*fee,
                 exit_mode='single', strategy_id=cfg.strategy_id, setup_id=setup.get('setup_id', ''),
                 signal_features=setup.get('signal_features', {}), neighbor_score=setup.get('neighbor_score', {}))
    p.fills.append({'kind': 'ENTRY', 'ts': int(ts), 'price': entry, 'qty': qty,
                    'fee': p.fees, 'model': 'scheduled_next_minute_open_adverse_slippage'})
    return p


def neighbor_diagnostic(trades, query, side, asof, neighbors=15):
    """Descriptive KNN on completed NEW-strategy trades. NEVER an entry veto.

    Overlapping outcomes are omitted to avoid counting a market-wide move many
    times. It is not a calibrated probability and starts empty on a new ledger.
    """
    eligible = [t for t in trades if t.get('strategy_id') == STRATEGY_ID and t['side'] == side
                and t['exit_ts'] < asof and all(k in t.get('signal_features', {}) for k in FEATURES)]
    if len(eligible) < neighbors:
        return {'samples': 0, 'available': len(eligible)}
    x = np.array([[t['signal_features'][k] for k in FEATURES] for t in eligible])
    q = np.array([query[k] for k in FEATURES])
    scale = np.maximum(np.percentile(x, 75, axis=0)-np.percentile(x, 25, axis=0), 1e-6)
    distances = np.linalg.norm((x-q)/scale, axis=1)
    chosen = []
    for index in np.argsort(distances, kind='stable'):
        trade = eligible[int(index)]
        if all(trade['exit_ts'] < t['entry_ts'] or trade['entry_ts'] > t['exit_ts'] for t in chosen):
            chosen.append(trade)
            if len(chosen) == neighbors:
                return {'samples': len(chosen), 'available': len(eligible),
                        'mean_r': float(np.mean([t['net_r'] for t in chosen])),
                        'win_fraction': sum(t['net_pnl'] > 0 for t in chosen)/len(chosen)}
    return {'samples': 0, 'available': len(eligible)}


def new_state(cfg: Config):
    return {'schema': 3, 'config': dataclasses.asdict(cfg), 'balance': cfg.initial_equity,
            'positions': {}, 'pending': {}, 'last_scanned': {}, 'rejects': {},
            'evaluated': {}, 'cooldowns': {}, 'streaks': {}, 'tf_pause_until': {},
            'day': None, 'day_start_equity': cfg.initial_equity, 'day_paused': False,
            'peak_equity': cfg.initial_equity, 'max_drawdown': 0.0,
            'dd_paused': False, 'enabled': True, 'scalp_enabled': True,
            'closed_count': 0, 'realized_net': 0.0, 'last_update_id': 0, 'panel_messages': {}, 'scheduled': {},
            'data_issues': {}, 'last_evaluation': {}, 'created_at': int(time.time()*1000),
            'used_setups': {}, 'universe': [], 'universe_at': 0, 'candidates': {},
            'archive_accounts': [], 'scan_cycles': 0, 'last_scan_close': 0}


def account_equity(state):
    result = state['balance']
    for raw in state['positions'].values():
        p = Position(**raw)
        exit_price = p.mark * (1 - direction(p.side) * p.slip_rate)
        result += p.remaining * (direction(p.side) * (exit_price - p.entry) - exit_price * p.fee_rate)
    return result


def update_risk_state(state, now_ms, cfg):
    equity = account_equity(state)
    day = time.strftime('%Y-%m-%d', time.gmtime(now_ms / 1000))
    if state['day'] != day:
        state['day'], state['day_start_equity'] = day, equity
        state['day_paused'] = False
    state['peak_equity'] = max(state['peak_equity'], equity)
    drawdown = 1 - equity / max(state['peak_equity'], 1e-12)
    state['max_drawdown'] = max(state['max_drawdown'], drawdown)
    if equity <= state['day_start_equity'] * (1 - cfg.daily_loss_limit):
        state['day_paused'] = True
    if drawdown >= cfg.drawdown_limit:
        state['dd_paused'] = True
    return equity


def portfolio_gate(state, symbol, tf, side, now_ms, cfg):
    update_risk_state(state, now_ms, cfg)
    if not state['enabled'] or (tf == '15m' and not state['scalp_enabled']):
        return 'manual_pause'
    if state['day_paused'] or state['dd_paused']:
        return 'account_loss_limit'
    if now_ms < state['tf_pause_until'].get(tf, 0):
        return 'timeframe_loss_streak'
    if now_ms < state['cooldowns'].get(f'{symbol}_{tf}', 0):
        return 'cooldown'
    positions = list(state['positions'].values())
    if any(p['symbol'] == symbol for p in positions):
        return 'symbol_already_open'
    pending = list(state['pending'].values())
    if any(p['symbol'] == symbol for p in pending):
        return 'symbol_pending'
    if len(positions) + len(pending) >= cfg.max_positions:
        return 'position_limit'
    if sum(p['side'] == side for p in positions+pending) >= cfg.max_same_direction:
        return 'direction_limit'
    return ''


def open_paper_position(state, pending, bar, cfg):
    symbol, tf, side = pending['symbol'], pending['tf'], pending['side']
    ts = int(bar['ts'])
    reason = portfolio_gate(state, symbol, tf, side, ts, cfg)
    if reason:
        return None, reason
    if abs(float(bar['open']) - pending['reference']) > cfg.max_entry_gap_atr * pending['atr']:
        return None, 'entry_gap'
    factory = (lambda qty: make_setup_position(symbol, pending, float(bar['open']), ts, qty, cfg)) if 'stop' in pending else (
        lambda qty: make_position(symbol, tf, side, float(bar['open']), pending['atr'], ts, qty, cfg))
    try:
        unit = factory(1.)
    except ValueError as error:
        return None, str(error)
    equity = account_equity(state)
    used_risk = sum(p['planned_risk'] for p in state['positions'].values())
    used_notional = sum(p['remaining'] * p['mark'] for p in state['positions'].values())
    reserved_risk = sum(p.get('reserved_risk', 0.) for p in state['pending'].values())
    reserved_notional = sum(p.get('reserved_notional', 0.) for p in state['pending'].values())
    budget = min(equity * cfg.risk_fraction, equity * cfg.max_total_risk - used_risk - reserved_risk)
    notional = min(equity * cfg.max_position_notional, equity * cfg.max_gross_notional - used_notional - reserved_notional)
    if pending.get('strategy_id') == cfg.strategy_id:
        related = pending.get('related_symbols', [])
        risk = sum(p['planned_risk'] for p in state['positions'].values() if p['symbol'] in related)
        risk += sum(p.get('reserved_risk', 0.) for p in state['pending'].values() if p['symbol'] in related)
        budget = min(budget, equity*cfg.correlated_risk_cap-risk)
        if budget <= 0:
            return None, 'correlated_budget'
    if budget <= 0 or notional <= 0:
        return None, 'portfolio_budget'
    qty = min(budget / unit.planned_risk, notional / unit.entry)
    if qty * unit.entry < 10:  # paper-only dust floor, not an exchange filter
        return None, 'position_too_small'
    p = factory(qty)
    state['balance'] -= p.fees
    state['positions'][p.id] = dataclasses.asdict(p)
    return p, ''


def settle_bar(state, position_id, bar, funding, cfg, update_account_risk=True):
    p = Position(**state['positions'][position_id])
    before = p.net_pnl
    step_position(p, bar, funding)
    state['balance'] += p.net_pnl - before
    if p.remaining:
        state['positions'][p.id] = dataclasses.asdict(p)
        closed = None
    else:
        del state['positions'][p.id]
        state['closed_count'] += 1
        state['realized_net'] += p.net_pnl
        state['streaks'][p.tf] = state['streaks'].get(p.tf, 0) + 1 if p.net_pnl < 0 else 0
        state['cooldowns'][f'{p.symbol}_{p.tf}'] = int(bar['close_ts']) + TF_MS[p.tf] * (4 if p.net_pnl < 0 else 1)
        if state['streaks'][p.tf] >= cfg.loss_streak_limit:
            state['tf_pause_until'][p.tf] = int(bar['close_ts']) + 24 * TF_MS[p.tf]
            state['streaks'][p.tf] = 0
        closed = dataclasses.asdict(p)
        closed['net_pnl'] = p.net_pnl
        closed['net_r'] = p.net_pnl / p.planned_risk
    if update_account_risk:
        update_risk_state(state, int(bar['close_ts']), cfg)
    return closed


def reject(state, tf, reason):
    bucket = state['rejects'].setdefault(tf, {})
    bucket[reason] = bucket.get(reason, 0) + 1


class Store:
    """State and closed trades commit together; one process, SQLite + RLock."""
    def __init__(self, path, cfg):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, body TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS trades (id TEXT PRIMARY KEY, body TEXT NOT NULL)')
        self.db.execute('INSERT OR IGNORE INTO state VALUES (1, ?)', (json.dumps(new_state(cfg)),))
        self.db.commit()
        saved = self.snapshot()
        if saved.get('schema') != 3 or saved['config'] != dataclasses.asdict(cfg):
            self.db.close()
            raise ValueError('Config differs from database. Use a NEW DATA_DIR for a new experiment.')

    def snapshot(self):
        with self.lock:
            return json.loads(self.db.execute('SELECT body FROM state WHERE id=1').fetchone()[0])

    def transact(self, callback):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                state = self.snapshot()
                result, closed = callback(state)
                for trade in closed:
                    self.db.execute('INSERT INTO trades VALUES (?, ?)',
                                    (trade['id'], json.dumps(trade, allow_nan=False)))
                self.db.execute('UPDATE state SET body=? WHERE id=1',
                                (json.dumps(state, allow_nan=False),))
                self.db.commit()
                return result
            except Exception:
                self.db.rollback()
                raise

    def trades(self):
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute('SELECT body FROM trades ORDER BY rowid')]

    def close(self):
        self.db.close()


def read_saved_state(path):
    """Read legacy state without modifying its database, including any live WAL."""
    if not path.exists():
        return None
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        row = db.execute('SELECT body FROM state WHERE id=1').fetchone()
        if row is None:
            raise ValueError('Existing database has no account state; preserve it for inspection')
        return json.loads(row[0])
    finally:
        db.close()


def open_signal_store(data_dir, cfg):
    """Separate strategy ledger; leave old databases intact and reuse menu IDs."""
    if cfg.market != 'spot_proxy':
        raise ValueError('This scanner requires the spot_proxy research profile')
    path = Path(data_dir) / 'paper_pullback_v3.sqlite3'
    first_start = not path.exists()
    store = Store(path, cfg)
    if first_start:
        archives, legacy = [], None
        for name in ('paper_spot_signals.sqlite3', 'paper_futures.sqlite3', 'paper.sqlite3'):
            old = Path(data_dir) / name
            if old.exists():
                saved = read_saved_state(old)
                archives.append({'file': name, 'closed_count': saved.get('closed_count', 0),
                                 'open_count': len(saved.get('positions', {}))})
                if legacy is None:
                    legacy = saved
        def migrate(s):
            s['archive_accounts'] = archives
            if legacy:
                for key in ('panel_messages', 'last_update_id', 'enabled'):
                    s[key] = legacy.get(key, s[key])
            return None, []
        store.transact(migrate)
    return store


class MarketClient:
    def __init__(self, market='spot_proxy'):
        self.market = market
        self.base = 'https://data-api.binance.vision' if market in ('spot', 'spot_proxy') else 'https://fapi.binance.com'
        self.prefix = '/api/v3' if market in ('spot', 'spot_proxy') else '/fapi/v1'
        self.frames = {}
        self.clock_offset = 0
        self.clock_checked = 0
        self.universe_checked = 0
        self.universe_symbols = []

    def get(self, path, **params):
        url = self.base + path + '?' + urllib.parse.urlencode(params)
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers={'User-Agent': 'PaperSignalBot/3'})
                with urllib.request.urlopen(req, timeout=15) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                if error.code == 418:
                    raise RuntimeError('Exchange IP restriction; pause and inspect') from None
                if error.code != 429 and error.code < 500:
                    raise
                delay = min(30, max(1, int(error.headers.get('Retry-After', '3'))))
                if attempt == 2:
                    raise
                time.sleep(delay)
            except (urllib.error.URLError, TimeoutError):
                if attempt == 2:
                    raise
                time.sleep(1 + attempt)
        raise RuntimeError('Unreachable request state')

    def now_ms(self):
        if time.monotonic() - self.clock_checked > 60:
            before = time.time() * 1000
            server = int(self.get(self.prefix + '/time')['serverTime'])
            after = time.time() * 1000
            self.clock_offset = server - (before + after) / 2
            self.clock_checked = time.monotonic()
        return int(time.time() * 1000 + self.clock_offset) - 1000  # closure guard

    def klines_range(self, symbol, tf, start, end):
        rows = []
        cursor = int(start)
        while cursor <= end:
            batch = self.get(self.prefix + '/klines', symbol=symbol, interval=tf,
                             startTime=cursor, endTime=int(end), limit=1000)
            if not isinstance(batch, list):
                raise ValueError('Invalid klines response')
            if not batch:
                break
            rows.extend(batch)
            following = int(batch[-1][0]) + 1
            if following <= cursor:
                raise ValueError('Non-advancing candle pagination')
            cursor = following
            if len(batch) < 1000:
                break
        return frame_from_klines(rows, int(end) + 1)

    def history(self, symbol, tf, now, limit):
        key = (symbol, tf)
        if key not in self.frames:
            rows, end = [], int(now - 1)
            while len(rows) < limit + 1:
                batch = self.get(self.prefix + '/klines', symbol=symbol, interval=tf,
                                 limit=min(1000, limit + 1 - len(rows)), endTime=end)
                if not batch:
                    break
                rows = batch + rows
                new_end = int(batch[0][0]) - 1
                if new_end >= end:
                    raise ValueError('Non-advancing candle history')
                end = new_end
            self.frames[key] = frame_from_klines(rows, now).tail(limit).reset_index(drop=True)
        else:
            old = self.frames[key]
            if old.empty:
                del self.frames[key]
                return self.history(symbol, tf, now, limit)
            start = int(old.iloc[-1].ts) + TF_MS[tf]
            if start + TF_MS[tf] > now:
                validate_bars(old, TF_MS[tf])
                return old
            fresh = self.klines_range(symbol, tf, start, now - 1)
            if not fresh.empty:
                self.frames[key] = pd.concat([old, fresh], ignore_index=True).drop_duplicates('ts').tail(limit).reset_index(drop=True)
        result = self.frames[key]
        validate_bars(result, TF_MS[tf])
        return result

    def spread_bps(self, symbol):
        quote = self.get(self.prefix + '/ticker/bookTicker', symbol=symbol)
        bid, ask = float(quote['bidPrice']), float(quote['askPrice'])
        if not all(math.isfinite(x) for x in (bid, ask)) or not 0 < bid <= ask:
            raise ValueError('Invalid spread quote')
        return 10000 * (ask - bid) / ((ask + bid) / 2)

    def liquid_universe(self, cfg):
        """Hourly refresh from present public spot listings, volume and spread.

        This selection is for LIVE scanning only. A historical test must choose
        its symbols using information that was available in that period.
        """
        if self.universe_symbols and time.monotonic()-self.universe_checked < 3600:
            return self.universe_symbols
        listings = self.get(self.prefix+'/exchangeInfo')['symbols']
        excluded = {'USDC', 'FDUSD', 'TUSD', 'USDP', 'DAI', 'EUR', 'EURC', 'USD1', 'USDE',
                    'PAXG', 'XAUT', 'WBTC', 'WBETH', 'WETH', 'BETH'}
        allowed = {r['symbol'] for r in listings if r.get('status') == 'TRADING'
                   and r['symbol'] in CRYPTO_POOL
                   and r.get('quoteAsset') == 'USDT' and r.get('isSpotTradingAllowed', False)
                   and r.get('baseAsset') not in excluded
                   and not r.get('baseAsset', '').endswith(('UP', 'DOWN', 'BULL', 'BEAR'))}
        quotes = {r['symbol']: r for r in self.get(self.prefix+'/ticker/bookTicker')}
        ranked = []
        for row in self.get(self.prefix+'/ticker/24hr'):
            symbol = row['symbol']
            if symbol not in allowed or symbol not in quotes:
                continue
            volume = float(row['quoteVolume'])
            bid, ask = float(quotes[symbol]['bidPrice']), float(quotes[symbol]['askPrice'])
            if not all(math.isfinite(x) for x in (volume, bid, ask)) or not 0 < bid <= ask:
                continue
            if volume >= cfg.min_quote_volume and 20000*(ask-bid)/(ask+bid) <= cfg.max_spread_bps:
                ranked.append((volume, symbol))
        ranked.sort(reverse=True)
        if not ranked:
            raise ValueError('No eligible liquid markets; do not silently trade a fallback list')
        self.universe_symbols = [symbol for _, symbol in ranked[:cfg.universe_size]]
        self.universe_checked = time.monotonic()
        # Bound memory if the daily liquidity ranking changes repeatedly.
        keep = set(self.universe_symbols)
        self.frames = {key: frame for key, frame in self.frames.items() if key[0] in keep}
        return self.universe_symbols

    def funding(self, symbol, start, end):
        if self.market in ('spot', 'spot_proxy'):
            return []  # Spot proxy has no futures funding feed; UI labels this omission.
        events, cursor = [], int(start)
        while cursor <= end:
            batch = self.get('/fapi/v1/fundingRate', symbol=symbol, startTime=cursor,
                             endTime=int(end), limit=1000)
            if not isinstance(batch, list):
                raise ValueError('Invalid funding response')
            events.extend(batch)
            if not batch or len(batch) < 1000:
                break
            following = int(batch[-1]['fundingTime']) + 1
            if following <= cursor:
                raise ValueError('Non-advancing funding pagination')
            cursor = following
        return events


def summarize_trades(trades):
    pnl = [float(t['net_pnl']) for t in trades]
    profits, losses = sum(x for x in pnl if x > 0), -sum(x for x in pnl if x < 0)
    return {'trades': len(pnl), 'wins': sum(x > 0 for x in pnl),
            'winrate_pct': 100 * sum(x > 0 for x in pnl) / len(pnl) if pnl else 0,
            'net_pnl': sum(pnl), 'profit_factor': profits / losses if losses else None,
            'average_r': float(np.mean([t['net_r'] for t in trades])) if trades else 0,
            'fees': sum(t['fees'] for t in trades),
            'funding_paid': sum(t['funding_paid'] for t in trades)}


def report(store, cfg):
    with store.lock:
        state = store.snapshot()
    return overview(state, cfg, account_equity(state))


class TelegramAPIError(RuntimeError):
    def __init__(self, code, description):
        self.code = code
        self.description = description
        super().__init__(f'Telegram API error {code}')  # Never include token-bearing URLs.


class Telegram:
    def __init__(self):
        self.token = os.getenv('TELEGRAM_TOKEN', '')
        self.admins = {int(x.strip()) for x in (os.getenv('ADMIN_CHAT_IDS', '').strip() or '8299008675').split(',') if x.strip()}
        self.channel = os.getenv('SIGNAL_CHANNEL_ID', '')
        if self.token and not self.admins:
            raise ValueError('Set ADMIN_CHAT_IDS; no public access is granted by default')

    def call(self, method, data):
        if not self.token:
            return None
        req = urllib.request.Request('https://api.telegram.org/bot' + self.token + '/' + method,
                                     data=json.dumps(data).encode(),
                                     headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=35) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            code = error.code
            try:
                result = json.loads(error.read())
            except (ValueError, OSError):
                result = {}
            raise TelegramAPIError(code, str(result.get('description', 'Request rejected'))) from None
        if not result.get('ok'):
            raise TelegramAPIError(result.get('error_code', 0), str(result.get('description', 'Request rejected')))
        return result.get('result')

    def edit_panel(self, chat, message_id, text, markup):
        try:
            self.call('editMessageText', {'chat_id': chat, 'message_id': message_id,
                      'text': text, 'parse_mode': 'HTML', 'reply_markup': markup})
        except TelegramAPIError as error:
            if error.code == 400 and 'message is not modified' in error.description.lower():
                return True
            if error.code == 400 and any(phrase in error.description.lower() for phrase in
                    ('message to edit not found', "message can't be edited", 'message_id_invalid')):
                return False
            raise
        return True

    def answer(self, callback_id, text=None):
        data = {'callback_query_id': callback_id}
        if text:
            data['text'] = text[:180]
        try:
            self.call('answerCallbackQuery', data)
        except (TelegramAPIError, urllib.error.URLError, TimeoutError):
            # An expired callback must not prevent the panel from being refreshed.
            LOG.warning('Could not acknowledge Telegram button')

    def send(self, chat, message, menu=False):
        chunks = text_pages(message, 2800)
        for i, chunk in enumerate(chunks):
            data = {'chat_id': chat, 'text': panel_html(chunk), 'parse_mode': 'HTML'}
            if i == len(chunks)-1:
                matches = re.findall(r'^(?:Вход|Стоп|Цель): `([0-9.]+)`', message, re.M)
                if len(matches) == 3:
                    ticket = message.splitlines()[1]+'\nВход: '+matches[0]+'\nСтоп: '+matches[1]+'\nЦель: '+matches[2]
                    data['reply_markup'] = {'inline_keyboard': [
                        [copy_button('📋 Вход', matches[0]), copy_button('📋 Стоп', matches[1])],
                        [copy_button('📋 Цель', matches[2]), copy_button('📋 Все уровни', ticket)]]}
                elif menu:
                    data['reply_markup'] = keyboard()
            self.call('sendMessage', data)

    def document(self, chat, name, content):
        boundary = 'paperbot' + uuid.uuid4().hex
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n{chat}\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{name}"\r\n'
                'Content-Type: text/csv\r\n\r\n').encode() + content + f'\r\n--{boundary}--\r\n'.encode()
        req = urllib.request.Request('https://api.telegram.org/bot'+self.token+'/sendDocument', data=body,
                                     headers={'Content-Type': 'multipart/form-data; boundary='+boundary})
        with urllib.request.urlopen(req, timeout=60) as response:
            result = json.load(response)
        if not result.get('ok'):
            raise RuntimeError('Telegram export rejected')

    def notify(self, message):
        for recipient in set(self.admins) | ({self.channel} if self.channel else set()):
            try:
                self.send(recipient, message)
            except Exception as error:
                # Do not log exception URLs: a Telegram URL contains the token.
                LOG.warning('Telegram notification failed (%s); trade still recorded', type(error).__name__)


def market_error_text(error):
    if isinstance(error, urllib.error.HTTPError):
        if error.code in (403, 418, 451):
            return f'Биржа ограничила доступ с сервера (HTTP {error.code}); проверь доступность публичных данных Binance в регионе сервера'
        if error.code == 429:
            return 'Биржа временно ограничила частоту запросов'
        return f'Не удалось получить ответ биржи (HTTP {error.code})'
    if isinstance(error, (urllib.error.URLError, TimeoutError)):
        return 'Нет связи с биржей или превышено время ожидания ответа'
    if isinstance(error, RuntimeError) and 'IP restriction' in str(error):
        return 'Биржа ограничила IP сервера; новые входы остановлены'
    if isinstance(error, ValueError):
        return 'Данные цены неполные или некорректные; входы по ним не назначаются'
    return 'Ошибка обработки данных (' + type(error).__name__ + ')'


def entry_message(p):
    lines = ['✅ УЧЕБНЫЙ ВХОД РАССЧИТАН', p.symbol+' · '+SIDES[p.side], '']
    lines += level_lines(p.entry, p.stop, p.tp1, p.tp2)
    lines += ['', 'Одна цель · закрытие всей сделки', f'Планируемый риск: {p.planned_risk:.2f} USDT',
              f'Расчётный объём: {p.qty:.8g}', 'Время входа: '+stamp(p.entry_ts), '',
              'Расчёт сделан после завершения минуты. Это не текущая цена.',
              'Цены Binance Spot; перед ручным входом сверь фьючерс. Ордера не отправлялись.']
    return '\n'.join(lines)

def closed_message(t):
    return '\n'.join(['🏁 СДЕЛКА ЗАВЕРШЕНА', t['symbol']+' · '+SIDES[t['side']], '',
        f'Итог после расходов: {t["net_pnl"]:+.2f} USDT',
        'Причина: '+EXITS.get(t['exit_reason'], 'другое условие'),
        'Закрытие: '+stamp(t['exit_ts']), '',
        'Учебный результат по споту. Фьючерсные платежи не учтены.'])

class Runtime:
    def __init__(self, store, cfg, client=None, telegram=None):
        self.store, self.cfg = store, cfg
        self.client = client or MarketClient(cfg.market)
        self.telegram = telegram or Telegram()
        self.running = threading.Event()
        self.running.set()
        self.last_success = 0.0
        self.last_error = ''
        self.last_attempt = 0.0
        self.activity = 'Запуск; ожидание первой проверки'
        self.warm_retry_at = {}

    def monitor(self, now):
        state = self.store.snapshot()
        starts = {}
        for p in state['positions'].values():
            start = p['entry_ts'] if p['last_bar_ts'] < 0 else p['last_bar_ts'] + MINUTE
            starts[p['symbol']] = min(starts.get(p['symbol'], start), start)
        for p in state['pending'].values():
            start = p['entry_ts']
            starts[p['symbol']] = min(starts.get(p['symbol'], start), start)
        replay, funding_by_symbol = [], {}
        for symbol, start in starts.items():
            if start + MINUTE > now:
                continue
            bars = self.client.klines_range(symbol, '1m', start, now - 1)
            if bars.empty or int(bars.iloc[0].ts) != start:
                raise ValueError('Missing first unprocessed 1m candle')
            validate_bars(bars, MINUTE)
            if int(bars.iloc[-1].close_ts) != (now // MINUTE) * MINUTE - 1:
                raise ValueError('Stale minute feed; pause new entries')
            funding_by_symbol[symbol] = self.client.funding(symbol, start, int(bars.iloc[-1].close_ts))
            replay.extend((int(bar['ts']), symbol, bar) for bar in bars.to_dict('records'))
        # Replay a shared chronology, including after downtime. Processing all BTC
        # bars and then all ETH bars would leak BTC's future equity into ETH's past.
        replay.sort(key=lambda item: (item[0], item[1]))

        def apply(s):
            import itertools
            closed, notes = [], []
            for ts, group in itertools.groupby(replay, key=lambda item: item[0]):
                minute = list(group)
                update_risk_state(s, ts, self.cfg)
                for _, symbol, bar in minute:
                    for key, pending in list(s['pending'].items()):
                        if pending['symbol'] != symbol or pending['entry_ts'] > bar['ts']:
                            continue
                        del s['pending'][key]
                        if pending['entry_ts'] != bar['ts']:
                            reject(s, pending['tf'], 'missed_scheduled_entry')
                            continue
                        p, reason = open_paper_position(s, pending, bar, self.cfg)
                        if reason:
                            reject(s, pending['tf'], reason)
                            notes.append(f'ВХОД ОТМЕНЁН · {symbol}\n'+REASONS.get(reason,'Условия входа изменились'))
                        else:
                            notes.append(entry_message(p))
                for _, symbol, bar in minute:
                    for pid, raw in list(s['positions'].items()):
                        if raw['symbol'] != symbol:
                            continue
                        trade = settle_bar(s, pid, bar, funding_by_symbol[symbol], self.cfg,
                                           update_account_risk=False)
                        if trade:
                            closed.append(trade)
                            notes.append(closed_message(trade))
                update_risk_state(s, ts + MINUTE - 1, self.cfg)
            return notes, closed

        messages = self.store.transact(apply) if replay else []
        for message in messages:
            self.telegram.notify(message)

    def record_data_issue(self, symbol, tf, message):
        def apply(s):
            s.setdefault('data_issues', {})[f'{symbol}_{tf}'] = {'message':message,'at':int(time.time()*1000)}
            return None, []
        self.store.transact(apply)

    def related_symbols(self, symbol, side, asof):
        state = self.store.snapshot()
        related = []
        own = self.client.frames.get((symbol, '1h'), pd.DataFrame())
        for held in list(state['positions'].values())+list(state['pending'].values()):
            other = self.client.frames.get((held['symbol'], '1h'), pd.DataFrame())
            same_side = side == held['side']
            correlated = same_side  # Conservative fallback when covariance is unknown.
            if not own.empty and not other.empty:
                a = own.loc[own.close_ts <= asof].set_index('ts').close.pct_change().tail(96)
                b = other.loc[other.close_ts <= asof].set_index('ts').close.pct_change().tail(96)
                pair = pd.concat([a, b], axis=1).dropna()
                if len(pair) >= 48 and (pair.std() > 1e-12).all():
                    rho = float(pair.iloc[:, 0].corr(pair.iloc[:, 1]))
                    if math.isfinite(rho):
                        correlated = rho*direction(side)*direction(held['side']) >= self.cfg.correlation_threshold
            if correlated:
                related.append(held['symbol'])
        return related

    def fetch_setup_frames(self, symbol, now):
        return {tf: self.client.history(symbol, tf, now, self.cfg.feature_history) for tf in STRATEGY_TFS}

    def scan_one(self, symbol, tf, now, frames=None):
        tf = '5m'  # One strategy; the other frames provide context, not extra entries.
        state = self.store.snapshot()
        key = f'{symbol}_{tf}'
        latest_close = (now//TF_MS[tf])*TF_MS[tf]-1
        if state['last_scanned'].get(key, -1) >= latest_close or not state['enabled']:
            return
        if now-latest_close > self.cfg.signal_lifetime_ms:
            return
        self.activity = f'Проверяю {symbol}: направление → откат → вход'
        frames = frames if frames is not None else self.fetch_setup_frames(symbol, now)
        signal, reason, context = evaluate_setup(frames, latest_close, self.cfg)
        if signal and self.client.spread_bps(symbol) > self.cfg.max_spread_bps:
            signal, reason = None, 'spread'
        if signal:
            signal['neighbor_score'] = neighbor_diagnostic(self.store.trades(), signal['signal_features'],
                                                          signal['side'], latest_close, self.cfg.neighbors)
            signal['related_symbols'] = self.related_symbols(symbol, signal['side'], latest_close)
        decision_now = self.client.now_ms()

        def decide(s):
            if s['last_scanned'].get(key, -1) >= latest_close:
                return None, []
            s['last_scanned'][key] = latest_close
            s['evaluated'][tf] = s['evaluated'].get(tf, 0)+1
            s['data_issues'].pop(key, None)
            s['candidates'][symbol] = dict(context, at=latest_close)
            why = reason
            if signal:
                why = portfolio_gate(s, symbol, tf, signal['side'], decision_now, self.cfg)
                setup_key = symbol+':'+signal['setup_id']
                if setup_key in s['used_setups']:
                    why = 'setup_used'
                if decision_now-latest_close > self.cfg.signal_lifetime_ms:
                    why = 'stale_signal'
                if not why:
                    entry_ts = ((decision_now+1000)//MINUTE+1)*MINUTE
                    pending = dict(signal, symbol=symbol, tf=tf, entry_ts=entry_ts)
                    # Reserve the same budget the fill engine would use, but never
                    # open a position until the scheduled minute actually exists.
                    preview, why = open_paper_position(json.loads(json.dumps(s)), pending,
                                                       {'ts': entry_ts, 'open': pending['reference']}, self.cfg)
                    if preview:
                        pending['reserved_risk'] = preview.planned_risk
                        pending['reserved_notional'] = preview.qty*preview.entry
                        s['pending'][key] = pending
                        s['used_setups'][setup_key] = latest_close
                        s['used_setups'] = {k:v for k,v in s['used_setups'].items() if v > latest_close-7*86400000}
                        s['scheduled'][tf] = s['scheduled'].get(tf, 0)+1
                        s['last_evaluation'][key] = {'at': decision_now, 'outcome': 'scheduled'}
                        s['candidates'][symbol]['outcome'] = 'scheduled'
                        return pending, []
            reject(s, tf, why or 'no_signal')
            s['last_evaluation'][key] = {'at': decision_now, 'outcome': why or 'no_signal'}
            s['candidates'][symbol]['outcome'] = why or 'no_signal'
            return None, []
        pending = self.store.transact(decide)
        outcome = self.store.snapshot()['last_evaluation'].get(key, {}).get('outcome', 'unknown')
        LOG.info('Signal check %s 5m: %s', symbol, outcome)
        if pending:
            self.telegram.notify(signal_message(symbol, tf, pending, self.cfg))

    def scan(self, now):
        state = self.store.snapshot()
        if not state['enabled']:
            return
        latest_close = (now//TF_MS['5m'])*TF_MS['5m']-1
        if now-latest_close > self.cfg.signal_lifetime_ms:
            return
        symbols = self.client.liquid_universe(self.cfg)
        def remember(s):
            s['universe'], s['universe_at'] = list(symbols), int(now)
            s['candidates'] = {k:v for k,v in s['candidates'].items() if k in symbols}
            s['data_issues'] = {k:v for k,v in s['data_issues'].items() if k.rsplit('_', 1)[0] in symbols}
            return None, []
        self.store.transact(remember)
        todo = [symbol for symbol in symbols if state['last_scanned'].get(symbol+'_5m', -1) < latest_close]
        if not todo:
            return
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix='candles') as pool:
            jobs = {pool.submit(self.fetch_setup_frames, symbol, now): symbol for symbol in todo}
            for future in as_completed(jobs):
                symbol = jobs[future]
                try:
                    self.scan_one(symbol, '5m', now, future.result())
                except Exception as error:
                    message = market_error_text(error)
                    self.record_data_issue(symbol, '5m', message)
                    LOG.warning('Scan failed %s: %s', symbol, message)
                    if ((isinstance(error, urllib.error.HTTPError) and error.code in (403, 418, 429, 451))
                            or isinstance(error, RuntimeError) and 'IP restriction' in str(error)):
                        for job in jobs:
                            job.cancel()
                        raise
        def finish(s):
            if s['last_scan_close'] != latest_close and any(v['at'] == latest_close for v in s['candidates'].values()):
                s['scan_cycles'] += 1
                s['last_scan_close'] = latest_close
            return None, []
        self.store.transact(finish)
        snapshot = self.store.snapshot()
        counts = Counter(v.get('outcome', 'waiting') for v in snapshot['candidates'].values() if v['at'] == latest_close)
        LOG.info('Scan complete candles=%s markets=%s results=%s', latest_close, len(todo), dict(counts))

    def warm_one_history(self, now):
        # Preload one market between decision windows; no 2,500-candle KNN warmup.
        if not hasattr(self.client, 'frames') or now % TF_MS['5m'] < self.cfg.signal_lifetime_ms:
            return
        if TF_MS['5m']-now % TF_MS['5m'] < 30_000:
            return
        state = self.store.snapshot()
        if not state['enabled']:
            return
        for symbol in self.client.liquid_universe(self.cfg):
            if any((symbol, tf) not in self.client.frames for tf in STRATEGY_TFS):
                if self.warm_retry_at.get(symbol, 0) > time.monotonic():
                    continue
                self.warm_retry_at[symbol] = time.monotonic()+300
                self.activity = f'Готовлю историю {symbol}'
                self.fetch_setup_frames(symbol, now)
                return

    def work(self):
        while self.running.is_set():
            self.last_attempt=time.time()
            try:
                self.activity='Проверяю открытые сделки'
                now=self.client.now_ms()
                # Missing position data blocks new entries until monitoring succeeds.
                self.monitor(now)
                self.scan(self.client.now_ms())
                self.warm_one_history(self.client.now_ms())
                self.last_success=time.time()
                issues=self.store.snapshot().get('data_issues',{})
                self.last_error=f'Есть проблемы с данными: {len(issues)}. См. «Почему нет входа».' if issues else ''
                self.activity='Ожидаю завершения следующего ценового интервала'
            except Exception as error:
                self.last_error=market_error_text(error)
                self.activity='Повторю получение данных; новые входы пока не назначаются'
                LOG.warning('Market cycle failed: %s',self.last_error)
            self.running_wait(10)

    def handle_action(self, action):
        s = self.store.snapshot()
        if action.startswith('AN_') and action[3:] in PERIODS:
            return analytics(self.store.trades(), action[3:])
        if action.startswith('DETAILS_') and action[8:] in PERIODS:
            return breakdown(self.store.trades(), action[8:])
        if action == 'STATUS':
            lines = ['📡 СВЯЗЬ И ОБНОВЛЕНИЯ', f'Версия {BOT_VERSION}', '',
                     'Источник: публичные свечи Binance Spot', 'Направления: LONG и SHORT',
                     'Часовой тренд → откат 15м → вход 5м', '',
                     'Последний цикл: '+stamp(self.last_success*1000),
                     'Последняя оценка рынка: '+stamp(s.get('last_scan_close', 0)),
                     'Сейчас: '+self.activity, 'Ошибка: '+(self.last_error or 'нет'),
                     f'Монет в обзоре: {len(s.get("universe", []))}',
                     f'Проверок рынка: {s.get("scan_cycles", 0)}', '',
                     'МОНЕТЫ', ', '.join(s.get('universe', [])) or 'Загружаем список']
            if s.get('data_issues'):
                lines += ['', 'ПРОБЛЕМЫ С ДАННЫМИ']
                for key, value in list(s['data_issues'].items())[:8]:
                    lines.append(key.replace('_', ' · ')+': '+value['message'])
            lines += ['', 'Новая стратегия считает результаты отдельно от прежних правил.',
                      f'Архивов прежних счетов на сервере: {len(s.get("archive_accounts", []))}',
                      'На бесплатном Render история локальная и может исчезнуть при перезапуске или развёртывании.' if os.getenv('RENDER') and not os.getenv('RENDER_DISK_ID') else 'История хранится в папке DATA_DIR. Для сохранения при замене сервера нужен постоянный диск.']
            return '\n'.join(lines)
        if action in ('SHOW_STATS', 'MENU'):
            return report(self.store, self.cfg)
        if action.startswith('TRADE|'):
            raw = s['positions'].get(action.split('|', 1)[1])
            return trade_card(raw) if raw else '🏁 Сделка уже закрыта\nПосмотри результат в истории. Старые уровни не используются для нового входа.'
        if action == 'SHOW_LIVE_TRADES':
            return live_report(s)
        if action == 'SHOW_REJECTS':
            return why_report(s)
        if action == 'SHOW_HISTORY':
            return history_report(self.store.trades())
        if action == 'STRATEGY':
            return strategy_report()
        if action == 'CHECK':
            return historical_check_report()
        if action == 'HELP':
            return GUIDE
        if action in ('SHOW_AI_MEMORY', 'RISK'):
            return risk_report(s, self.cfg)
        if action == 'CONTROLS':
            return ('⚙️ УПРАВЛЕНИЕ ПОИСКОМ\n\n'
                    f'Поиск новых сделок: {"включён" if s["enabled"] else "выключен"}\n'
                    'Стратегия: направление 1ч → откат 15м → вход 5м\n\n'
                    'Выключение отменяет ожидающие входы. Уже открытые сделки продолжают отслеживаться.\n'
                    'Включение поиска не отменяет защитные паузы после потерь.')
        if action in ('SIGNALS_ON', 'SIGNALS_OFF', 'SCALP_ON', 'SCALP_OFF', 'TOGGLE_SIGNALS', 'TOGGLE_SCALP'):
            key = 'enabled'
            # Explicit on/off prevents a repeated click from reversing the action.
            def change(state):
                enabled = not state[key] if action.startswith('TOGGLE_') else action.endswith('_ON')
                state[key] = enabled
                if not enabled:
                    state['pending'] = {k:p for k,p in state['pending'].items()
                                        if key == 'scalp_enabled' and p['tf'] != '15m'}
                return None, []
            self.store.transact(change)
            return self.handle_action('CONTROLS')
        return report(self.store, self.cfg)

    def make_panel(self, action, page=0):
        aliases = {'SHOW_STATS':'MENU', 'DETAILS':'DETAILS_ALL', 'SHOW_AI_MEMORY':'RISK'}
        action = aliases.get(action, action)
        allowed = {'MENU','STATUS','SHOW_LIVE_TRADES','SHOW_REJECTS','SHOW_HISTORY','HELP','RISK','CONTROLS','STRATEGY','CHECK'}
        allowed |= {'AN_'+p for p in PERIODS} | {'DETAILS_'+p for p in PERIODS}
        controls = {'SIGNALS_ON','SIGNALS_OFF','SCALP_ON','SCALP_OFF','TOGGLE_SIGNALS','TOGGLE_SCALP'}
        if action.startswith('TRADE|'):
            allowed.add(action)
        if action in controls:
            text = self.handle_action(action)
            action = 'CONTROLS'
        else:
            action = action if action in allowed else 'MENU'
            text = self.handle_action(action)
        pages = text_pages(text)
        if action == 'SHOW_LIVE_TRADES':
            card_pages = (len(self.store.snapshot()['positions'])+4)//5
            while len(pages) < card_pages:
                pages.append('📌 КАРТОЧКИ СДЕЛОК\nВыбери монету кнопкой ниже. Все уровни — в карточке.')
        page = min(max(page, 0), len(pages)-1)
        text = pages[page]
        if len(pages)>1:
            if page>0:
                text = 'ПРОДОЛЖЕНИЕ\n\n'+text
            text += f'\n\nСтраница {page+1} из {len(pages)}'
        return panel_html(text), panel_keyboard(action, page, len(pages), self.store.snapshot())

    def save_panel(self, chat, message_id):
        def remember(s):
            s.setdefault('panel_messages', {})[str(chat)] = message_id
            return None, []
        self.store.transact(remember)

    def process_update(self, update):
        # Persist receipt before side effects so a replay cannot repeat a control.
        def receive(state):
            if int(update['update_id']) < state['last_update_id']:
                return False, []
            state['last_update_id'] = int(update['update_id'])+1
            return True, []
        if not self.store.transact(receive):
            return
        callback = update.get('callback_query')
        msg = callback.get('message', {}) if callback else update.get('message', {})
        sender = callback.get('from', {}) if callback else msg.get('from', {})
        chat = msg.get('chat', {}).get('id')
        if sender.get('id') not in self.telegram.admins or chat is None:
            if callback:
                self.telegram.answer(callback['id'], 'Доступ только для владельца бота.')
            return
        if not callback:
            command = msg.get('text','').split(' ',1)[0].split('@',1)[0].lower()
            if command not in ('/start','/menu','/help','/status'):
                return
            action = {'/help':'HELP','/status':'STATUS'}.get(command,'MENU')
        else:
            action = callback.get('data','MENU')
            self.telegram.answer(callback['id'])
        if action.startswith('SAVE|'):
            raw = self.store.snapshot()['positions'].get(action.split('|', 1)[1])
            if raw:
                self.telegram.send(chat, trade_card(raw))
            else:
                self.telegram.send(chat, '🏁 Эта сделка уже закрыта. Итог доступен в истории.')
            return
        page = 0
        if action.startswith('PAGE|'):
            try:
                action, raw_page = action[5:].rsplit('|', 1)
                page = max(0,int(raw_page))
            except (ValueError, TypeError):
                action, page = 'MENU', 0
        if action == 'EXPORT_CSV':
            # An explicitly requested file is a separate document, not another menu.
            self.telegram.document(chat, 'trades.csv', ledger_csv(self.store.trades()))
            return
        text, markup = self.make_panel(action, page)
        message_id = msg.get('message_id') if callback else self.store.snapshot().get('panel_messages',{}).get(str(chat))
        if message_id is not None:
            if self.telegram.edit_panel(chat, message_id, text, markup):
                self.save_panel(chat, message_id)
                return
            if callback:
                # Never turn every failed edit into chat spam. /start recreates a deleted panel.
                self.telegram.answer(callback['id'], 'Это меню больше нельзя обновить. Отправь /start.')
                return
        result = self.telegram.call('sendMessage', {'chat_id':chat,'text':text,'parse_mode':'HTML','reply_markup':markup})
        if result and result.get('message_id') is not None:
            self.save_panel(chat,result['message_id'])

    def telegram_work(self):
        while self.running.is_set() and self.telegram.token:
            try:
                offset = self.store.snapshot()['last_update_id']
                updates = self.telegram.call('getUpdates', {'offset':offset, 'timeout':20,
                                             'allowed_updates':['message','callback_query']}) or []
                for update in updates:
                    try:
                        self.process_update(update)
                    except Exception as error:
                        LOG.warning('Telegram action failed (%s)',type(error).__name__)
            except Exception as error:
                LOG.warning('Telegram polling error (%s)',type(error).__name__)
                self.running_wait(5)

    def running_wait(self, seconds):
        until = time.monotonic()+seconds
        while self.running.is_set() and time.monotonic()<until:
            time.sleep(min(.2,max(0,until-time.monotonic())))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', action='store_true', help='Print local paper stats; no network calls')
    parser.add_argument('--export', help='Export entire closed-trade ledger as JSON; no network calls')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    cfg = Config.from_env()
    data_dir = Path(os.getenv('DATA_DIR', 'data_v2'))
    store = open_signal_store(data_dir, cfg)
    if args.report or args.export:
        if args.report:
            print(report(store, cfg))
        if args.export:
            Path(args.export).write_text(json.dumps(store.trades(), ensure_ascii=False, indent=2), encoding='utf-8')
        store.close()
        return
    # Render/Linux and macOS release flock automatically even after a crash.
    import fcntl
    lock_handle = (data_dir / 'running.lock').open('a+')
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_handle.close()
        store.close()
        raise SystemExit('Another bot already uses this DATA_DIR; stop it before starting this one.')
    lock_handle.seek(0)
    lock_handle.truncate()
    lock_handle.write(str(os.getpid()))
    lock_handle.flush()
    runtime = None
    try:
        runtime = Runtime(store, cfg)
        started_at = time.time()
        # Render briefly overlaps old and new instances during a rollout. Give
        # the old poller its drain interval before activating this single bot.
        delay = float(os.getenv('STARTUP_DELAY_SECONDS', '75' if os.getenv('RENDER') else '0'))
        def start_worker(target):
            if delay:
                runtime.running_wait(delay)
            if runtime.running.is_set():
                target()
        workers = [threading.Thread(target=start_worker, args=(target,), daemon=True)
                   for target in (runtime.work, runtime.telegram_work)]
        for worker in workers:
            worker.start()
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Health(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path not in ('/', '/healthz'):
                    self.send_error(404)
                    return
                fresh = time.time() - runtime.last_success < 180
                starting = not runtime.last_attempt or (not runtime.last_success and time.time()-started_at < 300 and not runtime.last_error)
                snapshot = store.snapshot()
                evaluated = snapshot.get('last_scan_close', 0)
                data_fresh = bool(evaluated and time.time()*1000-evaluated < 7*MINUTE)
                body = json.dumps({'mode': 'paper', 'market': cfg.market, 'version': BOT_VERSION, 'healthy': fresh,
                                   'last_success': runtime.last_success,
                                   'market_data_fresh': data_fresh, 'last_scan_close': evaluated,
                                   'markets': len(snapshot.get('universe', [])),
                                   'checks': snapshot['evaluated'].get('5m', 0),
                                   'fee_bps': cfg.fee_bps,
                                   'data_issues': len(snapshot.get('data_issues', {})),
                                   'error_type': runtime.last_error}).encode()
                self.send_response(200 if fresh or starting else 503)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                return

        server = ThreadingHTTPServer(('0.0.0.0', int(os.getenv('PORT', '10000'))), Health)
        import signal
        def stop_service(signum, frame):
            runtime.running.clear()
            threading.Thread(target=server.shutdown, daemon=True).start()
        signal.signal(signal.SIGTERM, stop_service)
        signal.signal(signal.SIGINT, stop_service)
        LOG.info('PAPER ONLY. Version=%s strategy=%s market=%s startup_delay=%s', BOT_VERSION, STRATEGY_ID, cfg.market, delay)
        server.serve_forever()
        server.server_close()
        for worker in workers:
            worker.join(timeout=2)
    finally:
        if runtime:
            runtime.running.clear()
        lock_handle.close()


if __name__ == '__main__':
    main()

