"""bluestar/oanda_strength.py — Consolidated OANDA currency-strength engine.

CONSOLIDATION NOTE (audit fix): this file replaces two previously-diverging
implementations (``oanda_strength.py`` — a large multi-timeframe "Strength
Engine v10.0" extracted from a separate Streamlit dashboard — and
``oanda_strength_bridge.py`` — a thin D1-only adapter). They computed
"the same" currency-strength number two different ways and had already
drifted out of sync: the bridge's pair list was missing NZD_CAD (27 pairs
vs 28). This is the exact class of silent-divergence risk flagged in the
original BLUESTAR audit for ``market_data.py`` vs ``oanda_data.py`` (two
ATR implementations quietly disagreeing). One file, one pair list, one
aggregation method, closes it for good.

What was kept, and why:
- Pair list: the union of both (28 pairs, NZD_CAD restored).
- Currency list: MAJORS, single constant, used everywhere.
- Fetch layer: ``requests`` only (the bridge's choice) — no oandapyV20
  dependency — with the 429/timeout retry resilience from the old MTF
  engine's ``OandaClient`` folded in, so a single rate-limit no longer
  drops a pair outright.
- Aggregation: the D1 opponent-weighted algorithm (the bridge's "proven,
  verified against Oanda practice" algorithm — this is what
  ``macro_engine._oanda_strength_scores()`` actually consumes), with the
  z-score rescale fix already shipped: scores are bounded to [0,10] and
  only approach the extremes on a genuinely wide (~2-sigma) dispersion day,
  instead of a hard min-max that manufactured an exact 0 and an exact 10
  every single day regardless of how tight the real spread was.

What was deliberately NOT ported from the old MTF engine, and why:
- Multi-timeframe weighting (W/D/H4/H1), velocity, best-pair
  auto-selection, ATR/exposure filtering, the oandapyV20 dependency, the
  Streamlit-oriented StrengthResult/StrengthEngine classes. None of this
  was wired into the BLUESTAR briefing pipeline — only the D1 bridge's
  ``compute_scores()`` contract is (per its own docstring). Porting the
  full MTF engine is a separate, larger scope decision than "de-duplicate
  the adapter macro_engine.py actually calls" and would need its own
  sign-off if wanted later.

Public contract (unchanged — macro_engine.py needs no changes):
    compute_scores(access_token) -> Optional[Dict[str, float]]
        8 majors, 0.0-10.0, neutral 5.0, or None if fewer than MIN_PAIRS
        pairs could be fetched (documented degradation — caller falls back
        to CB-bias [PROXY]).
"""
from __future__ import annotations

import logging
import random
import time
from typing import Dict, List, Optional

import requests

from .config import STRENGTH_CANDLE_COUNT, STRENGTH_WINDOW_SESSIONS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_OANDA_BASE = "https://api-fxpractice.oanda.com/v3"
_TIMEOUT = 10.0            # seconds, per pair
_GRANULARITY = "D"        # D1 candles
# P1-2 : il faut WINDOW+1 clôtures pour mesurer la fenêtre, plus de la marge.
_CANDLE_COUNT = STRENGTH_CANDLE_COUNT
_WINDOW_SESSIONS = STRENGTH_WINDOW_SESSIONS
_MIN_PAIRS = 10           # below this, signal is meaningless -> None
_MAX_RETRIES = 3          # per pair, only on 429 / timeout / connection error

MAJORS: List[str] = ["USD", "EUR", "GBP", "JPY", "AUD", "CAD", "NZD", "CHF"]
CURRENCIES = MAJORS  # alias — both names were used across the two old files

# Opponent weighting (proven algorithm): a move against a reserve/liquidity
# currency is more informative for relative-strength ranking.
_HEAVY_OPPONENTS = frozenset({"USD", "EUR", "JPY"})
_HEAVY_WEIGHT = 2.0
_LIGHT_WEIGHT = 1.0

# 28 major FX pairs — union of the two former lists (consolidation fix:
# the old bridge was silently missing NZD_CAD).
PAIRS: List[str] = [
    "EUR_USD", "GBP_USD", "USD_JPY", "USD_CHF", "AUD_USD", "USD_CAD", "NZD_USD",
    "EUR_GBP", "EUR_JPY", "EUR_CHF", "EUR_AUD", "EUR_CAD", "EUR_NZD",
    "GBP_JPY", "GBP_CHF", "GBP_AUD", "GBP_CAD", "GBP_NZD",
    "AUD_JPY", "AUD_CAD", "AUD_NZD", "AUD_CHF",
    "CAD_JPY", "CAD_CHF", "NZD_CAD", "NZD_JPY", "NZD_CHF", "CHF_JPY",
]


# ---------------------------------------------------------------------------
# Single-pair fetch — % change of last two complete D1 closes, with retry.
# ---------------------------------------------------------------------------
def _fetch_pair_pct(
    instrument: str,
    access_token: str,
    *,
    session: Optional[requests.Session] = None,
    base_url: str = _OANDA_BASE,
) -> Optional[tuple[float, float, int]]:
    """Return ``(window_pct, last_session_pct, sessions_used)`` or ``None``.

    P1-2 (audit 09/11/2026, décision utilisateur) — la variation est désormais
    mesurée sur ``STRENGTH_WINDOW_SESSIONS`` séances (5 = une semaine de
    cotation) et non plus sur les deux dernières bougies (une seule séance).
    La variation de la DERNIÈRE séance est renvoyée séparément pour rester
    affichable : elle explique un mouvement court sans définir le classement.

    ``sessions_used`` dit combien de séances ont réellement servi : une fenêtre
    incomplète (< 5 séances d'historique) est signalée à l'appelant au lieu
    d'être présentée comme une fenêtre pleine.
    Uses complete candles only. Retries on 429 (rate limit) and on
    timeout/connection errors, up to ``_MAX_RETRIES`` — folded in from the
    old MTF engine's ``OandaClient`` so a single rate-limited pair is no
    longer silently dropped. Every non-success outcome logs a WARNING.
    """
    url = f"{base_url}/instruments/{instrument}/candles"
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {"granularity": _GRANULARITY, "count": _CANDLE_COUNT, "price": "M"}
    getter = session.get if session is not None else requests.get

    r = None
    for attempt in range(_MAX_RETRIES):
        try:
            r = getter(url, headers=headers, params=params, timeout=_TIMEOUT)
        except requests.Timeout as exc:
            if attempt < _MAX_RETRIES - 1:
                logger.warning("strength: %s timeout, retry %d/%d",
                               instrument, attempt + 1, _MAX_RETRIES)
                time.sleep(1.0)
                continue
            logger.warning("strength: %s timeout after %d attempts: %s",
                           instrument, _MAX_RETRIES, exc)
            return None
        except requests.RequestException as exc:
            logger.warning("strength: %s network error: %s", instrument, exc)
            return None

        if r.status_code == 429:
            if attempt < _MAX_RETRIES - 1:
                sleep_s = (2 ** attempt) + random.uniform(0, 0.5)  # nosec B311 — jitter non-cryptographique
                logger.warning("strength: %s HTTP 429, retry %d/%d in %.2fs",
                               instrument, attempt + 1, _MAX_RETRIES, sleep_s)
                time.sleep(sleep_s)
                continue
            logger.warning("strength: %s HTTP 429 after %d attempts", instrument, _MAX_RETRIES)
            return None
        break

    if r is None or r.status_code != 200:
        logger.warning("strength: %s HTTP %s", instrument, getattr(r, "status_code", "?"))
        return None

    try:
        candles = [c for c in r.json().get("candles", []) if c.get("complete")]
    except ValueError as exc:  # malformed JSON
        logger.warning("strength: %s bad JSON: %s", instrument, exc)
        return None

    if len(candles) < 2:
        logger.warning("strength: %s only %d complete candle(s)", instrument, len(candles))
        return None

    try:
        closes = [float(c["mid"]["c"]) for c in candles]
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("strength: %s parse error: %s", instrument, exc)
        return None

    if len(closes) < 2 or closes[-1] == 0:
        logger.warning("strength: %s closes inexploitables", instrument)
        return None

    last_session_pct = ((closes[-1] - closes[-2]) / closes[-2] * 100.0
                        if closes[-2] else None)
    if last_session_pct is None:
        return None

    sessions_used = min(_WINDOW_SESSIONS, len(closes) - 1)
    base = closes[-1 - sessions_used]
    if not base:
        logger.warning("strength: %s close de base nul", instrument)
        return None
    window_pct = (closes[-1] - base) / base * 100.0
    return window_pct, last_session_pct, sessions_used


# ---------------------------------------------------------------------------
# Aggregation — weighted mean performance per currency, then bounded rescale.
# ---------------------------------------------------------------------------
def _aggregate(pct_by_pair: Dict[str, float]) -> Dict[str, float]:
    """Turn per-pair % changes into a 0–10 strength score per major.

    For each pair BASE_QUOTE with %chg p:
      BASE gains  +p  weighted by the *opponent* (QUOTE) weight.
      QUOTE gains -p  weighted by the *opponent* (BASE)  weight.
    Each currency's raw score = weighted mean of its contributions.

    Scores are rescaled from a z-score (currency's raw value vs the
    cross-sectional mean/stdev of the majors that day), centered on 5.0
    (neutral) and clipped to [0,10]. A currency only approaches 0 or 10
    when it is genuinely ~2 standard deviations from the day's average —
    a real outlier — rather than every single day by construction (the
    old min-max forced an exact 0.00 and 10.00 daily regardless of how
    tight the real dispersion was).
    """
    totals: Dict[str, float] = {c: 0.0 for c in MAJORS}
    weights: Dict[str, float] = {c: 0.0 for c in MAJORS}

    for pair, pct in pct_by_pair.items():
        base, quote = pair.split("_")
        if base not in totals or quote not in totals:
            continue
        w_base = _HEAVY_WEIGHT if base in _HEAVY_OPPONENTS else _LIGHT_WEIGHT
        w_quote = _HEAVY_WEIGHT if quote in _HEAVY_OPPONENTS else _LIGHT_WEIGHT
        totals[base] += pct * w_quote
        weights[base] += w_quote
        totals[quote] += (-pct) * w_base
        weights[quote] += w_base

    raw: Dict[str, Optional[float]] = {}
    for c in MAJORS:
        raw[c] = (totals[c] / weights[c]) if weights[c] > 0 else None

    present = [v for v in raw.values() if v is not None]
    if not present:
        return {c: 5.0 for c in MAJORS}

    mean = sum(present) / len(present)
    variance = sum((v - mean) ** 2 for v in present) / len(present)
    stdev = variance ** 0.5
    _Z_SPAN = 2.0  # +/-2 stdev reaches the 0/10 bounds

    scores: Dict[str, float] = {}
    for c in MAJORS:
        v = raw[c]
        if v is None:
            scores[c] = 5.0
        elif stdev <= 1e-9:
            scores[c] = 5.0
        else:
            z = (v - mean) / stdev
            scaled = 5.0 + (z / _Z_SPAN) * 5.0
            scores[c] = round(min(10.0, max(0.0, scaled)), 2)
    return scores


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------
def compute_scores_detailed(
    access_token: str,
    *,
    session: Optional[requests.Session] = None,
    base_url: str = _OANDA_BASE,
) -> Optional[Dict[str, object]]:
    """Scores complets : fenêtre 5 séances + dernière séance (P1-2).

    Renvoie::

        {"scores": {ccy: 0-10},            # fenêtre STRENGTH_WINDOW_SESSIONS
         "last_session": {ccy: 0-10},      # dernière séance seulement
         "window_sessions": 5,
         "sessions_used": 5,               # min observé sur l'échantillon
         "pairs": 28,
         "last_session_observed_at": None}

    ou ``None`` si moins de ``_MIN_PAIRS`` paires ont répondu (dégradation
    documentée, identique à ``compute_scores``). Un seul aller-retour par paire
    alimente les deux agrégations.
    """
    if not access_token:
        logger.warning("strength: no access token — skipping Oanda strength")
        return None

    owns_session = session is None
    sess = session or requests.Session()
    window_by_pair: Dict[str, float] = {}
    last_by_pair: Dict[str, float] = {}
    sessions_used = _WINDOW_SESSIONS
    try:
        for pair in PAIRS:
            res = _fetch_pair_pct(pair, access_token, session=sess,
                                  base_url=base_url)
            if res is None:
                continue
            w, last, used = res
            window_by_pair[pair] = w
            last_by_pair[pair] = last
            sessions_used = min(sessions_used, used)
    finally:
        if owns_session:
            sess.close()

    n = len(window_by_pair)
    if n < _MIN_PAIRS:
        logger.warning(
            "strength: only %d/%d pairs fetched (< %d) — falling back to CB-bias",
            n, len(PAIRS), _MIN_PAIRS,
        )
        return None

    return {
        "scores": _aggregate(window_by_pair),
        "last_session": _aggregate(last_by_pair),
        "window_sessions": _WINDOW_SESSIONS,
        "sessions_used": sessions_used,
        "pairs": n,
    }


def compute_scores(
    access_token: str,
    *,
    session: Optional[requests.Session] = None,
    base_url: str = _OANDA_BASE,
) -> Optional[Dict[str, float]]:
    """Compute Oanda D1 relative-strength scores for the 8 majors.

    Returns a dict {currency: float in [0,10]} on success, or None when
    fewer than ``_MIN_PAIRS`` pairs could be fetched (documented
    degradation — the caller falls back to CB-bias [PROXY]). Never raises
    for per-pair errors; those are logged and skipped.
    """
    detailed = compute_scores_detailed(
        access_token, session=session, base_url=base_url)
    if detailed is None:
        return None
    scores = detailed["scores"]
    n = int(detailed["pairs"])
    if n < len(PAIRS):
        logger.info("strength: partial success %d/%d pairs — scores computed", n, len(PAIRS))
    else:
        logger.info("strength: full success %d/%d pairs", n, len(PAIRS))
    return scores  # type: ignore[return-value]
