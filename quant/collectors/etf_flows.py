"""
Bitcoin ETF net flow collector for Quant DB.

Scrapes daily BTC ETF net flow data from farside.co.uk.
Stores in quant.onchain (ETF flows = supply/demand signal).

Usage:
    python -m quant.collectors.etf_flows
    python -m quant.collectors.etf_flows --backfill
"""

import logging
import re
from datetime import datetime, timedelta, timezone

import requests

from quant.collectors.base import BaseCollector

logger = logging.getLogger('quant.collectors.etf_flows')

FARSIDE_URL = 'https://farside.co.uk/bitcoin-etf-flow-all-data/'


class ETFFlowsCollector(BaseCollector):
    name = 'etf_flows'
    expected_items = ['BTC']

    def collect(self):
        logger.info("[etf_flows] Fetching BTC ETF flow data from farside.co.uk")

        resp = requests.get(FARSIDE_URL, timeout=30, headers={
            'User-Agent': 'Mozilla/5.0 (compatible; QuaintDB/1.0)',
        })
        resp.raise_for_status()
        html = resp.text

        rows = self._parse_flows(html)
        if not rows:
            logger.warning("[etf_flows] No rows parsed from HTML")
            return

        # Filter to recent data unless backfilling
        if not self.backfill:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime('%Y-%m-%d')
            rows = [r for r in rows if r['ts'] >= cutoff]

        if rows:
            count = self.upsert_rows(
                'quant.onchain', rows,
                conflict_cols=['ts', 'asset', 'metric'],
                update_cols=['value', 'source'],
            )
            logger.info(f"  ETF flows: {len(rows)} days, {count} upserted")

    def _parse_flows(self, html):
        """Parse the farside.co.uk all-data table for total net flows.

        The table has columns: Date, IBIT, FBTC, BITB, ARKB, ...last col is Total.
        We extract Date and Total (last numeric column per row).
        """
        rows = []

        # Find table rows — farside uses simple HTML tables
        # Pattern: <tr><td>DD Mon YYYY</td><td>...</td>...<td>TOTAL</td></tr>
        tr_pattern = re.compile(r'<tr[^>]*>(.*?)</tr>', re.DOTALL | re.IGNORECASE)
        td_pattern = re.compile(r'<td[^>]*>(.*?)</td>', re.DOTALL | re.IGNORECASE)

        for tr_match in tr_pattern.finditer(html):
            tr_content = tr_match.group(1)
            cells = td_pattern.findall(tr_content)
            if len(cells) < 3:
                continue

            # First cell should be a date
            date_str = re.sub(r'<[^>]+>', '', cells[0]).strip()
            dt = self._parse_date(date_str)
            if not dt:
                continue

            # Last cell is Total — strip HTML tags, parens (negative), commas
            total_str = re.sub(r'<[^>]+>', '', cells[-1]).strip()
            total_val = self._parse_number(total_str)
            if total_val is None:
                continue

            rows.append({
                'ts': dt.strftime('%Y-%m-%d'),
                'asset': 'BTC',
                'metric': 'ETF_BTC_NET_FLOW',
                'value': round(total_val, 2),
                'source': 'farside',
            })

        logger.info(f"  Parsed {len(rows)} daily flow entries")
        return rows

    @staticmethod
    def _parse_date(s):
        """Try to parse various date formats from farside."""
        for fmt in ('%d %b %Y', '%d %B %Y', '%Y-%m-%d', '%d/%m/%Y'):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        return None

    @staticmethod
    def _parse_number(s):
        """Parse a number that may have commas, parens for negative, or be empty."""
        if not s or s.strip() in ('-', '', 'N/A'):
            return None
        s = s.strip()
        negative = False
        if s.startswith('(') and s.endswith(')'):
            s = s[1:-1]
            negative = True
        s = s.replace(',', '').replace('$', '').strip()
        try:
            val = float(s)
            return -val if negative else val
        except ValueError:
            return None


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(name)s %(levelname)s %(message)s',
    )
    args = BaseCollector.parse_args('Bitcoin ETF net flow collector')
    collector = ETFFlowsCollector(
        dry_run=args.dry_run,
        backfill=args.backfill,
        backfill_start=args.backfill_start,
    )
    collector.run()
