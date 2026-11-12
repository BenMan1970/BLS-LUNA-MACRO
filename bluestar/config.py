"""Central configuration and universe constants for the BLUESTAR engine.

Everything that is a tunable knob or a fixed reference list lives here so the
rest of the codebase stays free of magic numbers. No secrets are stored here;
the engine runs with zero API keys (degrading to [N/A]/[PROXY] when an external
source is unavailable).
"""
from __future__ import annotations

from zoneinfo import ZoneInfo

# ----------------------------------------------------------------------------
# Timezones
# ----------------------------------------------------------------------------
TZ_UTC = ZoneInfo("UTC")
TZ_CET = ZoneInfo("Europe/Paris")  # CET/CEST display timezone
TZ_ET  = ZoneInfo("America/New_York")  # CFTC publication timezone (Eastern)

# ----------------------------------------------------------------------------
# Calendar Layer (validated Forex Factory module)
# ----------------------------------------------------------------------------
FF_JSON_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CALENDAR_CACHE_TTL = 300           # seconds (matches the validated module)
RESIDUAL_RISK_WINDOW_H = 72        # past events kept in events_engine

# ----------------------------------------------------------------------------
# Market Data Layer
# ----------------------------------------------------------------------------
MARKET_CACHE_TTL = 300             # seconds
HTTP_TIMEOUT = 15                  # seconds
HTTP_RETRIES = 2
HTTP_BACKOFF = 1.5                 # seconds, exponential base
MARKET_FETCH_MAX_WORKERS = 8       # cap on concurrent instrument fetches
                                    # (Oanda v20 / yfinance) — bounds load on
                                    # the upstream APIs while still giving a
                                    # large wall-clock win over sequential.

# yfinance tickers used as a *no-key* primary/fallback market source.
# When yfinance (or the network) is unavailable the field becomes [N/A].
YF_TICKERS = {
    "VIX": "^VIX",
    "MOVE": "^MOVE",          # frequently unavailable -> [PROXY]/[N/A]
    "DXY": "DX-Y.NYB",
    "US10Y": "^TNX",          # quoted x10 (e.g. 43.8 => 4.38%)
    "XAU/USD": "GC=F",        # gold futures front month as spot proxy
    "Brent": "BZ=F",
    "WTI": "CL=F",
    "EUR/USD": "EURUSD=X",
    "GBP/USD": "GBPUSD=X",
    "USD/JPY": "JPY=X",
    "USD/CHF": "CHF=X",
    "AUD/USD": "AUDUSD=X",
    "NZD/USD": "NZDUSD=X",
    "USD/CAD": "CAD=X",
    "EUR/GBP": "EURGBP=X",
    "GBP/JPY": "GBPJPY=X",
    "DAX": "^GDAXI",
    "US30": "^DJI",
    "NAS100": "^NDX",
    "SPX500": "^GSPC",
}

# ----------------------------------------------------------------------------
# Universe (BLUESTAR mandate) -- NO crypto.
# ----------------------------------------------------------------------------
FX_PAIRS = [
    "EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF", "AUD/USD",
    "NZD/USD", "USD/CAD", "EUR/GBP", "GBP/JPY",
]
INDICES = ["DAX", "US30", "NAS100", "SPX500"]
COMMODITIES = ["XAU/USD", "Brent", "WTI"]
UNIVERSE = FX_PAIRS + INDICES + COMMODITIES

MAJOR_CURRENCIES = ["USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD"]

# Currencies that make up each instrument (used for positioning links).
INSTRUMENT_CCYS = {
    "EUR/USD": ("EUR", "USD"), "GBP/USD": ("GBP", "USD"),
    "USD/JPY": ("USD", "JPY"), "USD/CHF": ("USD", "CHF"),
    "AUD/USD": ("AUD", "USD"), "NZD/USD": ("NZD", "USD"),
    "USD/CAD": ("USD", "CAD"), "EUR/GBP": ("EUR", "GBP"),
    "GBP/JPY": ("GBP", "JPY"),
    # V4-03 FIX (round de validation zero-régression, 02/08/2026, MAJEUR) :
    # ces 7 instruments non-FX étaient absents de ce registre, donc
    # invisibles aux trois consommateurs de INSTRUMENT_CCYS
    # (`select_priority_assets` : gating calendaire ET garde-fou de
    # diversification par devise ; `build_context` : alerte de
    # positionnement IPS). Confirmé par l'audit indépendant (rapport RUN-4,
    # V4-03) : "XAU/USD, DAX, US30, NAS100, SPX500, Brent, WTI traversent la
    # sélection sans aucun contrôle de blackout", asymétrie avec le Desk qui,
    # lui, applique bien le calendrier USD à SPX500/USD.
    # Convention retenue, cohérente avec la dénomination effective de
    # chaque instrument (indices actions et matières premières cotés/pilotés
    # en USD, sauf le DAX qui est un indice de la zone euro) : un
    # tuple à une seule devise, structurellement compatible avec
    # `_events_for_ccys` (filtre `e.currency in ccys`, longueur quelconque)
    # sans aucune modification de cette fonction.
    "XAU/USD": ("USD",),
    "Brent": ("USD",),
    "WTI": ("USD",),
    "DAX": ("EUR",),
    "US30": ("USD",),
    "NAS100": ("USD",),
    "SPX500": ("USD",),
}

# Safe-haven currencies (used by the qualitative currency-strength overlay).
SAFE_HAVENS = ["USD", "JPY", "CHF"]

# ----------------------------------------------------------------------------
# Regime thresholds (VIX based, combined with other gauges in the engine)
# ----------------------------------------------------------------------------
VIX_RISK_ON_MAX = 15.0     # VIX below -> calm / risk-on tilt
VIX_RISK_OFF_MIN = 22.0    # VIX above -> stress / risk-off tilt

# MOVE (ICE BofA bond-vol index) thresholds used alongside VIX in the
# multi-factor regime engine (regime_engine.py). Values unchanged from the
# figures that were previously hardcoded inline there (90 / 120) — centralised
# here only so both the classification and the trigger-explainer text share a
# single source of truth instead of independently hardcoded numbers.
MOVE_RISK_ON_MAX = 90.0    # MOVE below -> calm bond-vol / risk-on tilt
MOVE_RISK_OFF_MIN = 120.0  # MOVE above -> stressed bond-vol / risk-off tilt

# Minimum cumulative weight (see RegimeIndicator.weight) a signal bucket must
# reach in regime_engine._classify() before it is allowed to steer the regime
# decision tree. Set to 0.10, the weight of a single primary directional
# indicator (MOVE/US10Y/DXY/Rate-Differential outright reading). Below this
# bar a signal is treated as present-but-immaterial and cannot, on its own,
# flip the regime.
#
# Audit fix (BLUESTAR v9.x correction pass, July 2026): the previous rule
# ("has_X = scores.get(X, 0) > 0") let ANY signal with weight > 0 gate the
# decision tree regardless of magnitude, so a single 0.05-weight confirmatory
# indicator (P/C Ratio, COT Positioning, GDPNow) could unilaterally set the
# regime even while outweighed 2:1 by a "neutral" reading from a primary
# indicator (documented reproduction: VIX=15.03 -> neutral/0.10, P/C
# complacence -> risk_on/0.05, dominant bucket is "neutral" yet the old code
# returned "Risk-On" at a displayed 67% confidence). This directly violates
# _pc_indicator's own documented contract ("Weight 0.05 — informative but
# never regime-determining alone"). Heuristic value, not backtested — revisit
# if/when the regime engine gets a proper calibration pass.
REGIME_MATERIAL_SIGNAL_WEIGHT = 0.10

# ----------------------------------------------------------------------------
# Positioning / IPS heuristic (Non-Commercials only) -- always [PROXY]
# ----------------------------------------------------------------------------
IPS_CROWDED = 80           # >80  => crowded (squeeze risk inverse)
IPS_CAPITULATION = 20      # <20  => capitulation / crowded short

# ----------------------------------------------------------------------------
# D-4 (audit du 10/11/2026) — AMPLEUR relative à l'historique, en écarts-types
# ----------------------------------------------------------------------------
# Un percentile extrême dit qu'une position est au bord de SON historique ; il
# ne dit pas de combien. Mesure du 03/11/2026 : GBP au 17e percentile mais à
# |z| = 1,06 seulement, alors qu'EUR au 1er percentile est à |z| = 2,72. Appeler
# les deux « capitulation » efface la différence d'ampleur.
#
# La définition unique combine donc les DEUX grandeurs (voir
# models.CotPositioning.state) :
#   * |z| >= IPS_EXTREME_Z   -> « extrême » (risque de squeeze)
#   * |z| >= IPS_STRETCHED_Z -> « tendu »
# Seuils HEURISTIQUES, non backtestés. Aucun seuil existant n'est modifié : ce
# sont deux seuils NOUVEAUX, appliqués en plus des 80/20.
IPS_EXTREME_Z = 2.0
IPS_STRETCHED_Z = 1.0
# Net contracts magnitude that maps to a "full" extreme reading (heuristic
# scaling because we do not have the complete CFTC history to compute a true
# percentile). Documented and surfaced as [PROXY] in the HTML.
IPS_FULL_SCALE_CONTRACTS = 150_000

# ----------------------------------------------------------------------------
# Sizing factor (NOT a real Kelly)
# ----------------------------------------------------------------------------
SIZING_VIX_DENOM = 30.0    # Sizing = conviction * 1/(1 + VIX/30)
SIZING_PROXY_VIX = 20.0    # assumed VIX when VIX is unavailable (+[PROXY])

# Expected-move ATR multipliers when only [PROXY] data is available.
PROXY_ATR_PCT = 0.006      # ~0.6% of price as a daily move proxy
LEVEL_ATR_MULT = 1.0       # buy/sell zone distance in ATR units
STOP_ATR_MULT = 1.8        # stop distance in ATR units

# ----------------------------------------------------------------------------
# Operating modes (affect selection thresholds / max conviction)
# ----------------------------------------------------------------------------
MODES = ("Conservative", "Normal", "Aggressive")
MODE_SELECTION_MIN_SCORE = {
    "Conservative": 0.62,
    "Normal": 0.50,
    "Aggressive": 0.40,
}
MAX_PRIORITY_ASSETS = 3

# ----------------------------------------------------------------------------
# C-4 (audit du 10/11/2026) — qualité d'un setup, deux seuils HEURISTIQUES
# ----------------------------------------------------------------------------
# R:R minimal pour qu'un setup reste présenté comme « prioritaire ». Mesuré sur
# le briefing du 10/11/2026 : AUD/USD 1:0,6 et GBP/JPY 1:0,5 étaient affichés en
# « ACTIFS PRIORITAIRES » avec « CHERCHER LONG/SHORT », c'est-à-dire une
# récompense INFÉRIEURE au risque. 1,0 = « on risque autant qu'on peut
# espérer gagner » : en dessous, le setup n'est pas un setup.
#
# HEURISTIQUE, pas une vérité : ce seuil ne dit rien de la probabilité de
# toucher l'objectif, il écarte seulement les configurations où le rapport
# récompense/risque est défavorable par construction. Il ne s'applique QU'AUX
# R:R MESURÉS (objectif structurel) — un R:R paramétrique (1,0/1,8 ATR) n'est
# pas une mesure et ne peut donc pas servir de filtre.
MIN_RR_RATIO_FOR_PRIORITY = 1.0

# Marge minimale entre le score composite d'un actif et le seuil de sélection
# du mode, en deçà de laquelle le setup est signalé « marginal ». Mesuré :
# GBP/JPY score 0,549 contre un seuil de 0,50 => marge +0,049, soit ~10 % du
# seuil ; c'est exactement le genre de marge qu'un changement de séance
# retourne (le 09/11 09:21 le même GBP/JPY était SHORT).
#
# HEURISTIQUE également : 0,05 correspond à 5 points de score composite, soit
# ~11 points de différentiel de force entre les deux jambes. Aucun lissage ni
# hystérésis n'est appliqué — la mention est purement informative.
SIGNAL_MARGIN_MIN = 0.05

# ----------------------------------------------------------------------------
# CB rate-derived bias fallback (audit fix, 23/07/2026 — synergy gap #1)
# ----------------------------------------------------------------------------
# Approximate consensus nominal-neutral-rate bands per central bank, used
# ONLY to derive a hawkish/dovish/neutral bias tag from the live FRED/BoE
# policy rate when no textual override (FAIT/BIAIS narrative) is supplied.
# This is a heuristic reference point, not a forecast, model output, or
# official central-bank communication — the rendered text is explicitly
# tagged "[dérivé taux]" wherever it is shown so it is never mistaken for a
# sourced FOMC/ECB/BoJ/BoE statement read. A manual override, when present,
# always takes priority over this derivation (see
# macro_engine._derive_bias_from_rate). Heuristic, not backtested — revisit
# as each central bank's own neutral-rate estimate evolves.
CB_NEUTRAL_RATE_BAND = {
    "FED": (2.50, 3.50),
    "BCE": (1.50, 2.50),
    "BoJ": (0.25, 1.00),
    "BoE": (2.50, 3.50),
    # P0-7d (audit 09/11/2026) — bandes des banques ajoutées. MÊME statut
    # heuristique que les quatre précédentes : ordre de grandeur du taux
    # neutre estimé, pas une prévision ni un communiqué. La bande SNB inclut
    # explicitement le territoire négatif (le taux SNB y a séjourné de 2015 à
    # 2022). À revoir si le taux neutre estimé de ces banques évolue.
    "RBA": (2.00, 3.50),
    "RBNZ": (1.50, 3.00),
    "BoC": (2.00, 3.00),
    "SNB": (-0.75, 0.75),
}

# ----------------------------------------------------------------------------
# E-6 (audit du 11/11/2026) — convention de lecture des FOURCHETTES de taux
# ----------------------------------------------------------------------------
# Quand un taux directeur est publié en fourchette (la Fed cible 3,75–4,00 %), le
# différentiel utilise le MILIEU (3,875 %), pas la borne haute : c'est la seule
# convention symétrique, et elle est indépendante du sens de l'écart. La borne
# haute seule gonflerait mécaniquement la jambe concernée (D-4/P0-7 en ont déjà
# corrigé l'affichage). Le libellé du différentiel NOMME cette convention.
CB_RATE_RANGE_CONVENTION = "milieu de fourchette"

# ----------------------------------------------------------------------------
# E-7 (audit du 11/11/2026) — alerte de positionnement par DEVISE
# ----------------------------------------------------------------------------
# D-5 a livré `currency_positioning_alerts()` mais ne l'a pas câblée : l'alerte
# affichée reste liée aux SETUPS, donc vide dès qu'aucun setup n'est retenu —
# mesure du 10/11 : `positioning_alert = ""` alors qu'EUR est extrême (|z| = 2,72)
# et que GBP/JPY/AUD/NZD sont tendues.
#
# DÉCISION UTILISATEUR, donc drapeau DÉSACTIVÉ PAR DÉFAUT : False reproduit
# exactement le comportement actuel ; True affiche une ligne par devise (extrêmes
# d'abord, triées par |z|). Aucun test ne dépend de la valeur : les deux états
# sont couverts.
# G-4 (11/11/2026) — DÉCISION UTILISATEUR : ON.
# L'alerte de positionnement devient l'alerte par DEVISE (D-5) : une ligne par
# devise extrême/tendue/en dégonflage, indépendante des setups. Mesure du 11/11 :
# 6 lignes au lieu de 0.
POSITIONING_ALERT_BY_CURRENCY = True

# ----------------------------------------------------------------------------
# F-1 (audit du 11/11/2026) — que faire d'un R:R NON MESURÉ ?
# ----------------------------------------------------------------------------
# `_filter_low_rr_setups` ne filtre que les R:R MESURÉS (`rr_basis ==
# "structurel"`) : un R:R paramétrique (repli 1:2,5 quand aucun objectif
# structurel n'existe) n'a pas de valeur mesurée, donc il échappe au seuil et
# reste « ACTIF PRIORITAIRE ».
#
# MESURE sur le rejeu E-4 (267 couples, 30 séances) :
#   * 113 paramétriques (42,3 %), TOUS à moins de 0,5 ATR de l'extrême
#     (max 0,295) — c'est mécanique : le repli paramétrique sert précisément
#     quand le prix EST à l'extrême, donc quand il n'y a pas d'objectif
#     structurel devant ;
#   * ils forment 73,9 % des survivants sous le seuil 1,0.
#
# Trois politiques, chiffrées dans AUDIT_REPORT_round5.md. DÉFAUT = comportement
# actuel, aucune décision prise ici :
#   "status_quo"    : publié en prioritaire, avec le ratio fixe 1:2,5
#   "watch"         : V1 — non évalué, donc NON prioritaire ; listé « à
#                     surveiller » avec sa raison
#   "conservative"  : V2 — R:R réputé égal à RR_PARAMETRIC_CONSERVATIVE, puis
#                     filtre normal
# G-4 (11/11/2026) — DÉCISION UTILISATEUR : V1 « watch ».
# Un R:R non mesuré n'est plus publié en « actif prioritaire » : il est écarté
# du prioritaire et listé à surveiller avec sa raison. Mesure (passe 5) :
# 5,10 -> 1,33 setup publié par séance, 73,9 % -> 0 % de paramétriques.
RR_PARAMETRIC_POLICY = "watch"

# V2 : valeur conservatrice. MESURÉE, pas choisie arbitrairement : 10 % des R:R
# réellement mesurés du rejeu sont <= 0,20 (P10 = 0,20 = minimum observé sur une
# distribution quantifiée 0,2/0,3/0,4…). C'est une hypothèse PESSIMISTE : elle
# traite l'inconnu comme le pire cas mesuré, jamais comme le meilleur.
RR_PARAMETRIC_CONSERVATIVE = 0.20

# V3 : bandeau explicite « R:R non mesuré (objectif structurel indisponible) »
# sur la carte et dans le récapitulatif — indépendant de la politique ci-dessus,
# pour que le statu quo cesse au moins d'être SILENCIEUX.
RR_PARAMETRIC_BANNER = False

# ----------------------------------------------------------------------------
# G-3 (audit du 11/11/2026) — estampillage des HTML de TEST
# ----------------------------------------------------------------------------
# Un snapshot de test ne doit jamais pouvoir passer pour un briefing publiable.
# False (défaut) : production inchangée. True : bandeau rouge visible
# « SNAPSHOT DE TEST — données gelées, ne pas utiliser pour trader » et <title>
# distinct. Les tests de rendu (dont le smoke test d'app.py) le passent à True ;
# aucun chemin de production ne le modifie.
SNAPSHOT_MODE = False

# ----------------------------------------------------------------------------
# H-1 (audit du 11/11/2026) — UNE règle pour les gauges EOD (VIX, US10Y)
# ----------------------------------------------------------------------------
# Règle : afficher la SÉANCE LA PLUS RÉCENTE disponible, étiquetée par sa source
# et sa date ; FRED reste le contrôle croisé (l'écart est signalé, jamais masqué).
#
# Mesure qui justifie la règle (tools/measure_h1_us10y.py, 11/11/2026) :
#   |FRED DGS10 − ^TNX| sur séances communes : max 0,016 pt, moyenne 0,007 pt
#   ^TNX 09/11 = 4,864 -> 10/11 = 4,983  => +0,119 pt de VRAIE variation
# L'écart de 0,10 pt signalé par le contrôle croisé n'était donc pas une
# divergence de source : le briefing affichait simplement une séance de retard.
#
# Le VIX applique DÉJÀ cette règle (yfinance primaire, FRED en contrôle) ; le
# drapeau l'étend à l'US10Y. False = comportement historique (FRED primaire,
# même quand yfinance a une séance de plus).
EOD_GAUGE_FRESHEST_SOURCE = False

# ----------------------------------------------------------------------------
# Staleness & coverage thresholds (v9.0 — audit C2/C3/C5 fix)
# ----------------------------------------------------------------------------
MIN_LIVE_COVERAGE_RATIO = 0.30   # minimum fraction of live fields to publish

# ----------------------------------------------------------------------------
# P0-1 (audit 09/11/2026) — fraîcheur mesurée en SÉANCES, pas en heures
# ----------------------------------------------------------------------------
# Une observation datée de la dernière séance attendue est LIVE ; d'une séance
# antérieure (mais récente) elle est EOD (J-n) ; au-delà de ce seuil elle est
# STALE. 2 séances = 1 week-end normal (vendredi → lundi) + 1 séance de
# tolérance pour une source qui publie en décalage (FRED VIXCLS/DGS10, EIA).
MAX_STALENESS_SESSIONS = 2

# D-1 (audit du 10/11/2026) — heure (ET) à partir de laquelle la séance du jour
# est considérée CLÔTURÉE, donc utilisable comme référence de fraîcheur.
# 18:00 ET : la séance actions US a clôturé (16:00) et la séance FX aussi
# (17:00), et les séries EOD (FRED H.15, VIXCLS) sont publiées. Avant 18:00 ET,
# la séance du jour ne peut pas être « attendue » : elle n'a rien publié.
SESSION_CLOSE_ET_HOUR = 18

# ----------------------------------------------------------------------------
# P0-2 (audit 09/11/2026) — contrôle croisé FRED / yfinance
# ----------------------------------------------------------------------------
# Écart maximal toléré entre la source primaire (yfinance, quasi temps réel) et
# le contrôle croisé (FRED, EOD) avant d'émettre une ValidationIssue WARN.
# Valeurs mesurées le 09/11/2026 sur des données réelles : VIX 25,64 (FRED) vs
# 25,64 (yfinance, clôture du 06/11) → 0,00 pt ; US10Y 4,86 (FRED) vs 4,859
# (yfinance ^TNX/10) → 0,001 pt. Les seuils ci-dessous laissent donc une marge
# de ~40x le bruit observé, tout en détectant une vraie divergence de source.
VIX_CROSSCHECK_MAX_POINTS = 1.5      # points d'indice VIX
US10Y_CROSSCHECK_MAX_POINTS = 0.10   # points de pourcentage de rendement
# Nombre d'observations demandées à FRED pour pouvoir ignorer les trous (".")
# et choisir la séance valide juste avant la dernière (P0-2b).
FRED_MIN_OBSERVATIONS = 10

# ----------------------------------------------------------------------------
# P0-7 (audit 09/11/2026) — obsolescence des tables de calendrier officielles
# ----------------------------------------------------------------------------
# Une table de dates de réunion qui couvre moins que cet horizon doit produire
# une ValidationIssue WARN : la carte deviendra [N/A] sans prévenir sinon.
CB_CALENDAR_MIN_HORIZON_DAYS = 90

# ----------------------------------------------------------------------------
# P1-2 (audit 09/11/2026, décision utilisateur) — fenêtre de force des devises
# ----------------------------------------------------------------------------
# Nombre de séances D1 complètes utilisées pour le momentum par devise.
# 5 = une semaine de cotation (décision validée) : lisse un mouvement d'une
# seule séance sans devenir mensuel. La dernière séance reste affichée à part.
STRENGTH_WINDOW_SESSIONS = 5
# Nombre de bougies D1 à demander pour couvrir la fenêtre + la dernière séance.
STRENGTH_CANDLE_COUNT = 12

# ----------------------------------------------------------------------------
# P2 (audit 09/11/2026) — réglages jusqu'ici lus par getattr avec défaut caché
# ----------------------------------------------------------------------------
# Seuil de confiance sous lequel le régime multi-facteurs est requalifié en
# « Mixed / Selective » (regime_engine.assess_regime). Valeur identique au
# défaut qui était codé en dur dans le getattr : zéro changement de
# comportement, la valeur devient simplement visible et modifiable ici.
REGIME_CONFIDENCE_FLOOR = 0.20
# Poids appliqué au vote d'un indicateur de régime adossé à une donnée [PROXY]
# (une saisie manuelle ne doit pas peser autant qu'une lecture live).
REGIME_PROXY_WEIGHT_MULT = 0.5
# Seuil de significativité de la corrélation de Pearson (macro_engine).
CORRELATION_SIGNIFICANCE_MIN = 0.20
# Horizon (heures) au-delà duquel un catalyseur n'est plus étiqueté
# « imminent » dans les libellés de setup (macro_engine).
CATALYST_LABEL_HORIZON_H = 168.0

# ----------------------------------------------------------------------------
# P0-4 (audit 09/11/2026) — taux directeur BoJ
# ----------------------------------------------------------------------------
# Aucune source officielle keyless et machine-readable du taux directeur BoJ
# n'a pu être vérifiée en direct (voir AUDIT_REPORT.md, P0-4) : le flux Forex
# Factory porte « BOJ Policy Rate » les jours de réunion seulement. Hors de ces
# jours, la série FRED IRSTCI01JPM156N est un taux interbancaire MENSUEL OCDE —
# elle est donc affichée en PROXY, jamais utilisée pour dériver un biais, et sa
# jambe est signalée dans le différentiel de taux.
BOJ_RATE_FALLBACK_IS_PROXY = True
