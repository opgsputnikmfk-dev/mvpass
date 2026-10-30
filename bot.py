"""Telegram signal bot v2.3 using spot candles as a futures-signal reference. PAPER ONLY: no order API and no exchange credentials.

Deployment: replace only bot.py; keep the existing requirements.txt and TELEGRAM_TOKEN.
The supplied owner ID is the fallback when ADMIN_CHAT_IDS is absent/empty.
This version uses public spot candles to generate LONG/SHORT reference signals.
It does not fetch futures quotes or funding. Compare levels with the target contract.
MARKET from older deployments is ignored. A separate paper_spot_signals.sqlite3 is used.
Existing spot/futures accounts remain untouched; this is a different research model.
Keep DATA_DIR on persistent storage to retain history across server replacements.
Telegram /start or /menu opens one editable panel; alerts and CSV remain separate.
Python 3.12 and the previous numpy/pandas requirements are supported.

Signals use closed candles. Paper fills are committed for the next full minute
before its open is observed and settled using closed 1m OHLC. Intrabar order is
unknown: an existing stop wins a tie; a new break-even stop starts next bar.
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
from collections import Counter, defaultdict
from datetime import datetime, timezone

BOT_VERSION = '2.3 · Сигналы по споту'
PERIODS = {'ALL': 'Всё время', 'TODAY': 'Сегодня · UTC', '7D': 'Последние 7 дней', '30D': 'Последние 30 дней'}
MODES = {'15m': 'Быстрые сделки · 15 мин', '1h': 'Внутри дня · 1 час', '4h': 'Длительные сделки · 4 часа'}
SIDES = {'LONG': 'На рост · LONG', 'SHORT': 'На падение · SHORT'}
EXITS = {'SL': 'Защитный стоп', 'BE': 'Защитный стоп после первой цели',
         'TP1': 'Первая цель', 'TP2': 'Вторая цель', 'TIMEOUT': 'Истекло время ожидания',
         'END_OF_DATA': 'Конец данных проверки'}
REASONS = {
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
    open_result = sum(p['gross_pnl']-p['fees']-p['funding_paid'] for p in positions) + equity-state['balance']
    pauses = []
    if not state['enabled']: pauses.append('поиск выключен вручную')
    if state['day_paused']: pauses.append('достигнут дневной лимит потерь')
    if state['dd_paused']: pauses.append('достигнут общий лимит снижения счёта')
    lines = ['AI CRYPTO · ГЛАВНАЯ', f'Версия {BOT_VERSION}',
             'Источник: Binance Spot · сигналы на рост и падение', '',
             'Поиск: '+('; '.join(pauses) if pauses else 'включён'),
             'Последняя проверка входа: '+stamp(max((v['at'] for v in state.get('last_evaluation',{}).values()),default=0)),
             f'Открытых сделок: {len(positions)} · Ожидают входа: {len(state["pending"])}', '',
             'УЧЕБНЫЙ СЧЁТ',
             f'Сейчас: {equity:,.2f} USDT', f'На старте: {cfg.initial_equity:,.2f} USDT',
             f'Изменение счёта: {equity-cfg.initial_equity:+.2f} USDT ({(equity/cfg.initial_equity-1)*100:+.2f}%)',
             f'Завершённые сделки: {state["realized_net"]:+.2f} USDT',
             f'Открытые сделки, включая уже закрытые части: {open_result:+.2f} USDT', '',
             'Уровни и условный результат рассчитаны по споту. Для фьючерсного входа сверяй их с контрактом; платежи за удержание не учтены. Ордера не отправляются.',
             'Результат открытых сделок — оценка по последним обработанным минутным данным.']
    if state.get('legacy_spot_archive'):
        lines += ['', 'Спотовая история сохранена отдельно. Здесь — только фьючерсный счёт; старые спотовые сделки больше не отслеживаются.']
    return '\n'.join(lines)


def analytics(trades, period='ALL'):
    rows = select_trades(trades, period)
    m = trade_metrics(rows)
    lines = ['РЕЗУЛЬТАТЫ · '+PERIODS[period], 'Модель по спотовым ценам · суммы в USDT', '',
             f'Завершено сделок: {m["n"]}', f'Итог после расходов: {m["net"]:+.2f}',
             f'Прибыльных: {m["wins"]} · Убыточных: {m["losses"]} · Нулевых: {m["flat"]}']
    if rows:
        ratio = f'{m["pf"]:.2f}' if m['pf'] is not None else 'пока нельзя сравнить — убытков нет'
        lines += [f'Доля прибыльных: {m["winrate"]:.1f}%', f'Средний итог одной сделки: {m["avg"]:+.2f}',
                  f'Средняя прибыль: {m["avg_win"]:+.2f}', f'Средний убыток: {m["avg_loss"]:+.2f}',
                  f'Отношение суммы прибыли к сумме убытков: {ratio}',
                  'Число выше 1 означает, что сумма прибылей превышает сумму убытков.',
                  f'Самая длинная серия убытков: {m["streak"]}',
                  f'Убыточных сделок подряд в конце периода: {m["current_streak"]}']
    else:
        lines += ['Когда сделки полностью закроются, здесь появятся их результаты.']
    lines += ['', f'Комиссии: {m["fees"]:.2f}', f'Платежи за удержание фьючерсов: нет данных, не учтены',
              'Комиссии и поправка на исполнение уже включены. Это результат модели по споту, без фьючерсных платежей и разницы цен рынков.', '', 'ПО ВИДАМ СДЕЛОК']
    for tf, name in MODES.items():
        g=trade_metrics([t for t in rows if t['tf']==tf])
        lines += [f'{name}: {g["net"]:+.2f} · сделок {g["n"]}']
    lines += ['', 'ПО НАПРАВЛЕНИЯМ']
    for side, name in SIDES.items():
        g=trade_metrics([t for t in rows if t['side']==side])
        lines += [f'{name}: {g["net"]:+.2f} · сделок {g["n"]}']
    lines += ['', 'Период определяется по полному закрытию сделки. Частичные закрытия входят в её общий итог. Время — UTC.']
    return '\n'.join(lines)


def breakdown(trades, period='ALL'):
    rows = select_trades(trades, period)
    groups = defaultdict(list)
    for t in rows: groups[t['symbol']].append(t)
    lines=['ПОДРОБНОСТИ · '+PERIODS[period], 'Модель по спотовым ценам · суммы в USDT', '', 'ПО МОНЕТАМ']
    for symbol, group in sorted(groups.items(), key=lambda item: sum(t['net_pnl'] for t in item[1]), reverse=True):
        m=trade_metrics(group)
        lines.append(f'{symbol}: {m["net"]:+.2f} · сделок {m["n"]}')
    if not rows: lines.append('Завершённых сделок пока нет.')
    lines += ['', 'КАК ЗАКРЫВАЛИСЬ СДЕЛКИ']
    for reason, count in sorted(Counter(t['exit_reason'] for t in rows).items()):
        lines.append(f'{EXITS.get(reason, "Другая причина")}: {count}')
    lines += [f'Дошли до первой цели: {sum(bool(t["tp1_hit"]) for t in rows)} из {len(rows)}', '', 'ПО ДНЯМ ЗАКРЫТИЯ · UTC']
    days=defaultdict(float)
    for t in rows: days[datetime.fromtimestamp(t['exit_ts']/1000,timezone.utc).strftime('%d.%m.%Y')] += t['net_pnl']
    for day, net in sorted(days.items(), key=lambda p: datetime.strptime(p[0], '%d.%m.%Y'), reverse=True):
        lines.append(f'{day}: {net:+.2f}')
    return '\n'.join(lines)


def risk_report(s, cfg):
    equity=account_equity(s)
    dd=max(0,1-equity/max(s['peak_equity'],1e-12))*100
    used=sum(p['planned_risk'] for p in s['positions'].values())
    return '\n'.join(['ЗАЩИТА СЧЁТА', 'Все суммы относятся к учебному счёту.', '',
        f'Планируемый риск одной сделки: до {cfg.risk_fraction*100:.2f}% счёта',
        f'Общий лимит планируемого риска: {cfg.max_total_risk*100:.2f}%',
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
    lines=['ОТКРЫТЫЕ СДЕЛКИ', 'Спотовые цены · условные LONG/SHORT · без фьючерсных платежей.']
    for p in s['positions'].values():
        pos=Position(**p)
        exit_price=pos.mark*(1-direction(pos.side)*pos.slip_rate)
        net=pos.net_pnl+pos.remaining*(direction(pos.side)*(exit_price-pos.entry)-exit_price*pos.fee_rate)
        lines += ['', f'{p["symbol"]} · {SIDES[p["side"]]}', MODES[p['tf']],
                  f'Цена входа: {p["entry"]:.8g}', f'Последняя цена: {p["mark"]:.8g}',
                  f'Защитный стоп: {p["stop"]:.8g}',
                  f'Первая цель: {p["tp1"]:.8g}'+(' · достигнута' if p['tp1_hit'] else ''),
                  f'Вторая цель: {p["tp2"]:.8g}',
                  f'Осталось открыто: {100*p["remaining"]/p["qty"]:.0f}% исходного объёма',
                  f'Результат всей сделки при закрытии сейчас: {net:+.2f} USDT',
                  f'Данные: {stamp(p["last_bar_ts"]+MINUTE if p["last_bar_ts"]>=0 else p["entry_ts"])}']
    if not s['positions']: lines += ['', 'Открытых сделок нет.']
    if s['pending']:
        lines += ['', 'ОЖИДАЮТ ВХОДА']
        for p in s['pending'].values():
            lines.append(f'{p["symbol"]} · {SIDES[p["side"]]} · {stamp(p["entry_ts"])}')
    lines += ['', 'Стоп — уровень защитного выхода. Цели — уровни фиксации прибыли; результат до закрытия может измениться.']
    return '\n'.join(lines)


def history_report(trades):
    lines=['ИСТОРИЯ СДЕЛОК', 'Сначала последние закрытия · время UTC']
    if not trades: lines += ['', 'Завершённых сделок пока нет.']
    for t in sorted(trades,key=lambda t:(t['exit_ts'],t['id']),reverse=True)[:100]:
        lines += ['', f'{t["symbol"]} · {SIDES.get(t["side"],t["side"])}',
                  f'{MODES.get(t["tf"],t["tf"])} · {stamp(t["exit_ts"])}',
                  f'{EXITS.get(t["exit_reason"],"Другая причина")} · {t["net_pnl"]:+.2f} USDT после расходов']
    lines += ['', 'Показаны до 100 последних сделок. Полную таблицу можно скачать в разделе «Результаты».']
    return '\n'.join(lines)


def why_report(s):
    lines=['ПОЧЕМУ НЕТ ВХОДА', 'Бот проверяет рынок после завершения каждого ценового интервала.', '',
           'Проверки — это не готовые сделки. Для каждой показана первая причина отказа; последующие условия могли также не пройти.']
    now=int(time.time()*1000)
    if not s['enabled']: lines += ['', 'Сейчас поиск выключен вручную. Его можно включить в «Управлении».']
    if s['day_paused'] or s['dd_paused']: lines += ['', 'Сработала защита от потерь. Подробнее — «Защита счёта».']
    for tf,name in MODES.items():
        lines += ['',name.upper(), f'Всего проверок: {s["evaluated"].get(tf,0)}',
                  f'Назначено входов с начала этого счёта: {s.get("scheduled",{}).get(tf,0)}']
        if tf=='15m' and not s['scalp_enabled']: lines.append('Этот режим выключен вручную.')
        if s['tf_pause_until'].get(tf,0)>now: lines.append('Перерыв после убытков до '+stamp(s['tf_pause_until'][tf]))
        reasons=s['rejects'].get(tf,{})
        for reason,count in sorted(reasons.items(), key=lambda x:-x[1]):
            lines.append(f'• {REASONS.get(reason,"Другая проверка")}: {count}')
        if not reasons: lines.append('Отказов пока нет.')
    if s.get('data_issues'):
        lines += ['', 'ЕСТЬ ПРОБЛЕМЫ С ДАННЫМИ']
        for key,value in sorted(s['data_issues'].items()):
            lines.append(f'{key.replace("_"," · ")}: {value["message"]}')
    lines += ['', 'Счётчики накоплены с начала этого счёта. В списке также отмены уже назначенных входов. Вход назначается только после всех проверок; затем ещё может отмениться из-за движения цены или лимита риска.']
    return '\n'.join(lines)


GUIDE = '''КАК ПОЛЬЗОВАТЬСЯ
Бот анализирует спотовые свечи Binance и ищет ориентиры для сделок на рост и падение. Перед входом на фьючерсах сверяй цену, стоп и цели с выбранным контрактом. Для всех трёх режимов доступны оба направления.

НАПРАВЛЕНИЯ
На рост (LONG): результат улучшается при росте цены.
На падение (SHORT): результат улучшается при снижении цены.

КАК ЧИТАТЬ СИГНАЛ
Цена входа — ориентир для начала сделки.
Защитный стоп — уровень выхода при неблагоприятном движении.
Первая цель — закрытие части сделки с прибылью.
Вторая цель — закрытие оставшейся части.
Достижение целей заранее неизвестно.

ЧТО ЗНАЧАТ КНОПКИ
Открытые сделки — вход, стоп, цели и текущая оценка результата.
Результаты — итог завершённых сделок с учётом расходов за выбранный период.
Подробности — результаты по монетам, дням и причинам закрытия.
История — список отдельных завершённых сделок.
Почему нет входа — какие проверки остановили новые сделки.
Работает ли бот — время проверки, доступность данных и сообщения об ошибках.
Защита счёта — ограничения риска и условия защитных пауз.
Управление — включение поиска и быстрых сделок. Уже открытые сделки продолжают отслеживаться.

КАК БОТ ВЫБИРАЕТ ВХОД
Он проверяет направление и силу движения, отсутствие резких аномалий, затем сравнивает ситуацию с прошлыми примерами. Это не вероятность успеха и не гарантия прибыли.

ЧТО СЧИТАЕТ СТАТИСТИКА
Все деньги условные. Это расчёт LONG/SHORT по спотовым ценам, а не реальная спотовая продажа без покрытия. Комиссии и ухудшение цены исполнения заложены в модель. Фьючерсные цены и платежи за удержание не загружаются; результаты не равны доходности фьючерсного счёта.
Время показано в UTC. USDT — единица расчёта счёта.
Новые сигналы приходят отдельными уведомлениями. Разделы меню меняются в одном сообщении.'''


def ledger_csv(trades):
    stream=io.StringIO(newline='')
    fields=['id','symbol','tf','side','entry_ts','exit_ts','entry','qty','net_pnl','net_r','fees','funding_paid','tp1_hit','exit_reason','ambiguous_bars']
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
    # All content is escaped; no symbol/error text can inject Telegram markup.
    lines=text.splitlines()
    return '\n'.join('<b>'+html.escape(line)+'</b>' if i==0 or (line.isupper() and len(line)>3) else html.escape(line)
                     for i,line in enumerate(lines))


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
    if action.startswith(('AN_','DETAILS_')):
        prefix='DETAILS_' if action.startswith('DETAILS_') else 'AN_'
        period=action[len(prefix):]
        rows += [[button(('✓ ' if period==p else '')+name,prefix+p) for p,name in [('TODAY','Сегодня'),('7D','7 дней'),('30D','30 дней')]],
                 [button('Всё время',prefix+'ALL'),button('Подробности' if prefix=='AN_' else 'Итоги',('DETAILS_' if prefix=='AN_' else 'AN_')+period)],
                 [button('Скачать все сделки · CSV','EXPORT_CSV')]]
    elif action=='CONTROLS':
        rows += [[button('Остановить поиск' if state.get('enabled',True) else 'Включить поиск',
                         'SIGNALS_OFF' if state.get('enabled',True) else 'SIGNALS_ON')],
                 [button('Выключить быстрые сделки' if state.get('scalp_enabled',True) else 'Включить быстрые сделки',
                         'SCALP_OFF' if state.get('scalp_enabled',True) else 'SCALP_ON')],
                 [button('Защита счёта','RISK')]]
    elif action=='MENU':
        rows += [[button('Открытые сделки','SHOW_LIVE_TRADES'),button('Результаты','AN_ALL')],
                 [button('Почему нет входа','SHOW_REJECTS'),button('История','SHOW_HISTORY')],
                 [button('Работает ли бот','STATUS'),button('Управление','CONTROLS')],
                 [button('Как пользоваться','HELP'),button('Защита счёта','RISK')]]
    if action!='MENU': rows.append([button('‹ Главная','MENU'),button('Обновить',f'PAGE|{action}|{page}')])
    else: rows.append([button('Обновить','MENU')])
    return {'inline_keyboard':rows}


def keyboard():
    return panel_keyboard()


import numpy as np
import pandas as pd

LOG = logging.getLogger('paper_bot')
MINUTE = 60_000
TF_MS = {'15m': 15 * MINUTE, '1h': 60 * MINUTE, '4h': 240 * MINUTE}
SYMBOLS = ('BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT',
           'DOGEUSDT', 'ADAUSDT', 'AVAXUSDT', 'DOTUSDT', 'LINKUSDT')
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
    # Scenario assumptions, not a claim about the user's actual exchange tier.
    fee_bps: float = 10.0
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


def new_state(cfg: Config):
    return {'schema': 2, 'config': dataclasses.asdict(cfg), 'balance': cfg.initial_equity,
            'positions': {}, 'pending': {}, 'last_scanned': {}, 'rejects': {},
            'evaluated': {}, 'cooldowns': {}, 'streaks': {}, 'tf_pause_until': {},
            'day': None, 'day_start_equity': cfg.initial_equity, 'day_paused': False,
            'peak_equity': cfg.initial_equity, 'max_drawdown': 0.0,
            'dd_paused': False, 'enabled': True, 'scalp_enabled': True,
            'closed_count': 0, 'realized_net': 0.0, 'last_update_id': 0, 'panel_messages': {}, 'scheduled': {},
            'data_issues': {}, 'last_evaluation': {}, 'created_at': int(time.time()*1000)}


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
    if len(positions) >= cfg.max_positions:
        return 'position_limit'
    if sum(p['side'] == side for p in positions) >= cfg.max_same_direction:
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
    unit = make_position(symbol, tf, side, float(bar['open']), pending['atr'], ts, 1.0, cfg)
    equity = account_equity(state)
    used_risk = sum(p['planned_risk'] for p in state['positions'].values())
    used_notional = sum(p['remaining'] * p['mark'] for p in state['positions'].values())
    budget = min(equity * cfg.risk_fraction, equity * cfg.max_total_risk - used_risk)
    notional = min(equity * cfg.max_position_notional, equity * cfg.max_gross_notional - used_notional)
    if budget <= 0 or notional <= 0:
        return None, 'portfolio_budget'
    qty = min(budget / unit.planned_risk, notional / unit.entry)
    if qty * unit.entry < 10:  # paper-only dust floor, not an exchange filter
        return None, 'position_too_small'
    p = make_position(symbol, tf, side, float(bar['open']), pending['atr'], ts, qty, cfg)
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
        if saved.get('schema') != 2 or saved['config'] != dataclasses.asdict(cfg):
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
    """Separate research account; never mix spot proxy results with futures fills."""
    if cfg.market != 'spot_proxy':
        raise ValueError('This scanner requires the spot_proxy research profile')
    return Store(Path(data_dir) / 'paper_spot_signals.sqlite3', cfg)


class MarketClient:
    def __init__(self, market='spot_proxy'):
        self.market = market
        self.base = 'https://data-api.binance.vision' if market in ('spot', 'spot_proxy') else 'https://fapi.binance.com'
        self.prefix = '/api/v3' if market in ('spot', 'spot_proxy') else '/fapi/v1'
        self.frames = {}
        self.clock_offset = 0
        self.clock_checked = 0

    def get(self, path, **params):
        url = self.base + path + '?' + urllib.parse.urlencode(params)
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers={'User-Agent': 'PaperSignalBot/2'})
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
            fresh = self.klines_range(symbol, tf, int(old.iloc[-1].ts) + TF_MS[tf], now - 1)
            if not fresh.empty:
                self.frames[key] = pd.concat([old, fresh], ignore_index=True).drop_duplicates('ts').reset_index(drop=True)
        result = self.frames[key]
        validate_bars(result, TF_MS[tf])
        return result

    def spread_bps(self, symbol):
        quote = self.get(self.prefix + '/ticker/bookTicker', symbol=symbol)
        bid, ask = float(quote['bidPrice']), float(quote['askPrice'])
        if not all(math.isfinite(x) for x in (bid, ask)) or not 0 < bid <= ask:
            raise ValueError('Invalid spread quote')
        return 10000 * (ask - bid) / ((ask + bid) / 2)

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
        # Plain text avoids broken Markdown/escaping on symbols and exception text.
        lines, chunk = message.splitlines(keepends=True), ''
        for line in lines + ['']:
            if len(chunk) + len(line) > 3800 or line == '':
                if chunk:
                    data = {'chat_id': chat, 'text': chunk}
                    if menu and line == '':
                        data['reply_markup'] = keyboard()
                    self.call('sendMessage', data)
                chunk = ''
            chunk += line[:3800]

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
    return (f'УЧЕБНЫЙ ВХОД · ПО СПОТОВЫМ СВЕЧАМ\n{p.symbol} · {SIDES[p.side]}\n{MODES[p.tf]}\n\n'
            f'Цена входа: {p.entry:.8g}\nЗащитный стоп: {p.stop:.8g}\n'
            f'Первая цель: {p.tp1:.8g} · закрыть {100*p.partial_fraction:g}%\n'
            f'Вторая цель: {p.tp2:.8g} · закрыть остаток\n'
            f'Расчётный объём: {p.qty:.8g}\nПланируемый риск: {p.planned_risk:.2f} USDT\n\n'
            'Учебная сделка по спотовым ценам. На фьючерсах уровни могут отличаться. Вход был назначен заранее; результат исполнения рассчитан после закрытия минутного интервала.')


def closed_message(t):
    return (f'СДЕЛКА ЗАВЕРШЕНА\n{t["symbol"]} · {SIDES[t["side"]]}\n{MODES[t["tf"]]}\n\n'
            f'Причина: {EXITS.get(t["exit_reason"],"другое условие выхода")}\n'
            f'Результат после расходов: {t["net_pnl"]:+.2f} USDT\n\n'
            'Модель по спотовым ценам. Частичные закрытия и комиссии включены; платежи за удержание фьючерсов не учтены.')


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

    def scan_one(self, symbol, tf, now):
        state = self.store.snapshot()
        key = f'{symbol}_{tf}'
        latest_close = (now // TF_MS[tf])*TF_MS[tf]-1
        if state['last_scanned'].get(key,-1)>=latest_close:
            return
        if not state['enabled'] or (tf=='15m' and not state['scalp_enabled']):
            return
        if now-latest_close>2*MINUTE:
            return  # Wait for a fresh closed candle; never backdate a new signal.
        self.activity = f'Проверяю {symbol} · {MODES[tf]}'
        raw = self.client.history(symbol,tf,now,self.cfg.history_limit)
        if raw.empty or int(raw.iloc[-1].close_ts)!=latest_close:
            self.record_data_issue(symbol,tf,'Биржа ещё не вернула свежие завершённые данные')
            return
        df = build_feature_frame(raw)
        signal, reason = predict_knn(df,len(df)-1,tf,self.cfg)
        if signal and self.client.spread_bps(symbol)>self.cfg.max_spread_bps:
            signal, reason = None,'spread'
        decision_now = self.client.now_ms()
        def decide(s):
            if s['last_scanned'].get(key,-1)>=latest_close:
                return None,[]
            s['last_scanned'][key]=latest_close
            s['evaluated'][tf]=s['evaluated'].get(tf,0)+1
            s.setdefault('data_issues',{}).pop(key,None)
            why = reason
            if signal:
                why = portfolio_gate(s,symbol,tf,signal['side'],decision_now,self.cfg)
                if any(p['symbol']==symbol for p in s['pending'].values()):
                    why='symbol_pending'
                if decision_now-latest_close>2*MINUTE:
                    why='stale_signal'
                if not why:
                    entry_ts=((decision_now+1000)//MINUTE+1)*MINUTE
                    s['pending'][key]=dict(signal,symbol=symbol,tf=tf,entry_ts=entry_ts)
                    s.setdefault('scheduled',{})[tf]=s.get('scheduled',{}).get(tf,0)+1
                    s.setdefault('last_evaluation',{})[key]={'at':decision_now,'outcome':'scheduled'}
                    return s['pending'][key],[]
            reject(s,tf,why or 'no_signal')
            s.setdefault('last_evaluation',{})[key]={'at':decision_now,'outcome':why or 'no_signal'}
            return None,[]
        pending=self.store.transact(decide)
        if pending:
            preview=make_position(symbol,tf,pending['side'],pending['reference'],pending['atr'],pending['entry_ts'],1.,self.cfg)
            self.telegram.notify(
                f'НОВЫЙ СИГНАЛ · ПО СПОТОВЫМ СВЕЧАМ\n{symbol} · {SIDES[pending["side"]]}\n{MODES[tf]}\n\n'
                f'Ориентир цены: {pending["reference"]:.8g}\n'
                f'Предварительный стоп: {preview.stop:.8g}\n'
                f'Первая цель: {preview.tp1:.8g}\nВторая цель: {preview.tp2:.8g}\n'
                f'Проверка входа: {stamp(pending["entry_ts"])}\n\n'
                'Источник — спот. Перед фьючерсным входом сверь уровни с контрактом. Вход учебного счёта ещё может отмениться, если цена уйдёт слишком далеко или сработает лимит риска. '
                'Точные уровни будут в сообщении о входе. Прибыль не гарантирована.')

    def scan(self, now):
        for tf in TF_MS:
            for symbol in SYMBOLS:
                try:
                    self.scan_one(symbol,tf,self.client.now_ms())
                except Exception as error:
                    message = market_error_text(error)
                    self.record_data_issue(symbol,tf,message)
                    LOG.warning('Scan failed for %s %s: %s',symbol,tf,message)
                    # Exchange-wide restrictions must not trigger 30 immediate retries.
                    if isinstance(error,urllib.error.HTTPError) and (error.code in (403,418,429,451) or error.code >= 500):
                        raise
                    if isinstance(error,(urllib.error.URLError,TimeoutError)) and not isinstance(error,urllib.error.HTTPError):
                        raise
                    if isinstance(error,RuntimeError) and 'IP restriction' in str(error):
                        raise

    def warm_one_history(self, now):
        # Prepare one missing history during idle time, instead of doing all cold
        # downloads inside the two-minute signal window. Never delay active monitoring.
        if not hasattr(self.client, 'frames'):
            return
        state = self.store.snapshot()
        if state['positions'] or state['pending'] or not state['enabled']:
            return
        for tf in TF_MS:
            if tf == '15m' and not state['scalp_enabled']:
                continue
            # Leave a minute before the next close for monitoring and fresh scans.
            until_close = TF_MS[tf] - now % TF_MS[tf]
            if now % TF_MS[tf] < 2*MINUTE or until_close < MINUTE:
                continue
            for symbol in SYMBOLS:
                key = (symbol, tf)
                if key in self.client.frames or self.warm_retry_at.get(key, 0) > time.monotonic():
                    continue
                self.activity = f'Готовлю историю {symbol} · {MODES[tf]}'
                self.warm_retry_at[key] = time.monotonic() + 300
                try:
                    self.client.history(symbol, tf, now, self.cfg.history_limit)
                except Exception as error:
                    self.record_data_issue(symbol, tf, market_error_text(error))
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
            lines = ['РАБОТАЕТ ЛИ БОТ', f'Версия {BOT_VERSION}',
                     'Источник свечей: Binance Spot · расчёт сигналов LONG/SHORT', 'Направления: рост и падение во всех трёх режимах', '',
                     f'Последний завершённый цикл: {stamp(self.last_success*1000)}',
                     f'Последняя попытка проверки: {stamp(self.last_attempt*1000)}',
                     'Текущее действие: '+self.activity,
                     'Ошибка: '+(self.last_error or 'нет'), '',
                     'ПОСЛЕДНИЕ ПРОВЕРКИ ЦЕН']
            for tf, name in MODES.items():
                values = [v['at'] for k,v in s.get('last_evaluation',{}).items() if k.endswith('_'+tf)]
                lines.append(name+': '+stamp(max(values) if values else 0))
            lines += ['', f'Проблем с получением данных: {len(s.get("data_issues",{}))}',
                      'Причины показаны в разделе «Почему нет входа».',
                      'Новый поиск выполняется после закрытия очередного интервала: 15 минут, 1 час или 4 часа.',
                      'Сразу после запуска может потребоваться время на загрузку истории.']
            return '\n'.join(lines)
        if action in ('SHOW_STATS', 'MENU'):
            return report(self.store, self.cfg)
        if action == 'SHOW_LIVE_TRADES':
            return live_report(s)
        if action == 'SHOW_REJECTS':
            return why_report(s)
        if action == 'SHOW_HISTORY':
            return history_report(self.store.trades())
        if action == 'HELP':
            return GUIDE
        if action in ('SHOW_AI_MEMORY', 'RISK'):
            return risk_report(s, self.cfg)
        if action == 'CONTROLS':
            return ('УПРАВЛЕНИЕ ПОИСКОМ\n\n'
                    f'Поиск новых сделок: {"включён" if s["enabled"] else "выключен"}\n'
                    f'Быстрые сделки: {"включены" if s["scalp_enabled"] else "выключены"}\n\n'
                    'Выключение отменяет ожидающие входы. Уже открытые сделки продолжают отслеживаться.\n'
                    'Включение поиска не отменяет защитные паузы после потерь.')
        if action in ('SIGNALS_ON', 'SIGNALS_OFF', 'SCALP_ON', 'SCALP_OFF', 'TOGGLE_SIGNALS', 'TOGGLE_SCALP'):
            key = 'enabled' if action.startswith('SIGNALS_') or action == 'TOGGLE_SIGNALS' else 'scalp_enabled'
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
        allowed = {'MENU','STATUS','SHOW_LIVE_TRADES','SHOW_REJECTS','SHOW_HISTORY','HELP','RISK','CONTROLS'}
        allowed |= {'AN_'+p for p in PERIODS} | {'DETAILS_'+p for p in PERIODS}
        controls = {'SIGNALS_ON','SIGNALS_OFF','SCALP_ON','SCALP_OFF','TOGGLE_SIGNALS','TOGGLE_SCALP'}
        if action in controls:
            text = self.handle_action(action)
            action = 'CONTROLS'
        else:
            action = action if action in allowed else 'MENU'
            text = self.handle_action(action)
        pages = text_pages(text)
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
        page = 0
        if action.startswith('PAGE|'):
            try:
                _, action, raw_page = action.split('|')
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
        threading.Thread(target=runtime.work, daemon=True).start()
        threading.Thread(target=runtime.telegram_work, daemon=True).start()
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Health(BaseHTTPRequestHandler):
            def do_GET(self):
                fresh = time.time() - runtime.last_success < 180
                starting = not runtime.last_attempt or (not runtime.last_success and time.time()-started_at < 300 and not runtime.last_error)
                body = json.dumps({'mode': 'paper', 'market': cfg.market, 'version': BOT_VERSION, 'healthy': fresh,
                                   'last_success': runtime.last_success,
                                   'error_type': runtime.last_error}).encode()
                self.send_response(200 if fresh or starting else 503)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                return

        server = ThreadingHTTPServer(('0.0.0.0', int(os.getenv('PORT', '10000'))), Health)
        LOG.info('PAPER ONLY. Market=%s. No exchange orders. Ctrl+C to stop.', cfg.market)
        server.serve_forever()
    finally:
        if runtime:
            runtime.running.clear()
        lock_handle.close()


if __name__ == '__main__':
    main()
