"""Download public market data only. No trading endpoint or account API."""
import argparse
import json
from pathlib import Path

import pandas as pd

from bot import MarketClient, MINUTE, validate_bars


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--symbol', required=True)
    p.add_argument('--market', choices=('spot', 'usdm'), default='spot')
    p.add_argument('--start', required=True, help='UTC date, inclusive')
    p.add_argument('--end', required=True, help='UTC date, exclusive')
    p.add_argument('--output', required=True, help='CSV output path')
    args = p.parse_args()
    start = int(pd.Timestamp(args.start, tz='UTC').timestamp() * 1000)
    end = int(pd.Timestamp(args.end, tz='UTC').timestamp() * 1000) - 1
    if start > end or start % MINUTE or (end + 1) % MINUTE:
        raise ValueError('Invalid minute-aligned range')
    output = Path(args.output)
    paths = [output, Path(str(output) + '.meta.json'), Path(str(output) + '.funding.json')]
    if any(x.exists() for x in paths):
        raise FileExistsError('Choose new output filenames; existing experiments are not overwritten')
    client = MarketClient(args.market)
    now = client.now_ms()
    if end >= now:
        raise ValueError('Requested end includes unclosed/future candles')
    print('Downloading public 1m candles. Large date ranges can take several minutes.', flush=True)
    bars = client.klines_range(args.symbol, '1m', start, end)
    validate_bars(bars, MINUTE)
    if bars.empty or int(bars.iloc[0].ts) != start or int(bars.iloc[-1].close_ts) != end:
        raise ValueError('Exchange returned incomplete period; select available continuous dates')
    funding = client.funding(args.symbol, start, end)
    output.parent.mkdir(parents=True, exist_ok=True)
    bars[['ts', 'open', 'high', 'low', 'close', 'volume', 'close_ts']].to_csv(output, index=False)
    metadata = {'market': args.market, 'symbol': args.symbol, 'interval': '1m',
                'start_ms': start, 'end_ms': end, 'source': client.base, 'rows': len(bars)}
    paths[1].write_text(json.dumps(metadata, indent=2))
    if args.market == 'usdm':
        paths[2].write_text(json.dumps(dict(metadata, events=funding), indent=2))
    print(f'Saved {len(bars)} closed candles: {output}')


if __name__ == '__main__':
    main()
