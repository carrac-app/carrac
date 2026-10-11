"""
Weekly cycle position for BTC, measured against the 200-week moving average.

Describes where price sits in its long-term cycle; it does not say what to do
about it. The primary reading is the Mayer multiple (price / 200-week MA):

- DISCOUNT: price at, below or near the 200-week MA (multiple < 1.2), with an
  on-chain reading agreeing when it is near rather than below
- NEUTRAL: price above the 200-week MA but short of the extended zone
  (multiple 1.2-2.0), or near the MA with nothing on-chain agreeing
- EXTENDED: price far above the 200-week MA (multiple > 2.0)

On-chain readings that can agree or disagree:
- MVRV Z-score: undervalued (Z < 0) or overvalued (Z > 3)
- Puell multiple: miner capitulation (< 0.5) or euphoria (> 4.0)
"""

from typing import Optional, Dict, Any


class WeeklySignalEngine:
    """
    Classify BTC's weekly cycle position, with the 200-week MA as the
    primary reading.

    Zones (see the module docstring): DISCOUNT, NEUTRAL, EXTENDED.
    """

    def __init__(self, db, consensio_calculator=None):
        self.db = db
        self.consensio = consensio_calculator

    def calculate(self, symbol: str = 'BTC',
                  mvrv_data: Optional[Dict] = None,
                  mayer_data: Optional[Dict] = None,
                  puell_data: Optional[Dict] = None,
                  weekly_consensio: Optional[int] = None) -> Dict[str, Any]:
        """
        Classify the weekly cycle position, 200-week MA first.
        """
        if symbol != 'BTC':
            return {
                'signal': 'NO_DATA',
                'confidence': 'none',
                'reason': 'Weekly signal only available for BTC (on-chain data)'
            }

        # Fetch data if not provided
        if mvrv_data is None:
            mvrv_data = self.db.fetch_mvrv_zscore()
        if mayer_data is None:
            mayer_data = self.db.fetch_mayer_multiple()
        if puell_data is None:
            puell_data = self.db.fetch_puell_multiple()

        # Extract values
        mayer = mayer_data.get('mayer_multiple')
        mvrv_z = mvrv_data.get('zscore')
        puell = puell_data.get('puell_multiple')

        # Handle missing data
        if mayer is None:
            return {
                'signal': 'NO_DATA',
                'confidence': 'none',
                'reason': 'Unable to calculate 200WMA position'
            }

        # Calculate zone and confirmations
        components = self._build_components(mayer_data, mvrv_data, puell_data)

        # Determine signal based on 200WMA position
        return self._determine_signal(mayer, mvrv_z, puell, components)

    def _build_components(self, mayer_data: Dict, mvrv_data: Dict,
                          puell_data: Dict) -> Dict:
        """Build component details for response."""
        mayer = mayer_data.get('mayer_multiple')
        mvrv_z = mvrv_data.get('zscore')
        puell = puell_data.get('puell_multiple')

        # Score each component
        mayer_score = self._score_mayer(mayer)
        mvrv_score = self._score_mvrv(mvrv_z)
        puell_score = self._score_puell(puell)

        return {
            'mayer': {
                'multiple': mayer,
                'ma_200w': mayer_data.get('ma_200w'),
                'zone': self._mayer_zone(mayer),
                'score': mayer_score,
                'detail': self._mayer_detail(mayer)
            },
            'mvrv': {
                'zscore': mvrv_z,
                'zone': mvrv_data.get('zone'),
                'score': mvrv_score,
                'detail': self._mvrv_detail(mvrv_z)
            },
            'puell': {
                'multiple': puell,
                'zone': puell_data.get('zone'),
                'score': puell_score,
                'detail': self._puell_detail(puell)
            }
        }

    def _score_mayer(self, mm: Optional[float]) -> int:
        """Score the Mayer multiple — the primary reading. Positive means
        price is low relative to its 200-week MA, negative means high."""
        if mm is None:
            return 0
        if mm < 0.8:
            return 3   # Deep discount: more than 20% below the 200-week MA
        elif mm < 1.0:
            return 2   # Below the 200-week MA
        elif mm < 1.2:
            return 1   # Within 20% above the 200-week MA
        elif mm < 2.0:
            return 0   # Above the 200-week MA, not extended
        elif mm < 2.4:
            return -2  # Extended: more than double the 200-week MA
        else:
            return -3  # Historically rare extreme

    def _score_mvrv(self, z: Optional[float]) -> int:
        """Score MVRV Z-Score - CONFIRMATION."""
        if z is None:
            return 0
        if z < 0:
            return 1   # Undervalued
        elif z < 2:
            return 0   # Fair value
        elif z < 4:
            return -1  # Overvalued
        else:
            return -2  # Extreme

    def _score_puell(self, pm: Optional[float]) -> int:
        """Score Puell Multiple - CONFIRMATION."""
        if pm is None:
            return 0
        if pm < 0.5:
            return 1   # Miner capitulation
        elif pm < 0.8:
            return 0   # Low but not capitulation
        elif pm < 3.0:
            return 0   # Normal
        elif pm < 4.0:
            return -1  # High
        else:
            return -2  # Euphoria

    def _mayer_zone(self, mm: Optional[float]) -> str:
        if mm is None:
            return 'unknown'
        if mm < 0.8:
            return 'deep_discount'
        elif mm < 1.0:
            return 'below_200wma'
        elif mm < 1.2:
            return 'near_200wma'
        elif mm < 2.0:
            return 'above_200wma'
        elif mm < 2.4:
            return 'extended'
        else:
            return 'bubble'

    def _mayer_detail(self, mm: Optional[float]) -> str:
        if mm is None:
            return 'No data'
        zone = self._mayer_zone(mm)
        zone_labels = {
            'deep_discount': 'deep discount',
            'below_200wma': 'below 200WMA',
            'near_200wma': 'near 200WMA',
            'above_200wma': 'above 200WMA',
            'extended': 'extended',
            'bubble': 'BUBBLE'
        }
        return f'MM={mm:.2f} ({zone_labels.get(zone, zone)})'

    def _mvrv_detail(self, z: Optional[float]) -> str:
        if z is None:
            return 'No data'
        if z < 0:
            return f'Z={z:.1f} (undervalued)'
        elif z < 2:
            return f'Z={z:.1f} (fair)'
        elif z < 4:
            return f'Z={z:.1f} (overvalued)'
        else:
            return f'Z={z:.1f} (EXTREME)'

    def _puell_detail(self, pm: Optional[float]) -> str:
        if pm is None:
            return 'No data'
        if pm < 0.5:
            return f'PM={pm:.2f} (capitulation)'
        elif pm < 3.0:
            return f'PM={pm:.2f} (normal)'
        elif pm < 4.0:
            return f'PM={pm:.2f} (high)'
        else:
            return f'PM={pm:.2f} (EUPHORIA)'

    def _determine_signal(self, mayer: float, mvrv_z: Optional[float],
                          puell: Optional[float], components: Dict) -> Dict[str, Any]:
        """
        Classify the cycle position, with the 200-week MA as the primary
        reading.

        1. At, below or near the MA (MM < 1.2): DISCOUNT, or NEUTRAL when
           price is only near the MA and nothing on-chain agrees
        2. Above the MA (MM 1.2-2.0): NEUTRAL
        3. Far above the MA (MM > 2.0): EXTENDED
        """
        mayer_score = self._score_mayer(mayer)
        mvrv_score = self._score_mvrv(mvrv_z)
        puell_score = self._score_puell(puell)
        total_score = mayer_score + mvrv_score + puell_score

        # Build reason: where price sits, then any reading that leans
        pct = (mayer - 1.0) * 100
        side = 'above' if pct >= 0 else 'below'
        position = f'Price is {abs(pct):.0f}% {side} the 200-week MA (multiple {mayer:.2f})'
        readings = []
        if mvrv_score != 0:
            readings.append(f"MVRV {components['mvrv']['detail']}")
        if puell_score != 0:
            readings.append(f"Puell {components['puell']['detail']}")
        reason_str = '; '.join([position] + readings)

        if mayer < 1.2:
            if mayer < 0.8:
                signal = 'DISCOUNT'
                confidence = 'high'
            elif mayer < 1.0:
                signal = 'DISCOUNT'
                confidence = 'high' if (mvrv_score > 0 or puell_score > 0) else 'medium'
            elif mvrv_score > 0 or puell_score > 0:
                signal = 'DISCOUNT'
                confidence = 'medium'
            else:
                # Near the MA, but no on-chain reading agrees
                signal = 'NEUTRAL'
                confidence = 'medium'
                reason_str = f'{reason_str}; no on-chain reading shows undervaluation'
        elif mayer < 2.0:
            signal = 'NEUTRAL'
            if mvrv_score < 0 or puell_score < 0:
                confidence = 'medium'
                reason_str = f'{reason_str}; on-chain readings lean overvalued'
            else:
                confidence = 'low'
        else:
            signal = 'EXTENDED'
            if mayer > 2.4 or (mvrv_score < -1 and puell_score < -1):
                confidence = 'high'
            else:
                confidence = 'medium'

        return {
            'signal': signal,
            'confidence': confidence,
            'reason': reason_str,
            'total_score': total_score,
            'scores': {
                'mayer': mayer_score,
                'mvrv': mvrv_score,
                'puell': puell_score
            },
            'components': components
        }

