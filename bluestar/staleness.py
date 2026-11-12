"""Staleness & Data Coverage Engine — BLUESTAR reliability layer.

This module implements the *data integrity* layer that was missing from the
original codebase.  It addresses audit findings C2, C3, C5, C6:

* **Staleness detection**: every datapoint carries an age; if the age exceeds
  a configurable threshold, the datum is flagged as ``STALE``.
* **Source coverage report**: counts how many fields are live (PRIMARY),
  fallback, proxy, or unavailable — and blocks publication when the live
  coverage ratio falls below a minimum threshold.
* **Weekend & holiday awareness**: market data fetched on a weekend or US
  holiday is automatically stamped with the *data's* effective date (the last
  trading day), not the fetch timestamp.

Design contract:
  * Pure functions — no I/O, no side effects.
  * Never raises; returns structured results.
  * Integrates with existing ``Datum`` / ``SourceStamp`` without breaking
    their contract.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, date
from enum import Enum
from typing import Optional

from .config import (
    MAX_STALENESS_SESSIONS, MIN_LIVE_COVERAGE_RATIO, SESSION_CLOSE_ET_HOUR,
    TZ_ET, TZ_UTC,
)
from .models import Datum, MarketSnapshot, Reliability

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# US Federal holidays (observed) — covers CFTC, BEA, equity/FX market closures.
# This is a minimal static table; for production a proper holiday library
# (e.g. ``pandas-market-calendars``) should be used, but this covers the
# common cases that caused audit finding C1.
# ---------------------------------------------------------------------------
_US_FEDERAL_HOLIDAYS_2026: set[date] = {
    date(2026, 1, 1),    # New Year's Day
    date(2026, 1, 19),   # MLK Day (3rd Monday)
    date(2026, 2, 16),   # Presidents' Day (3rd Monday)
    date(2026, 5, 25),   # Memorial Day (last Monday)
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7, 3),    # Independence Day observed (July 4 = Saturday → Friday)
    date(2026, 9, 7),    # Labor Day (1st Monday)
    date(2026, 10, 12),  # Columbus Day (2nd Monday)
    date(2026, 11, 11),  # Veterans Day
    date(2026, 11, 26),  # Thanksgiving (4th Thursday)
    date(2026, 12, 25),  # Christmas
}

_US_FEDERAL_HOLIDAYS_2025: set[date] = {
    date(2025, 1, 1),
    date(2025, 1, 20),
    date(2025, 2, 17),
    date(2025, 5, 26),
    date(2025, 6, 19),
    date(2025, 7, 4),
    date(2025, 9, 1),
    date(2025, 10, 13),
    date(2025, 11, 11),
    date(2025, 11, 27),
    date(2025, 12, 25),
}


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-ième ``weekday`` (0=lundi) du mois — n=1 pour le premier."""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """Dernier ``weekday`` du mois (Memorial Day)."""
    d = date(year, month, 1) + timedelta(days=31)
    d = d.replace(day=1) - timedelta(days=1)
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d


def _observed(d: date) -> date:
    """Report fédéral : samedi -> vendredi, dimanche -> lundi."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def us_federal_holidays(year: int) -> set[date]:
    """Jours fériés fédéraux US observés pour ``year``, calculés par règles.

    P1-5 (audit 09/11/2026) — remplace les deux ensembles figés 2025/2026, qui
    rendaient la fonction fausse dès janvier 2027 (aucun férié reconnu, donc
    ``sessions_behind`` comptait un jour férié comme une séance cotée).

    Périmètre ASSUMÉ : c'est le calendrier fédéral US, utilisé pour tous les
    actifs. Les fermetures propres aux marchés obligataires / actions (Columbus
    Day et Veterans Day notamment, qui ne ferment pas la même chose selon la
    classe d'actif) ne sont PAS modélisées séparément — voir AUDIT_REPORT.md
    (limite connue P1-5). Pour un seul calendrier, le fédéral est le plus
    conservateur (il ferme plus souvent que le marché des changes).
    """
    return {
        _observed(date(year, 1, 1)),                       # New Year's Day
        _nth_weekday(year, 1, 0, 3),                       # MLK (3e lundi)
        _nth_weekday(year, 2, 0, 3),                       # Presidents' Day
        _last_weekday(year, 5, 0),                         # Memorial Day
        _observed(date(year, 6, 19)),                      # Juneteenth
        _observed(date(year, 7, 4)),                       # Independence Day
        _nth_weekday(year, 9, 0, 1),                       # Labor Day
        _nth_weekday(year, 10, 0, 2),                      # Columbus Day
        _observed(date(year, 11, 11)),                     # Veterans Day
        _nth_weekday(year, 11, 3, 4),                      # Thanksgiving
        _observed(date(year, 12, 25)),                     # Christmas
    }


def is_us_holiday(d: date) -> bool:
    """True si ``d`` est un jour férié fédéral US observé (toute année).

    P1-5 : calculé, plus figé — la fonction reste correcte en 2027, 2030, etc.
    Les ensembles statiques 2025/2026 sont conservés uniquement comme référence
    de vérification (voir tests/test_p15_holidays.py).
    """
    return d in us_federal_holidays(d.year)


def is_weekend(d: date) -> bool:
    """Saturday or Sunday."""
    return d.weekday() >= 5


def last_trading_day(ref: datetime) -> date:
    """Return the most recent US trading day at or before ``ref``.

    Walks backwards from the reference date, skipping weekends and US federal
    holidays.  This is the *effective date* of market data fetched outside
    trading hours (weekend, holiday, pre-market, post-market).
    """
    d = ref.astimezone(TZ_ET).date()
    while is_weekend(d) or is_us_holiday(d):
        d -= timedelta(days=1)
    return d


def last_closed_session(now_utc: datetime,
                        close_hour_et: int = SESSION_CLOSE_ET_HOUR) -> date:
    """Dernière séance dont les données EOD peuvent exister à ``now_utc``.

    D-1 (audit du 10/11/2026) — c'est la RÉFÉRENCE de fraîcheur, et elle n'est
    pas la date calendaire : la séance datée D ne publie qu'après sa clôture.
    Avant ``close_hour_et`` (18:00 ET par défaut), la séance du jour n'a rien
    publié : la référence est donc la séance ouvrée précédente.

    Défaut corrigé, mesuré à horloges injectées (`tools/measure_d1_clock.py`) à
    dates de données IDENTIQUES :

        04:18 UTC (ET 09/11 23:18)  référence 09/11 -> US10Y J-1
        05:00 UTC (ET 10/11 00:00)  référence 10/11 -> US10Y J-2   <-- saut
        20:59 UTC (ET 10/11 15:59)  référence 10/11                (séance ouverte)

    Un retour sous ce plafond n'est pas un retard : c'est le fonctionnement
    normal. Week-ends et jours fériés fédéraux sont sautés.
    """
    et = now_utc.astimezone(TZ_ET)
    d = et.date()
    if et.hour < close_hour_et:
        d -= timedelta(days=1)
    while is_weekend(d) or is_us_holiday(d):
        d -= timedelta(days=1)
    return d


def sessions_behind(observed: date, now_utc: datetime) -> int:
    """Nombre de séances attendues entre ``observed`` et la dernière séance.

    0 = la donnée EST celle de la dernière séance attendue (vraiment live).
    1 = elle date de la séance précédente (EOD J-1, ex. clôture de vendredi
    lue un lundi matin).
    2 = EOD J-2 (ex. une observation FRED du jeudi lue le lundi).

    P0-1 : c'est cette grandeur — et non l'âge de la requête HTTP — qui
    décide si une valeur peut s'afficher « live ». Compte les jours ouvrés
    US (week-ends + fériés fédéraux), donc un lundi férié renvoie bien 1 et
    non 3.
    """
    # D-1 : la référence est la dernière séance CLÔTURÉE, plus la date du jour.
    expected = last_closed_session(now_utc)
    if observed >= expected:
        return 0
    n = 0
    d = observed
    while d < expected:
        d += timedelta(days=1)
        if not (is_weekend(d) or is_us_holiday(d)):
            n += 1
    return n


# ---------------------------------------------------------------------------
# Staleness classification
# ---------------------------------------------------------------------------
class Freshness(str, Enum):
    """Freshness classification for a single datapoint."""
    LIVE = "live"           # data IS the current/most recent expected session
    # P0-1 (audit 09/11/2026) : une observation d'une séance ANTÉRIEURE (mais
    # récente) n'est ni « live » ni « stale ». Sans cette classe, la seule
    # alternative était de l'appeler « live » (défaut corrigé : une clôture de
    # vendredi s'affichait « live · 0,0 h » un lundi matin) ou « stale »
    # (faux : une clôture de vendredi est la donnée la plus fraîche qui existe
    # un samedi). EOD = End Of Day d'une séance antérieure, avec son rang.
    EOD = "eod"
    STALE = "stale"         # data age exceeds the staleness threshold
    PROXY = "proxy"         # documented approximation, not a real observation
    UNAVAILABLE = "unavailable"


@dataclass
class StalenessReport:
    """Per-datum staleness assessment."""
    field_name: str
    reliability: Reliability
    freshness: Freshness
    age_hours: Optional[float]       # None if no timestamp
    data_effective_date: Optional[str]  # ISO date of the underlying data
    fetch_timestamp: Optional[str]      # ISO datetime of when it was fetched
    is_stale: bool
    note: str = ""
    # P0-1 (additifs) : l'instant de l'OBSERVATION et son rang en séances.
    # `sessions_behind` est None pour les champs macro (pas de notion de
    # séance) et pour les données dont la date est inconnue.
    observed_timestamp: Optional[str] = None
    sessions_behind: Optional[int] = None


@dataclass
class CoverageReport:
    """Aggregate source coverage for the entire briefing."""
    total_fields: int = 0
    live_count: int = 0
    fallback_count: int = 0
    proxy_count: int = 0
    unavailable_count: int = 0
    stale_count: int = 0
    eod_count: int = 0          # P0-1 : séance antérieure, mais récente
    fields: list[StalenessReport] = field(default_factory=list)

    @property
    def live_ratio(self) -> float:
        """Fraction of fields that are live (PRIMARY + not stale)."""
        if self.total_fields == 0:
            return 0.0
        return self.live_count / self.total_fields

    @property
    def usable_ratio(self) -> float:
        """Fraction of fields usable for publication.

        live + fallback + EOD, stale excluded. P0-1 : les champs EOD sont
        utilisables (une clôture de vendredi reste exploitable un lundi) mais
        ne sont jamais comptés « live » — c'est `live_ratio` qui le dit.
        """
        if self.total_fields == 0:
            return 0.0
        return (self.live_count + self.fallback_count + self.eod_count) / self.total_fields

    @property
    def publication_blocked(self) -> bool:
        """True if the usable coverage is too low to publish.

        RC2 FIX (Incident Review Board) [DATA-GOVERNANCE POLICY — flagged for
        sign-off]: gate on ``usable_ratio`` (live + fresh fallback) instead of
        ``live_ratio`` (PRIMARY only). In the sanctioned zero-key mode every
        market field is a *fresh* yfinance FALLBACK (freshness=LIVE,
        is_stale=False), so live_ratio is structurally 0% and the briefing could
        NEVER publish even with 22/25 fresh fields (demonstrated: usable_ratio
        88% while live_ratio 0%). usable_ratio still excludes stale/unavailable
        data, so genuinely degraded runs remain blocked. One-line semantic change;
        no other behaviour altered."""
        return self.usable_ratio < MIN_LIVE_COVERAGE_RATIO

    def summary_line(self) -> str:
        """One-line human-readable summary for the HTML footer."""
        return (
            f"Couverture sources : {self.live_count} live · "
            f"{self.eod_count} EOD (séance antérieure) · "
            f"{self.fallback_count} fallback · {self.proxy_count} proxy · "
            f"{self.unavailable_count} N/A · {self.stale_count} stale · "
            f"ratio live = {self.live_ratio:.0%} · "
            f"ratio exploitable = {self.usable_ratio:.0%}"
        )


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------
# A datum is STALE if its effective data date is older than this.
# For FX/market data: 24h on a trading day, 72h over a weekend.
MAX_STALENESS_HOURS_WEEKDAY = 26      # slightly more than 24h to allow for TZ drift
MAX_STALENESS_HOURS_WEEKEND = 96      # Fri close → Mon open is ~62h, 96h gives margin

# Macro data (GDPNow, COT, etc.) can legitimately be older.
MAX_STALENESS_HOURS_MACRO = 7 * 24   # 7 days for slow-moving macro series

# Minimum coverage ratio to allow publication : SOURCE UNIQUE = config.py
# (P1-4 : la constante était dupliquée ici et dans config.py).

# Fields that are "macro" (slow-moving) and get a longer staleness window.
_MACRO_FIELDS = {"GDP_NOWCAST", "SURPRISE_IDX"}


def _staleness_threshold(field_name: str, now_utc: datetime) -> float:
    """Return the staleness threshold in hours for a given field."""
    if field_name in _MACRO_FIELDS:
        return MAX_STALENESS_HOURS_MACRO
    # Check if we're in a weekend context
    now_et = now_utc.astimezone(TZ_ET)
    if is_weekend(now_et.date()):
        return MAX_STALENESS_HOURS_WEEKEND
    return MAX_STALENESS_HOURS_WEEKDAY


def assess_datum(
    field_name: str,
    datum: Datum,
    now_utc: datetime,
) -> StalenessReport:
    """Assess the staleness of a single ``Datum``.

    The key insight (audit C2): the ``SourceStamp.timestamp`` is the *fetch*
    time, not the *data* time.  For market data fetched on a weekend, the
    effective data date is the last trading day (Friday).  We compute the
    data's effective date and compare it to the staleness threshold.
    """
    stamp = datum.stamp
    obs_ts = stamp.observed_at
    fetch_ts = stamp.timestamp

    if not datum.stamp.ok:
        return StalenessReport(
            field_name=field_name,
            reliability=stamp.reliability,
            freshness=Freshness.UNAVAILABLE,
            age_hours=None,
            data_effective_date=None,
            fetch_timestamp=fetch_ts.isoformat() if fetch_ts else None,
            is_stale=False,
            note=stamp.note or "unavailable",
            observed_timestamp=obs_ts.isoformat() if obs_ts else None,
        )

    if stamp.reliability is Reliability.PROXY:
        return StalenessReport(
            field_name=field_name,
            reliability=Reliability.PROXY,
            freshness=Freshness.PROXY,
            age_hours=None,
            data_effective_date=None,
            fetch_timestamp=fetch_ts.isoformat() if fetch_ts else None,
            is_stale=False,
            note=stamp.note or "proxy",
            observed_timestamp=obs_ts.isoformat() if obs_ts else None,
        )

    # P0-1 : c'est l'instant de la DONNÉE qui fait foi. L'heure d'acquisition
    # n'est qu'un repli documenté quand la source n'expose pas d'observation.
    reference = obs_ts or fetch_ts
    if reference is None:
        return StalenessReport(
            field_name=field_name,
            reliability=stamp.reliability,
            freshness=Freshness.LIVE,  # benefit of the doubt
            age_hours=None,
            data_effective_date=None,
            fetch_timestamp=None,
            is_stale=False,
            note="aucun horodatage (ni donnée ni acquisition) — fraîcheur non vérifiée",
        )

    age_h = (now_utc - reference).total_seconds() / 3600.0

    if field_name in _MACRO_FIELDS:
        # Pas de notion de séance pour une série macro : seuil en heures, et
        # la date de référence est la date de PUBLICATION quand elle est
        # connue (observed_at), sinon celle de l'acquisition.
        effective = reference.astimezone(TZ_ET).date()
        threshold = _staleness_threshold(field_name, now_utc)
        is_stale = age_h > threshold
        return StalenessReport(
            field_name=field_name,
            reliability=stamp.reliability,
            freshness=Freshness.STALE if is_stale else Freshness.LIVE,
            age_hours=round(age_h, 1),
            data_effective_date=effective.isoformat(),
            fetch_timestamp=fetch_ts.isoformat() if fetch_ts else None,
            is_stale=is_stale,
            note=("stale" if is_stale
                  else ("" if obs_ts else "date d'observation inconnue — âge mesuré sur l'acquisition")),
            observed_timestamp=obs_ts.isoformat() if obs_ts else None,
        )

    # --- Donnée de marché quotidienne : le verdict est en SÉANCES, pas en h.
    if obs_ts is not None:
        # Une observation datée sans heure (FRED, EIA, fixing) porte une date
        # calendaire publiée : la convertir en fuseau ET la décalerait d'un
        # jour. On garde donc la date telle que publiée.
        effective = (obs_ts.date() if stamp.observed_is_date_only
                     else obs_ts.astimezone(TZ_ET).date())
        behind = sessions_behind(effective, now_utc)
        if behind == 0:
            freshness = Freshness.LIVE
            note = ""
        elif behind <= MAX_STALENESS_SESSIONS:
            freshness = Freshness.EOD
            note = f"EOD J-{behind} — séance antérieure à la dernière séance attendue"
        else:
            freshness = Freshness.STALE
            note = f"stale — {behind} séances de retard (max {MAX_STALENESS_SESSIONS})"
        return StalenessReport(
            field_name=field_name,
            reliability=stamp.reliability,
            freshness=freshness,
            age_hours=round(age_h, 1),
            data_effective_date=effective.isoformat(),
            fetch_timestamp=fetch_ts.isoformat() if fetch_ts else None,
            is_stale=freshness is Freshness.STALE,
            note=note,
            observed_timestamp=obs_ts.isoformat(),
            sessions_behind=behind,
        )

    # --- Repli documenté : aucune date d'observation exposée par la source.
    # On retombe sur l'ancien comportement (dernière séance à la date
    # d'acquisition) en le DISANT dans `note`, pour que la table Intégrité ne
    # puisse pas laisser croire à une observation datée.
    effective = last_trading_day(reference)
    threshold = _staleness_threshold(field_name, now_utc)
    real_age = (now_utc.astimezone(TZ_ET).date() - effective).total_seconds() / 3600.0
    age_h = max(age_h, real_age)
    is_stale = age_h > threshold
    return StalenessReport(
        field_name=field_name,
        reliability=stamp.reliability,
        freshness=Freshness.STALE if is_stale else Freshness.LIVE,
        age_hours=round(age_h, 1),
        data_effective_date=effective.isoformat(),
        fetch_timestamp=fetch_ts.isoformat() if fetch_ts else None,
        is_stale=is_stale,
        note=("stale" if is_stale
              else "date d'observation non exposée par la source — âge mesuré sur l'acquisition"),
        sessions_behind=None,
    )


def build_coverage_report(
    market: MarketSnapshot,
    now_utc: datetime,
) -> CoverageReport:
    """Build a full source-coverage report for the market snapshot.

    This is the function that should be called after ``build_market_snapshot``
    and before ``build_context`` — or at the end of the pipeline — to assess
    whether the briefing has enough live data to be published.
    """
    report = CoverageReport()
    now_utc = now_utc or datetime.now(TZ_UTC)

    def _count(sr: StalenessReport) -> None:
        """Comptage unique pour les jauges ET les prix (P0-1)."""
        report.fields.append(sr)
        report.total_fields += 1
        if sr.freshness is Freshness.UNAVAILABLE:
            report.unavailable_count += 1
        elif sr.freshness is Freshness.PROXY:
            report.proxy_count += 1
        elif sr.freshness is Freshness.EOD:
            # P0-1 : utilisable, mais JAMAIS compté live.
            report.eod_count += 1
        elif sr.freshness is Freshness.STALE:
            # AUDIT-FIX (institutional review, 14/07/2026): a stale datum
            # must not count toward live_count regardless of its original
            # source reliability. The old code did
            # `if PRIMARY: live_count += 1  # stale but was live`, which let
            # badly-stale PRIMARY data inflate live_ratio -- publication_blocked
            # could stay False even when the "live" data was actually days
            # old. Zero-regression direction: this can only ever move a
            # field OUT of live_count, so live_ratio can only go down or
            # stay the same relative to the old formula, never up -- a run
            # that was already (correctly) blocked stays blocked, this only
            # catches cases that were wrongly NOT blocked before.
            report.stale_count += 1
        elif sr.reliability is Reliability.PRIMARY:
            report.live_count += 1
        elif sr.reliability is Reliability.FALLBACK:
            report.fallback_count += 1

    # Gauges
    for key, datum in market.gauges.items():
        _count(assess_datum(key, datum, now_utc))

    # Prices
    for key, datum in market.prices.items():
        _count(assess_datum(key, datum, now_utc))

    return report


def stale_fields_summary(report: CoverageReport) -> str:
    """Human-readable list of stale fields for the validation engine.

    P0-1 : les champs EOD (séance antérieure) sont signalés ici aussi — ils
    ne bloquent pas la publication mais le lecteur doit savoir lesquels ne
    sont pas de la dernière séance. C'est exactement ce que l'ancien message
    ne pouvait pas dire, faute de les distinguer des données live.
    """
    stale = [f for f in report.fields if f.is_stale]
    eod = [f for f in report.fields if f.freshness is Freshness.EOD]
    parts = []
    if stale:
        items = []
        for f in stale:
            age_str = f"{f.age_hours:.0f}h" if f.age_hours is not None else "?"
            items.append(f"{f.field_name} ({age_str})")
        parts.append("Données stale : " + " · ".join(items))
    if eod:
        items = []
        for f in eod:
            rank = f"J-{f.sessions_behind}" if f.sessions_behind is not None else "?"
            items.append(f"{f.field_name} ({rank})")
        parts.append("Séance antérieure (EOD, pas live) : " + " · ".join(items))
    return " · ".join(parts)
