"""Typed data models for the BLUESTAR engine.

Plain ``dataclasses`` are used (no Pydantic dependency) to keep the install
footprint minimal and Python 3.10+ friendly. Every figure that reaches the HTML
carries a :class:`SourceStamp` describing its provenance and reliability so the
renderer can always emit either ``[Source | HH:MM CET | JJ/MM]`` or
``[N/A]`` / ``[PROXY]`` -- never an unsourced number.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time as _midnight
from enum import Enum
from typing import Optional

from . import config as _cfg  # PATCH-IPS-DEDUP (audit 31/07/2026, F-17 bis)
from .config import TZ_CET


class Reliability(str, Enum):
    """Provenance quality of a single datapoint."""

    PRIMARY = "primary"
    FALLBACK = "fallback"
    PROXY = "proxy"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class SourceStamp:
    """Provenance of a single value.

    ``render()`` produces the bracketed citation used throughout the HTML.

    P0-1 (audit du 09/11/2026) — deux horodatages distincts, jamais confondus :

    * ``timestamp``   : heure d'ACQUISITION (la requête HTTP). C'est ce que le
      stamp affichait seul jusqu'ici, d'où le « live · 0,0 h » mensonger sur
      toutes les lignes de la table Intégrité : la table mesurait l'âge de la
      requête, pas l'âge de la donnée.
    * ``observed_at`` : heure de l'OBSERVATION sous-jacente — temps de
      clôture de la bougie Oanda, date de la dernière ligne yfinance, date
      d'observation FRED, date de fixing Frankfurter, ``realtime_start`` du
      GDPNow, date du dernier changement de taux directeur. ``None`` quand la
      source ne l'expose pas (dégradation honnête : on retombe alors sur
      l'ancien comportement, jamais sur une date inventée).

    ``render()`` affiche la date de la DONNÉE dès qu'elle est connue ;
    l'heure d'acquisition reste disponible via ``acquisition_str()`` et dans la
    table « Fraîcheur par champ » du pied de page.
    """

    source_name: str
    reliability: Reliability = Reliability.PRIMARY
    timestamp: Optional[datetime] = None
    url: Optional[str] = None
    note: str = ""
    observed_at: Optional[datetime] = None

    @property
    def observed_is_date_only(self) -> bool:
        """True quand l'observation est une DATE sans heure.

        C'est le cas de toutes les séries quotidiennes/mensuelles (FRED, EIA,
        fixing BCE, changement de taux directeur) : leur horodatage est à
        minuit. Afficher « 00:00 » laisserait croire à une observation
        intraday à minuit ; on n'affiche donc que le jour.
        """
        return (self.observed_at is not None
                and self.observed_at.time() == _midnight(0, 0))

    def observation_str(self) -> str:
        """Date (ISO) de la donnée observée, ou ``"?"`` si inconnue."""
        return self.observed_at.isoformat() if self.observed_at else "?"

    def acquisition_str(self) -> str:
        """Horodatage d'acquisition (UTC, ISO), ou ``"?"`` si inconnu."""
        return self.timestamp.isoformat() if self.timestamp else "?"

    def _observed_label(self) -> str:
        """``06/11`` ou ``06/11 22:00 CET`` (année ajoutée si ≠ acquisition)."""
        obs = self.observed_at
        assert obs is not None
        with_year = (self.timestamp is not None and obs.year != self.timestamp.year)
        day = f"{obs:%d/%m/%Y}" if with_year else f"{obs:%d/%m}"
        if self.observed_is_date_only:
            return f"donnée {day}"
        cet = obs.astimezone(TZ_CET)
        return f"donnée {day} {cet:%H:%M} {cet.tzname() or 'CET'}"

    def render(self) -> str:
        """Return the bracket tag shown in the briefing."""
        if self.reliability is Reliability.UNAVAILABLE:
            return f"[N/A · {self.note}]" if self.note else "[N/A]"
        if self.reliability is Reliability.PROXY:
            base = f"[PROXY · {self.source_name}]" if self.source_name else "[PROXY]"
            # Une observation [PROXY] peut tout de même être datée (ex. proxy
            # interbancaire OCDE mensuel) : la date est alors affichée, sans
            # quoi le lecteur ne peut pas savoir de quand date le proxy.
            if self.observed_at is not None:
                return f"{base[:-1]} | {self._observed_label()}]"
            return base
        if self.observed_at is not None:
            return f"[{self.source_name} | {self._observed_label()}]"
        ts = self.timestamp
        if ts is not None:
            cet = ts.astimezone(TZ_CET)
            # C-08 (audit 31/07/2026) : l'étiquette suit le fuseau réel (CEST en
            # été), plus le littéral "CET" qui affirmait UTC+1 toute l'année
            # alors que l'heure affichée était l'heure de Paris (correcte, mal
            # étiquetée). Vérification externe du 31/07/2026 : acquisition
            # macro 10:38 UTC affichée « 12:38 CET » — heure juste, label faux.
            return f"[{self.source_name} | {cet:%H:%M} {cet.tzname() or 'CET'} | {cet:%d/%m}]"
        return f"[{self.source_name}]"

    @property
    def ok(self) -> bool:
        """True when the value is usable (not unavailable)."""
        return self.reliability is not Reliability.UNAVAILABLE


def dated_stamp(
    source_name: str,
    reliability: Reliability,
    observed_at: Optional[datetime],
    acquired_at: Optional[datetime],
    url: Optional[str] = None,
    note: str = "",
) -> SourceStamp:
    """Stamp qui porte explicitement les DEUX horodatages (P0-1).

    Passe-plat volontairement explicite plutôt qu'un ``SourceStamp(...)``
    positionnel : l'ordre ``observed_at``/``timestamp`` est facile à inverser
    silencieusement, et une inversion reproduirait exactement le défaut que
    P0-1 corrige.
    """
    return SourceStamp(
        source_name,
        reliability,
        timestamp=acquired_at,
        url=url,
        note=note,
        observed_at=observed_at,
    )


def na_stamp(note: str = "") -> SourceStamp:
    """Shorthand for an unavailable datapoint."""
    return SourceStamp("", Reliability.UNAVAILABLE, note=note)


def proxy_stamp(source_name: str = "", note: str = "") -> SourceStamp:
    """Shorthand for a documented approximation."""
    return SourceStamp(source_name, Reliability.PROXY, note=note)


@dataclass
class Datum:
    """A single market value with its provenance.

    ``value`` is ``None`` when unavailable. ``display`` is the formatted string
    used in the HTML (e.g. ``"18,9"`` or ``"N/A"``).
    """

    value: Optional[float]
    stamp: SourceStamp
    display: str = "N/A"
    trend: str = ""

    @property
    def available(self) -> bool:
        return self.value is not None and self.stamp.ok

    @property
    def is_proxy(self) -> bool:
        return self.stamp.reliability is Reliability.PROXY


@dataclass
class MacroEvent:
    """A high-impact calendar event (enriched by the Calendar Layer)."""

    currency: str
    event_name: str
    datetime_utc: str
    date_display: str
    time_display: str
    day_of_week: str
    impact: str
    forecast: str
    previous: str
    actual: str
    hours_until: float
    priority: str            # CRITICAL / HIGH / MEDIUM / PAST
    session: str
    pairs_affected: list[str] = field(default_factory=list)
    is_upcoming: bool = True

    @classmethod
    def from_enriched(cls, d: dict) -> "MacroEvent":
        return cls(
            currency=d.get("currency", ""),
            event_name=d.get("event_name", ""),
            datetime_utc=d.get("datetime_utc", ""),
            date_display=d.get("date_display", ""),
            time_display=d.get("time_display", ""),
            day_of_week=d.get("day_of_week", ""),
            impact=d.get("impact", "high"),
            forecast=d.get("forecast", "—"),
            previous=d.get("previous", "—"),
            actual=d.get("actual", "—"),
            hours_until=float(d.get("hours_until", 0.0)),
            priority=d.get("priority", "MEDIUM"),
            session=d.get("session", "OFF"),
            pairs_affected=list(d.get("pairs_affected", [])),
            is_upcoming=bool(d.get("is_upcoming", True)),
        )


@dataclass
class MarketSnapshot:
    """Snapshot of market gauges and prices, each a :class:`Datum`."""

    as_of_utc: datetime
    gauges: dict[str, Datum] = field(default_factory=dict)   # VIX, MOVE, DXY, US10Y, GDP...
    prices: dict[str, Datum] = field(default_factory=dict)   # instrument -> Datum
    atr: dict[str, float] = field(default_factory=dict)      # instrument -> 14d ATR (absolute)
    closes: dict[str, list[float]] = field(default_factory=dict)  # instrument -> recent daily closes (oldest->newest); reused for [PROXY] correlation, never displayed raw
    # E-1 (audit du 10/11/2026), additif : la MÊME série, datée.
    # ``{instrument: [(date de séance ISO, clôture), ...]}`` (du plus ancien au
    # plus récent), barres INCOMPLÈTES EXCLUES. Indispensable à toute
    # corrélation : les deux fournisseurs n'ont pas la même grille de dates, et
    # zipper deux listes par position décale les rendements d'une séance (le
    # signe de EUR/USD ≈ DXY s'inversait selon l'heure du run).
    series: dict[str, list[tuple[str, float]]] = field(default_factory=dict)
    # AUDIT-FIX (A009): was previously only ever set dynamically via
    # `snap.currency_strength_oanda = ...` in oanda_data.py and read back
    # via `getattr(market, "currency_strength_oanda", None)` in
    # macro_engine.py -- functionally correct but invisible to static
    # typing/mypy and to anyone reading this dataclass. Declaring it here
    # changes nothing at runtime (the existing dynamic assignment still
    # works identically); it only makes the contract explicit. Default
    # `None` matches the exact fallback value `getattr(...)` already used.
    currency_strength_oanda: Optional[dict[str, float]] = None
    # P0-2 (audit 09/11/2026), additif : problèmes détectés PENDANT la collecte
    # (contrôle croisé VIX/US10Y divergent, source dégradée). Ces issues sont
    # agrégées par build_market_snapshot puis fusionnées dans ctx.issues par
    # build_context, pour qu'un écart de source soit VISIBLE dans le HTML et
    # jamais seulement présent dans les logs.
    issues: list["ValidationIssue"] = field(default_factory=list)
    # P1-2 (audit 09/11/2026), additif : fenêtre réellement utilisée par le
    # moteur de force des devises (nombre de séances, paires servies, scores de
    # la DERNIÈRE séance pour l'affichage séparé).
    currency_strength_meta: Optional[dict] = None

    def gauge(self, key: str) -> Datum:
        return self.gauges.get(key, Datum(None, na_stamp(), "N/A"))

    def price(self, key: str) -> Datum:
        return self.prices.get(key, Datum(None, na_stamp(), "N/A"))


@dataclass
class CentralBankSnapshot:
    """Central bank policy state, split FACT vs INTERPRETATION."""

    name: str
    flag: str
    rate_display: str            # e.g. "3,50–3,75%"
    fact: str                    # FAIT: rate + next-decision probability (sourced)
    bias_interpretation: str     # BIAIS: hawkish/dovish/neutre + 1-line argument
    next_meeting: str            # date or [PROXY]/[N/A]
    stamp: SourceStamp = field(default_factory=na_stamp)
    # Optional probability bar (Fed only in the scaffold)
    pause_pct: Optional[int] = None
    cut_pct: Optional[int] = None
    hike_pct: Optional[int] = None
    # N4 (17/07/2026, audit A1): prélèvement FedWatch, affiché sous la barre
    # de probabilité — None quand le payload BCM n'en porte pas (affichage
    # alors masqué, comportement historique inchangé). Additif, jamais inventé.
    fedwatch_as_of: Optional[str] = None
    # N4bis (17/07/2026, audit A1 — cause racine réelle sur le briefing du
    # 16/07): True quand les probabilités pause/cut/hike proviennent des
    # OVERRIDES manuels (cas constaté : 70/0/30 saisi une semaine plus tôt,
    # stamp carte = [FRED] seul → barre affichée sans provenance). Le renderer
    # affiche alors « saisie manuelle [PROXY] » sous la barre. Défaut False →
    # affichage historique inchangé pour les snapshots existants.
    proba_from_override: bool = False


@dataclass
class CotPositioning:
    """Non-Commercials positioning for one currency (CFTC, J-3)."""

    currency: str
    net_contracts: Optional[int]
    ips_score: Optional[int]     # 0-100 [PROXY]
    ips_label: str               # Crowded / Normal / Capitulation
    delta_week: str              # qualitative + / - / stable
    momentum: str                # arrows
    stamp: SourceStamp = field(default_factory=na_stamp)
    # D-2 (audit du 10/11/2026), additif : date des POSITIONS au format ISO
    # (le mardi du rapport CFTC). La date de publication, elle, n'existe pas
    # dans la source : elle est dérivée (règle du vendredi).
    report_date: str = ""
    # D-4 (audit du 10/11/2026), additif : écart-type du net courant à son
    # historique (fourni par institutional). None pour un override manuel, qui
    # ne porte pas d'historique — le rang seul s'applique alors.
    zscore: Optional[float] = None

    @property
    def side(self) -> str:
        """Côté de la position nette : ``long`` / ``short`` / ``flat`` / ``unknown``.

        C-1 (audit du 10/11/2026, additif) : le percentile IPS classe un NIVEAU
        dans son historique. Il ne dit rien du SIGNE de la position. Le côté doit
        donc se lire sur ``net_contracts``, jamais sur le score.
        """
        if self.net_contracts is None:
            return "unknown"
        if self.net_contracts > 0:
            return "long"
        if self.net_contracts < 0:
            return "short"
        return "flat"

    @property
    def state(self) -> str:
        """État de positionnement — DÉFINITION UNIQUE (D-4, audit 10/11/2026).

        Deux grandeurs, explicitement combinées :

        * le **rang** : le percentile IPS est-il dans la bande extrême, et DU
          BON CÔTÉ (``config.IPS_CROWDED`` / ``IPS_CAPITULATION``) ;
        * l'**ampleur** : ``|z|`` versus l'historique (``config.IPS_EXTREME_Z``
          = 2,0 ; ``config.IPS_STRETCHED_Z`` = 1,0).

        Renvoie ``"extreme"`` (rang + |z| >= 2), ``"stretched"`` (rang + 1 <=
        |z| < 2), ``"unwinding"`` (rang haut du mauvais côté), ``"normal"``,
        ``"flat"`` (net nul) ou ``"unknown"`` (net absent).

        Mesure qui a motivé la règle : GBP au 17ᵉ percentile mais |z| = 1,06, et
        CHF au 92ᵉ percentile alors qu'il est net SHORT — les deux définitions
        concurrentes les classaient différemment.
        """
        side = self.side
        if side == "unknown":
            return "unknown"
        if side == "flat":
            return "flat"
        if self.ips_score is None:
            return "normal"
        high = self.ips_score >= _cfg.IPS_CROWDED
        low = self.ips_score <= _cfg.IPS_CAPITULATION
        if not (high or low):
            return "normal"
        # Rang du BON côté : long => percentile haut, short => percentile bas.
        # Sans z (override manuel), on ne peut juger que le rang.
        if self.zscore is None:
            right = high if side == "long" else low
            return "stretched" if right else "unwinding"
        amplitude = abs(self.zscore)
        right_side = high if side == "long" else low
        if not right_side:
            # E-2b (audit du 10/11/2026) : « dégonflage » est une affirmation sur
            # le NIVEAU historique ; elle exige donc l'ampleur, comme « extrême ».
            # Mesure : CAD au 81e percentile mais |z| = 0,91 — son niveau n'a rien
            # d'historiquement extrême, il ne doit pas être annoncé comme tel.
            return "unwinding" if amplitude >= _cfg.IPS_STRETCHED_Z else "normal"
        if amplitude >= _cfg.IPS_EXTREME_Z:
            return "extreme"
        if amplitude >= _cfg.IPS_STRETCHED_Z:
            return "stretched"
        return "normal"

    @property
    def is_extreme(self) -> bool:
        """Positionnement réellement tendu DU BON CÔTÉ.

        C-1 (audit du 10/11/2026) — « extrême » exige les deux conditions :

        * le percentile IPS est extrême (>= IPS_CROWDED ou <= IPS_CAPITULATION) ;
        * ce percentile pointe du côté de la position : un net LONG n'est
          « crowded » que si son percentile est HAUT, un net SHORT que si son
          percentile est BAS.

        Reproduction mesurée du défaut : le franc suisse est net short sur
        **156 semaines sur 156** (min −49 793, max −3 445 contrats). Un net de
        −16 589 y occupe le 92e percentile de sa propre série — c'est le moins
        short depuis trois ans — et le code précédent l'affichait « Crowded
        long » avec `is_extreme = True`, ce qui faussait aussi le décompte du
        moteur de régime (« COT Positioning = 7 extrême(s) »).

        ``net_contracts is None`` (override sans net) conserve à l'identique
        l'ancien comportement fondé sur le seul score : un override manuel ne
        porte pas toujours le net, et le dégrader en « jamais extrême »
        supprimerait une information réellement fournie par l'utilisateur.
        """
        # D-4 : `is_extreme` devient le PALIER « extreme » de la définition
        # unique (rang ET |z| >= IPS_EXTREME_Z). Sans z (override manuel), on
        # conserve à l'identique l'ancien comportement fondé sur le seul rang :
        # un override ne porte pas d'historique, et le dégrader en « jamais
        # extrême » supprimerait une information réellement fournie.
        state = self.state
        if state == "extreme":
            return True
        if state == "stretched" and self.zscore is None:
            return True
        if state == "unknown":
            # Override sans net ni z : ancien comportement (rang seul).
            return (self.ips_score is not None
                    and (self.ips_score >= _cfg.IPS_CROWDED
                         or self.ips_score <= _cfg.IPS_CAPITULATION))
        return False


@dataclass
class CurrencyStrength:
    """One row of the qualitative Currency Strength Ranking ([PROXY])."""

    currency: str
    score: int                   # 0-100 qualitative
    driver: str                  # 3-4 words
    css_class: str = "neutral"   # strong / neutral / weak


@dataclass
class AssetSetup:
    """A full asset card (Section 4) or a row in Section 1 / recap."""

    asset: str
    color: str                   # green / yellow / red
    bias: str                    # short text
    bias_class: str              # long / short / wait
    reason_short: str
    reason_macro: str
    conviction: int              # 1-5 stars (after adjustments)
    action: str                  # CHERCHER LONG / CHERCHER SHORT / ATTENDRE
    action_class: str            # long / short / wait
    arrow: str                   # ↑ / ↓ / ⏸
    # Levels (each with an origin tag)
    zone_buy: str = "[N/A]"
    origin_buy: str = "[N/A]"
    zone_sell: str = "[N/A]"
    origin_sell: str = "[N/A]"
    stop: str = "[N/A]"
    origin_stop: str = "[N/A]"
    expected_move: str = "[N/A]"
    em_method: str = "[N/A]"
    session: str = "—"
    session_reason: str = ""
    invalidation_risk: str = ""
    invalidation_level: str = "[N/A]"
    positioning_link: str = ""
    correlation_key: str = "[PROXY]"
    ips_summary: str = "[N/A]"
    squeeze_risk: str = "Faible"
    squeeze_class: str = "green"
    # AUDIT-FIX (15/07/2026, finding 6 — MINEURE): 'sizing_factor' removed.
    # It was computed every run (compute_sizing_factor in macro_engine.py)
    # but never read by renderer.py or anywhere else — v9.0 replaced the
    # Sizing Factor display with the R:R ratio below (see
    # validation.check_sizing_formula_present's own comment: "In v9.0,
    # Sizing Factor is replaced by R:R"), leaving this a pure
    # dead-code field misleading future maintainers into thinking it
    # reaches the reader. Removed field + its computation + call-site,
    # zero other consumers (verified: no references anywhere else in the
    # codebase).
    risk_reward: str = "[N/A]"     # R:R ratio (e.g. "1:2,5")
    # P0-5 (audit 09/11/2026), additifs : sur quoi repose le R:R affiché.
    # "structurel" = objectif mesuré dans l'historique de clôtures ;
    # "paramétrique" = ratio fixe produit par (LEVEL_ATR_MULT, STOP_ATR_MULT),
    # donc NON informatif — affiché comme tel et retiré des indicateurs de
    # qualité. "indisponible" = ni l'un ni l'autre (ATR/stop manquant).
    rr_basis: str = "indisponible"
    rr_note: str = ""
    # C-4 (audit du 10/11/2026), additifs : valeur numérique du R:R mesuré
    # (None si paramétrique / indisponible) et drapeau « marge de signal
    # faible » calculé à la sélection.
    rr_value: Optional[float] = None
    signal_marginal: bool = False
    price_display: str = "[N/A]"
    levels_are_proxy: bool = False


@dataclass
class RiskScenario:
    """Bull / bear scenario with an anchored trigger."""

    title: str
    proba: str                   # "%" only if anchored, else qualitative
    trigger: str
    trigger_source: str
    rows: list[str] = field(default_factory=list)


@dataclass
class ValidationIssue:
    """A single finding from the validation engine."""

    rule: str
    severity: str                # ERROR / WARN / INFO
    message: str


@dataclass
class BriefingContext:
    """Everything the renderer needs to produce the final HTML."""

    generated_utc: datetime
    generated_cet: datetime
    is_live_session: bool
    operational_note: Optional[str]
    regime: str
    regime_class: str            # regime-on / regime-off / regime-mix (DÉCIDÉE)
    regime_since: str
    market: MarketSnapshot
    central_banks: list[CentralBankSnapshot]
    diff_dominant: str
    diff_implication: str
    macro_theme: str
    macro_theme_src: str
    cot_summary: str
    cot_date: str
    squeeze_currency: Optional[str]
    dxy_context: str
    dxy_src: str
    vol_regime: str
    vol_implication: str
    correlation_summary: str
    liquidity_flow: str
    currency_strength: list[CurrencyStrength]
    ips_scores: list[CotPositioning]
    positioning_alert: Optional[str]
    catalysts_high: list[MacroEvent]
    catalysts_medium: list[MacroEvent]
    catalyst_scenarios: dict[str, dict]   # event_key -> beat/miss/advice/cons/prev
    priority_assets: list[AssetSetup]
    avoid_assets: list[tuple[str, str]]   # (asset, reason)
    no_setup_reason: Optional[str]
    risk_main: dict                       # desc/asset/level/proba/source
    bull: RiskScenario
    bear: RiskScenario
    invalidation_principal: str
    issues: list[ValidationIssue] = field(default_factory=list)
    # v9.0: Enhanced regime and interpretation layers
    regime_assessment: object = None  # RegimeAssessment (avoid circular import)
    interpretation: object = None     # InterpretationLayer
    # P0-1 FIX (Incident Review Board, RC3): whether the calendar feed
    # (Forex Factory) was reachable on this run. Defaults to True so any
    # existing caller that doesn't pass it (e.g. hand-built test fixtures)
    # keeps today's "quiet day" rendering -- no behaviour change unless a
    # caller explicitly sets it to False.
    calendar_reachable: bool = True
    # V4-04 FIX : calendar horizon truncation signal — when the weekly feed
    # is shorter than 168h (e.g. Friday run), the "silence calendaire"
    # is NOT an absence of risk. The renderer emits an honest warning.
    calendar_feed_truncated: bool = False
    calendar_feed_horizon_h: Optional[str] = None  # e.g. "142.5h" for display
    # P1-1 (audit 09/11/2026), additifs : « multi-facteurs » (assess_regime) ou
    # « vix-seul (repli) ». `regime_class` est la classe qui a réellement piloté
    # la sélection ; `vix_tilt` conserve la lecture VIX seule comme indicateur,
    # pour que les deux ne puissent plus se contredire en silence.
    regime_decision_source: str = "multi-facteurs"
    vix_tilt: str = ""
    # H-4 (audit du 11/11/2026), additif : la même alerte de positionnement, en
    # LISTE (une entrée par devise, extrêmes d'abord). Vide quand l'alerte est
    # celle des setups, auquel cas le rendu retombe sur `positioning_alert`.
    positioning_alerts: list[str] = field(default_factory=list)
