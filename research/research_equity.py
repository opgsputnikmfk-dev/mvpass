"""Lossless minute MTM recorder for future research runners, not live bot code."""
from datetime import datetime, timezone
import math

MINUTE = 60000
DAY = 86400000


class EquityRecorder:
    """Feed every minute in [start, end); include liquidation costs in equity.

    Samples are minute-OPEN marks after events due at that timestamp. A sample
    at a UTC midnight is that day's opening equity. Missing points are errors,
    never interpolated. finish() accepts terminal liquidation equity separately.
    """
    def __init__(self, start, end, initial=10000.):
        if start % MINUTE or end % MINUTE or end <= start:
            raise ValueError("Invalid interval")
        self.start, self.end, self.expected = start, end, start
        self.initial = self._number(initial)
        if self.initial <= 0:
            raise ValueError("Initial equity must be positive")
        self.peak = self.initial
        self.max_drawdown = 0.
        self.daily_open = []
        self.finished = False

    @staticmethod
    def _number(value):
        value = float(value)
        if not math.isfinite(value):
            raise ValueError("Non-finite equity")
        return value

    def _observe(self, value):
        self.peak = max(self.peak, value)
        self.max_drawdown = max(self.max_drawdown, 1-value/self.peak)

    def append(self, ts, liquidation_equity):
        if self.finished or ts != self.expected or ts >= self.end:
            raise ValueError("Missing, duplicated, unordered or out-of-window minute")
        value = self._number(liquidation_equity)
        self._observe(value)
        if ts % DAY == 0:
            self.daily_open.append([ts, value])
        self.expected += MINUTE

    def finish(self, terminal_equity):
        if self.finished or self.expected != self.end:
            raise ValueError("Incomplete or already finished recording")
        value = self._number(terminal_equity)
        self._observe(value)
        self.finished = True
        return dict(start_ms=self.start, end_exclusive_ms=self.end,
                    initial_equity=self.initial, terminal_equity=value,
                    daily_open_equity=self.daily_open.copy(),
                    max_drawdown_pct=self.max_drawdown*100,
                    sample_semantics="minute open after due events; liquidation costs included",
                    terminal=dict(ts=self.end, equity=value))
