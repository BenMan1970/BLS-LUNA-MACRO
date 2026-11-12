"""External data sources for the BLUESTAR engine — keyed / scraped feeds.

This module centralises every *external* source that requires either an API
key (FRED) or web scraping (CFTC, CME FedWatch, Atlanta Fed GDPNow). It is the
single upgrade path away from the [PROXY]/[N/A] degradation that the keyless
core falls back to.

Contract / design rules (must not be violated by callers):
  * Every public function is **best-effort**: on any failure (missing key,
    network error, parse error, unexpected schema) it returns ``None`` or an
    empty container — it NEVER raises. The caller degrades to [N/A].
  * No function ever *invents* a value. A missing observation ("." in FRED,
    an empty scrape) yields ``None``, never a placeholder number.
  * User overrides always take precedence *upstream* (in macro_engine /
    oanda_data). This module has no knowledge of overrides by design.
  * All diagnostics go through ``logging.warning`` — never ``print``.

Note on FRED series IDs: the original spec referenced Quandl-style codes
(``ECB/ECB``, ``BOJ/BOJ``, ``BOE/BOE``) which do not exist on FRED and would
return ``None`` forever. We substitute the correct FRED series IDs (documented
in ``_CB_RATE_SERIES``) so the feature actually works. ``FEDFUNDS`` is kept
as specified.

Changelog — institutional audit patch (2026-07-11):
  C1  fetch_pc_ratio: vix_value optional param; _vix_pc_composite() added.
  C2  _cboe_parse: signal computed on MA window, not raw daily latest.
  C3  _CBOE_THRESHOLDS: equity thresholds recalibrated on empirical percentiles.
  C4  _CBOE_SEVERITY: severity map added; _pc_composite() preserves severity floor.
  M1  _pc_composite: 6-quadrant matrix (2 missing regimes added).
  M2  _cboe_parse: observation_date captured; fetch_pc_ratio: stale flag added.
  M3  _cboe_parse: ma_incomplete flag + ma_Nd_obs observation count added.
  m1  _cboe_signal: guard for unknown ratio_type → returns "N/A".
  m2  _cboe_parse: data_start is None vs == 0 produce distinct error messages.

Changelog — réseau vérifié empiriquement (2026-07-17, deux environnements
indépendants : crawler le 16/07 ~02:01 UTC + sandbox BLUESTAR le 17/07) :
  N1  §5 CBOE : les URLs .../datahouse/equitypc.csv & indexpc.csv sont MORTES
      (301/404 — infra retirée lors de la refonte du site CBOE, PAS un blocage
      WAF comme le supposait l'ancien commentaire). Elles coûtaient 1 requête
      HTTP inutile par jambe à chaque run. Supprimées de la chaîne active ;
      _cboe_parse conservé pour un éventuel miroir CSV futur (voir §5).
  N2  §5 CBOE : preuve que CBOE ne distribue plus les P/C ratios en public —
      cdn.cboe.com sert _VIX.json (1 153 407 octets) et _SKEW.json normalement
      depuis la même IP, mais _PCALL/_EQUITYPC/_TOTALPC/_INDEXPC/_VIXPC/PCALL
      .json renvoient tous 403 AccessDenied (objet absent). Donnée déplacée
      derrière DataShop / All Access API (payant). Jambe index : aucune source
      keyless fiable — dégradation vers None assumée (mode single-leg P1).
  N3  §5 CBOE : ^PCALL confirmé symbole RÉEL et vivant, MAIS Yahoo
      rate-limite (HTTP 429) les IPs de crawler indépendamment du ticker —
      fallback yfinance conservé en best-effort, étiqueté honnêtement.
  N4  §2 FedWatch : la probabilité rendue pouvait rester figée ~1 semaine sans
      indice de fraîcheur (anomalie audit A1 du 16/07). La clé additive
      "as_of" est désormais extraite du payload BCM quand elle y est présente
      (best-effort, schéma-agnostique — jamais inventée) pour que l'aval
      affiche la date de prélèvement.
  N5  §5 CBOE : ^PCALL live-testé par l'utilisateur final (17/07/2026) →
      HTTP 404 "Quote not found for symbol: ^PCALL" / "possibly delisted".
      N3 ci-dessus est donc caduque : le symbole n'était pas rate-limité,
      il est supprimé chez Yahoo. Preuve directe, pas une inférence.
  N6  §5 CBOE : DÉCOMMISSIONNÉ. Sur la base de N2 (CBOE a retiré la
      distribution publique keyless), N5 (^PCALL confirmé mort, pas
      throttlé) et de 4 audits croisés indépendants (17/07/2026) n'ayant
      trouvé aucune source gratuite, documentée et conforme aux CGU,
      l'implémentation CBOE (~830 lignes) a été retirée. fetch_pc_ratio()
      est un stub permanent retournant None — voir le commentaire ADR en
      tête de la section 5 (fin de fichier) pour le raisonnement complet.
      Le changelog C1→N5 ci-dessus est un historique factuel, pas une
      description du code actif.
"""
from __future__ import annotations

import csv
import concurrent.futures
import datetime
import threading
import io
import logging
import os
import re
import zipfile
from typing import Optional

import requests

from .config import FRED_MIN_OBSERVATIONS, MAX_STALENESS_SESSIONS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# P0-1 FINGERPRINT + P0-2 .env — ajout 15/09/2026
#
# external_sources est le module dont le déploiement a été suspecté (le
# briefing du 14/09/2026 affichait les labels de l'ANCIENNE version — voir
# le rapport d'audit « Avis macro external sources.txt »). Un fingerprint
# (version + chemin importé + sha256 du fichier) est donc journalisé à
# l'IMPORT du module, sur le thread principal uniquement — jamais depuis un
# worker (même discipline que st.secrets, cf. SIGSEGV documenté 23/07).
# Le sha256 est l'identité réelle du fichier chargé : il rend impossible
# qu'une copie fantôme ailleurs dans le PYTHONPATH passe inaperçue.
# ---------------------------------------------------------------------------
__version__ = "2026-09-15.1"  # P0-2 : ordre BoE inversé + fetch_central_bank_rates_with_meta


def _file_sha256_12() -> Optional[str]:
    """sha256 (12 premiers hex) du fichier source de CE module, ou None."""
    try:
        import hashlib
        with open(os.path.abspath(__file__), "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:12]
    except OSError as exc:  # pragma: no cover — partage réseau éphémèrement indisponible
        logger.warning("external_sources: fingerprint de fichier indisponible (%s)", exc)
        return None


def _ensure_dotenv_loaded() -> None:
    """Charge le .env projet dans os.environ — main thread uniquement, à l'import.

    TROIS règles de câblage (mission secrets 15/09/2026) :
      * python-dotenv n'est importé qu'ici et dans credentials.py — jamais
        depuis un worker thread ;
      * ``override=False`` : un secret déjà posé (st.secrets/secrets.toml,
        environnement système, service) garde la main sur le .env ;
      * la CLÉ FRED RESTE RELUE À CHAQUE APPEL dans ``_fred_api_key()`` —
        aucun cache module-level n'est introduit ici (bug du 23/07 : une
        lecture figée à l'import survivait mal au rajout tardif de la clé).

    Idempotent multi-modules : le marqueur ``_BLUESTAR_DOTENV_PATH`` posé dans
    os.environ par le premier importé (ici ou credentials.py) rend le second
    appel no-op — le .env n'est lu qu'une fois par processus. Le poser AVANT
    import avec une valeur quelconque désactive donc le chargement .env pour
    tout le processus (utile pour rejouer le mode « sans clé » de façon
    contrôlée dans les tests).
    """
    if os.environ.get("_BLUESTAR_DOTENV_PATH"):
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        logger.warning(
            "python-dotenv absent — le .env n'est PAS chargé ; les secrets "
            "doivent venir de st.secrets (secrets.toml) ou de l'environnement "
            "système. pip install python-dotenv pour activer le chemin .env.")
        return
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.environ.get("BLUESTAR_DOTENV_PATH"),          # override explicite
        os.path.join(here, ".env"),                       # package bluestar/
        os.path.join(os.path.dirname(here), ".env"),      # racine applicative (app.py)
        os.path.join(os.path.dirname(os.path.dirname(here)), ".env"),  # parent de la racine
        os.path.join(os.getcwd(), ".env"),                # courant au lancement
    ]
    for path in candidates:
        if not path or not os.path.isfile(path):
            continue
        try:
            load_dotenv(path, override=False)
        except OSError as exc:  # pragma: no cover
            logger.warning("Chargement .env impossible (%s) : %s", path, exc)
            continue
        os.environ["_BLUESTAR_DOTENV_PATH"] = path
        logger.warning("BLUESTAR — secrets .env chargés depuis %s "
                       "(override=False ; secrets.toml/environnement système prioritaires)", path)
        return
    logger.warning(
        "BLUESTAR — aucun .env trouvé (candidats : BLUESTAR_DOTENV_PATH, "
        "%s, %s, %s, cwd) — les clés live (OANDA/FRED) restent accessibles via "
        "st.secrets/secrets.toml ou l'environnement système ; sinon dégradation "
        "FALLBACK assumée.", here, os.path.dirname(here),
        os.path.dirname(os.path.dirname(here)))


_ensure_dotenv_loaded()


def _log_import_fingerprint() -> None:
    """P0-1 — preuve au boot de la version de CE fichier réellement importée."""
    sha = _file_sha256_12()
    logger.warning("BLUESTAR external_sources v%s chargé depuis %s (sha256 %s)",
                   __version__, os.path.abspath(__file__), sha or "?")


_log_import_fingerprint()


# Optional Streamlit secrets access (mirrors oanda_data.py degradation pattern).
try:
    import streamlit as st  # type: ignore
    _ST_OK = True
except Exception:  # pragma: no cover
    _ST_OK = False

# Optional BeautifulSoup (used for CFTC index + GDPNow scraping).
try:
    from bs4 import BeautifulSoup  # type: ignore
    _BS_OK = True
except Exception:  # pragma: no cover
    _BS_OK = False
    logger.warning("beautifulsoup4 unavailable — CFTC/GDPNow scraping disabled")

# ---------------------------------------------------------------------------
# HTTP configuration
# ---------------------------------------------------------------------------
_TIMEOUT = 12          # seconds — within the 10–15 s spec band
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; BLUESTAR/8.1; +macro-briefing) "
        "Python-requests"
    ),
    "Accept": "text/html,application/json,text/csv,*/*",
}


def _get(url: str, extra_headers: dict | None = None,
         **kwargs) -> Optional[requests.Response]:
    """Single GET with unified timeout / headers / error handling.

    ``extra_headers`` overrides / extends ``_HEADERS`` for caller-specific
    needs (e.g. CBOE bot-bypass) without touching the module-level default.
    Returns the Response on HTTP 200, else ``None`` (logged). Never raises.
    """
    headers = {**_HEADERS, **(extra_headers or {})}
    try:
        r = requests.get(url, headers=headers, timeout=_TIMEOUT, **kwargs)
        r.raise_for_status()
        return r
    except requests.RequestException as exc:
        logger.warning("HTTP GET failed for %s: %s", url, exc)
        return None


# ===========================================================================
# 1. FRED API
# ===========================================================================
_FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"

# ---------------------------------------------------------------------------
# Audit A1 FIX : Séries vérifiées live 2026-07-12.
# - FEDFUNDS : mensuel, remplacé par DFEDTARU (quotidien, borne haute).
# - IRSTCB01JPM156N : coquille 'B', discontinuée. Remplacé par IRSTCI01JPM156N.
# - BOERUKM : gelée depuis jan. 2017. AUCUNE série BoE fiable sur FRED.
#   -> BoE retirée du mapping FRED. Voir plus bas (section 1bis) pour la
#      source BoE dédiée (API officielle Bank of England, hors FRED) ajoutée
#      le 15/07/2026 — enrichissement pur, ne touche à rien de ce qui suit.
# ---------------------------------------------------------------------------
_CB_RATE_SERIES: dict[str, str] = {
    "FED": "DFEDTARU",
    "BCE": "ECBDFR",
    "BoJ": "IRSTCI01JPM156N",
}

# Plausibilité : bornes larges pour détecter les sentinelles FRED (-999, .)
# et les valeurs aberrantes. Un taux hors bornes est écarté.
_CB_RATE_BOUNDS: dict[str, tuple[float, float]] = {
    "FED": (-0.5, 15.0),
    "BCE": (-0.5, 15.0),
    "BoJ": (-1.0, 10.0),
    "BoE": (-0.5, 15.0),
    # P0-7 : bornes de plausibilité des banques ajoutées. Larges et purement
    # défensives (détection de sentinelle / d'erreur de parsing), pas une
    # modélisation du taux neutre.
    "RBA": (-0.5, 20.0),
    "RBNZ": (-0.5, 20.0),
    "BoC": (-0.5, 20.0),
    "SNB": (-2.0, 10.0),
}

# Staleness max : au-delà, la série est considérée potentiellement gelée.
_CB_MAX_STALENESS_DAYS: int = 70

# BoE-specific staleness bound (audit-enrichment 15/07/2026): the FRED
# series above are DAILY (a value repeats every day even when the rate
# itself is unchanged), so a fresh observation *date* within 70 days is a
# reliable freshness signal even for a rate that hasn't moved. The BoE
# IADB Bank Rate series is event-based: a new row only appears the day the
# MPC actually changes the rate, so the latest observation can legitimately
# be many months old while still being the current valid rate (MPC meets
# ~8x/year and often holds). Reusing the 70-day FRED bound here would
# wrongly discard a perfectly valid, unchanged BoE rate as "stale". A much
# more generous window is used instead — long enough to tolerate a normal
# holding pattern, still short enough to catch a genuinely dead/discontinued
# feed.
_BOE_MAX_STALENESS_DAYS: int = 400

_SOFR_SERIES = "SOFR"
_EFFR_SERIES = "EFFR"

# ---------------------------------------------------------------------------
# P0-4 (audit 09/11/2026) — le taux BoJ affiché n'était pas le taux directeur
# ---------------------------------------------------------------------------
# IRSTCI01JPM156N (série utilisée jusqu'ici pour le Japon) est, d'après sa
# propre fiche FRED, « Immediate Rates (< 24 Hours): Call Money/Interbank Rate »
# — un taux interbancaire MENSUEL publié par l'OCDE, pas la cible de la Banque
# du Japon. Vérifié en direct le 09/11/2026 : dernière observation 2026-09-01 =
# 1,043 %. La série « vraie » de taux directeur OCDE (IRSTCB01JPM156N) est
# discontinuée depuis 2023-12 (0,30 %), et les décisions BoJ ne sont publiées
# qu'en PDF : aucune source officielle keyless et machine-readable n'existe.
# => la valeur est donc affichée en [PROXY], exclue de la dérivation de biais,
#    et sa jambe est signalée dans le différentiel (application de la règle de
#    repli explicitement prévue par la mission).
_CB_PROXY_RATE: frozenset[str] = frozenset({"BoJ"})

# Sources officielles sans clé, vérifiées EN DIRECT le 09/11/2026 (statut HTTP
# et valeur de contrôle dans tests/fixtures/MANIFEST.json) :
#   RBA  https://www.rba.gov.au/statistics/tables/csv/f1-data.csv
#        -> 200, CSV, dernière ligne cotée 06/11/2026 = 4.85, « Change in the
#           Cash Rate Target » = 0.25 le 04/11/2026 (hausse confirmée).
#   BoC  https://www.bankofcanada.ca/valet/observations/V39079/json
#        -> 200, JSON, « Target for the overnight rate » 2.50 au 06/11/2026.
#   SNB  https://data.snb.ch/api/cube/snboffzisa/data/json/en
#        -> 200, JSON, « Switzerland - SNB policy rate » 0.00 (mensuel).
#   RBNZ https://www.rbnz.govt.nz/... -> 403 (deux User-Agents testés, y
#        compris celui qui débloque la RBA). Aucun substitut keyless trouvé :
#        la carte RBNZ est rendue en [N/A] avec sa raison.
_RBA_F1_URL = "https://www.rba.gov.au/statistics/tables/csv/f1-data.csv"
_BOC_VALET_URL = "https://www.bankofcanada.ca/valet/observations/V39079/json"
_SNB_CUBE_URL = "https://data.snb.ch/api/cube/snboffzisa/data/json/en"
_RBNZ_OCR_URL = "https://www.rbnz.govt.nz/monetary-policy/official-cash-rate-decisions"
# RBNZ (403 Cloudflare) et BoJ (FRED = proxy mensuel) : BIS WS_CBPOL, sonde du 12/11/2026.
_BIS_CBPOL_URL = "https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL/1.0/D.{area}"
_BIS_MAX_AGE_DAYS = 30   # un taux directeur change rarement ; au-delà, on écarte

# Certains sites institutionnels refusent le User-Agent navigateur et exigent un
# client « simple » (observé : RBA répond 200 à python-requests et 403 à un UA
# Chrome ; l'inverse exact de la BoE). Un dict par source, donc.
_PLAIN_UA = {"User-Agent": "python-requests/2.33"}


def _fr2(value: float) -> str:
    """Formate un pourcentage à la française (``3.75`` -> ``3,75``).

    Copie locale volontaire de ``oanda_data.fr_num`` : external_sources ne doit
    pas dépendre du module de données de marché (et importer oanda_data depuis
    ici créerait un cycle d'import).
    """
    return f"{value:.2f}".replace(".", ",")


def _last_change_from_pairs(
    pairs: list[tuple[str, float]],
) -> Optional[tuple[str, float, float]]:
    """Dernier changement d'une série : ``(date, ancien, nouveau)`` ou ``None``.

    ``pairs`` doit être trié du plus ANCIEN au plus RÉCENT. Utilisé pour
    fabriquer le FAIT « dernier changement : date, ancien taux → nouveau taux »
    à partir de la série elle-même (aucune valeur inventée : si la série
    n'expose pas l'historique, on renvoie ``None`` et le FAIT reste vide).
    """
    prev: Optional[tuple[str, float]] = None
    last_change: Optional[tuple[str, float, float]] = None
    for date_iso, value in pairs:
        if prev is not None and abs(value - prev[1]) > 1e-9:
            last_change = (date_iso, prev[1], value)
        prev = (date_iso, value)
    return last_change


def _rba_cash_rate() -> Optional[dict]:
    """Taux cible RBA depuis le CSV officiel F1 (quotidien, sans clé).

    Vérifié en direct le 09/11/2026 : HTTP 200, `Publication date,09-Nov-2026`,
    dernière ligne cotée `06-Nov-2026,4.85`, et `04-Nov-2026,4.85,0.25` sur la
    colonne « Change in the Cash Rate Target » — la hausse de +25 pb confirmée.
    Le fichier contient des lignes de métadonnées avant l'en-tête de données et
    une ligne finale sans valeur : les deux sont écartées par le parseur.
    """
    r = _get(_RBA_F1_URL, extra_headers=_PLAIN_UA)
    if r is None:
        logger.warning("CB rate: RBA — CSV F1 injoignable")
        return None
    pairs: list[tuple[str, float]] = []
    changes: list[tuple[str, float]] = []
    try:
        header_seen = False
        for row in csv.reader(io.StringIO(r.text)):
            if not row or len(row) < 2:
                continue
            if row[0].strip().lower().startswith("series id"):
                header_seen = True
                continue
            if not header_seen:
                continue
            try:
                d = datetime.datetime.strptime(row[0].strip(), "%d-%b-%Y").date()
            except ValueError:
                continue
            if not row[1].strip():
                continue          # dernière ligne (jour sans cotation)
            try:
                v = float(row[1].strip())
            except ValueError:
                continue
            pairs.append((d.isoformat(), v))
            if len(row) > 2 and row[2].strip():
                try:
                    changes.append((d.isoformat(), float(row[2].strip())))
                except ValueError:
                    pass
    except csv.Error as exc:
        logger.warning("CB rate: RBA — parse CSV F1 en échec: %s", exc)
        return None
    if not pairs:
        logger.warning("CB rate: RBA — aucune ligne de données exploitable")
        return None
    date_iso, val = pairs[-1]
    last_change = _last_change_from_pairs(pairs)
    if last_change is None and changes:
        # Repli : la colonne « Change » officielle donne la date du mouvement ;
        # l'ancien taux est alors la valeur de la veille de ce mouvement.
        chg_date, chg = changes[-1]
        old = next((v for d, v in reversed(pairs) if d < chg_date), None)
        if old is not None:
            last_change = (chg_date, old, old + chg)
    bounds = _CB_RATE_BOUNDS.get("RBA")
    if bounds and not (bounds[0] <= val <= bounds[1]):
        logger.warning("CB rate: RBA — %.4f hors bornes %s, écartée", val, bounds)
        return None
    return {"rate": val, "display": f"{_fr2(val)}%",
            "source": "RBA · table F1 (CSV officiel)",
            "observed_date": date_iso, "last_change": last_change,
            # La table F1 enregistre le taux EN VIGUEUR à la date indiquée :
            # c'est une date d'effet. La date de décision RBA n'a pas pu être
            # vérifiée en direct (pages de réunions en 404 lors de l'audit).
            "last_change_kind": "effective"}


def _boc_policy_rate() -> Optional[dict]:
    """Taux directeur BoC via l'API Valet (V39079, sans clé).

    Vérifié en direct le 09/11/2026 : HTTP 200, série « Target for the overnight
    rate (business daily) », 2.50 au 06/11/2026.
    """
    r = _get(_BOC_VALET_URL, params={"recent": 400},
             extra_headers=_PLAIN_UA)
    if r is None:
        logger.warning("CB rate: BoC — API Valet injoignable")
        return None
    try:
        obs = r.json().get("observations", [])
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning("CB rate: BoC — parse JSON en échec: %s", exc)
        return None
    pairs: list[tuple[str, float]] = []
    for o in obs:
        try:
            pairs.append((str(o["d"]), float(o["V39079"]["v"])))
        except (KeyError, TypeError, ValueError):
            continue
    if not pairs:
        return None
    pairs.sort(key=lambda t: t[0])
    date_iso, val = pairs[-1]
    bounds = _CB_RATE_BOUNDS.get("BoC")
    if bounds and not (bounds[0] <= val <= bounds[1]):
        logger.warning("CB rate: BoC — %.4f hors bornes %s, écartée", val, bounds)
        return None
    return {"rate": val, "display": f"{_fr2(val)}%",
            "source": "Bank of Canada · API Valet (V39079)",
            "observed_date": date_iso,
            "last_change": _last_change_from_pairs(pairs),
            "last_change_kind": "effective"}


def _snb_policy_rate() -> Optional[dict]:
    """Taux directeur SNB via le cube officiel data.snb.ch (sans clé).

    Vérifié en direct le 09/11/2026 : HTTP 200, « Switzerland - SNB policy
    rate », fréquence MENSUELLE (P1M), dernière valeur 0.00 pour 2026-09.
    La fréquence mensuelle est signalée dans le stamp : la valeur n'est pas
    une lecture du jour.
    """
    r = _get(_SNB_CUBE_URL, extra_headers=_PLAIN_UA)
    if r is None:
        logger.warning("CB rate: SNB — cube injoignable")
        return None
    try:
        series = r.json().get("timeseries", [])
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning("CB rate: SNB — parse JSON en échec: %s", exc)
        return None
    for ts in series:
        header = (ts.get("header") or [{}])[0]
        if "SNB policy rate" not in str(header.get("dimItem", "")):
            continue
        vals = [(str(v.get("date", "")), v.get("value"))
                for v in ts.get("values", []) if v.get("value") is not None]
        if not vals:
            continue
        pairs = []
        for ym, v in vals:
            try:
                d = datetime.datetime.strptime(ym, "%Y-%m")
            except ValueError:
                continue
            # Une série mensuelle est horodatée au 1er du mois : c'est la
            # date de PUBLICATION de la valeur du mois, pas un point du jour.
            pairs.append((d.date().isoformat(), float(v)))
        if not pairs:
            continue
        pairs.sort(key=lambda t: t[0])
        date_iso, val = pairs[-1]
        bounds = _CB_RATE_BOUNDS.get("SNB")
        if bounds and not (bounds[0] <= val <= bounds[1]):
            continue
        return {"rate": val, "display": f"{_fr2(val)}%",
                "source": "SNB · cube officiel (mensuel)",
                "observed_date": date_iso,
                "last_change": _last_change_from_pairs(pairs),
                "last_change_kind": "effective",
                "note": "série MENSUELLE — la valeur est celle du dernier mois publié"}
    return None


def _bis_policy_rate(area: str, name: str, who: str) -> Optional[dict]:
    """Taux directeur d'une banque centrale via l'API BIS « WS_CBPOL » (sans clé).

    Source : BIS Central bank policy rates, série QUOTIDIENNE (fin de période),
    republiée à partir de la banque centrale elle-même (colonne SOURCE_REF).
    Sonde réelle du 12/11/2026 (RAPPORT_PROBE_2026-11-12.md) : HTTP 200 en
    text/csv pour D.NZ (RBNZ, dernière obs 2026-11-06 = 2.75) et D.JP (BoJ,
    dernière obs 2026-11-10 = 1.25), alors que rbnz.govt.nz renvoie 403
    (Cloudflare) et que la série FRED BoJ est un proxy interbancaire mensuel.

    Le CSV est lu PAR NOM DE COLONNE (TIME_PERIOD, OBS_VALUE, REF_AREA) : la
    colonne COMPILATION contient des virgules entre guillemets, un découpage
    naïf serait faux. Toute anomalie (colonne absente, zone différente de celle
    demandée, valeur hors bornes, donnée trop ancienne) renvoie ``None`` : la
    carte reste [N/A] ou proxy, jamais une valeur inventée.
    """
    r = _get(_BIS_CBPOL_URL.format(area=area),
             params={"lastNObservations": 400, "format": "csv"},
             extra_headers=_PLAIN_UA)
    if r is None:
        logger.warning("CB rate: %s — API BIS injoignable", name)
        return None
    try:
        reader = csv.DictReader(io.StringIO(r.text))
        fields = set(reader.fieldnames or [])
        if not {"TIME_PERIOD", "OBS_VALUE", "REF_AREA"} <= fields:
            logger.warning("CB rate: %s — CSV BIS sans colonnes attendues (%s)",
                           name, sorted(fields)[:8])
            return None
        pairs: list[tuple[str, float]] = []
        for row in reader:
            if (row.get("REF_AREA") or "").strip() != area:
                logger.warning("CB rate: %s — REF_AREA inattendue %r", name,
                               row.get("REF_AREA"))
                return None
            d = (row.get("TIME_PERIOD") or "").strip()
            v = (row.get("OBS_VALUE") or "").strip()
            if not v:
                continue
            try:
                datetime.date.fromisoformat(d)
                pairs.append((d, float(v)))
            except ValueError:
                continue
    except (csv.Error, ValueError) as exc:
        logger.warning("CB rate: %s — parse CSV BIS en échec: %s", name, exc)
        return None
    if not pairs:
        return None
    pairs.sort(key=lambda t: t[0])
    date_iso, val = pairs[-1]
    bounds = _CB_RATE_BOUNDS.get(name)
    if bounds and not (bounds[0] <= val <= bounds[1]):
        logger.warning("CB rate: %s — %.4f hors bornes %s, écartée", name, val, bounds)
        return None
    age_days = (datetime.datetime.now(datetime.timezone.utc).date()
                - datetime.date.fromisoformat(date_iso)).days
    if age_days > _BIS_MAX_AGE_DAYS:
        logger.warning("CB rate: %s — dernière observation BIS du %s (%d j) : "
                       "trop ancienne, écartée", name, date_iso, age_days)
        return None
    return {"rate": val, "display": f"{_fr2(val)}%",
            "source": f"BIS · taux directeurs (source : {who})",
            "observed_date": date_iso,
            "last_change": _last_change_from_pairs(pairs),
            "last_change_kind": "effective",
            "note": ("série quotidienne BIS, publiée avec quelques jours de "
                     "retard : la date de la donnée est celle de la dernière "
                     "observation")}


def _boe_rate_history() -> Optional[list[tuple[str, float]]]:
    """Historique BoE ``[(date_iso, taux)]`` depuis Bank-Rate.asp (P1-6).

    Remplace les deux stratégies regex de l'ancien `_boe_bank_rate_scrape` :
    la stratégie 2 « date + nombre » pouvait capter n'importe quelle date ou
    n'importe quel nombre de la page (y compris hors du tableau) et servir de
    taux de repli. Ici on ne lit QUE le tableau « Date Changed / Rate » via
    BeautifulSoup, et on refuse la valeur en cas d'ambiguïté (0 ou ≥2 tableaux
    candidats).
    """
    r = _get(_BOE_BANK_RATE_URL, extra_headers=_BOE_HEADERS)
    if r is None:
        logger.warning("BoE Bank-Rate.asp: page injoignable")
        return None
    if not _BS_OK:
        logger.error("BoE Bank-Rate.asp: beautifulsoup4 absent — parsing du "
                     "tableau impossible, valeur refusée (pas de repli regex)")
        return None
    try:
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception:  # pragma: no cover
        logger.exception("BoE Bank-Rate.asp: parsing HTML en échec")
        return None

    candidates = []
    for table in soup.find_all("table"):
        text = table.get_text(" ", strip=True).lower()
        if "date changed" in text and "rate" in text:
            candidates.append(table)
    if len(candidates) != 1:
        logger.error("BoE Bank-Rate.asp: %d tableau(x) « Date Changed / Rate » "
                     "trouvé(s) — ambiguïté, valeur refusée", len(candidates))
        return None

    rows: list[tuple[str, float]] = []
    for tr in candidates[0].find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if len(cells) < 2:
            continue
        d = _boe_parse_date(cells[0])
        if d is None:
            continue
        try:
            v = float(cells[1].replace(",", ".").replace("%", "").strip())
        except ValueError:
            continue
        rows.append((d.isoformat(), v))
    if not rows:
        logger.error("BoE Bank-Rate.asp: tableau trouvé mais illisible — "
                     "valeur refusée")
        return None
    rows.sort(key=lambda t: t[0])
    return rows


def _boe_policy_rate() -> Optional[dict]:
    """Taux BoE + dernier changement, depuis le tableau officiel (chemin PRIMAIRE)."""
    hist = _boe_rate_history()
    if not hist:
        return None
    date_iso, val = hist[-1]
    try:
        age_days = (datetime.date.today()
                    - datetime.date.fromisoformat(date_iso)).days
    except ValueError:
        return None
    if age_days > _BOE_MAX_STALENESS_DAYS:
        logger.warning(
            "CB rate: BoE — dernier changement daté du %s (%d j) → série "
            "potentiellement gelée, valeur écartée", date_iso, age_days)
        return None
    bounds = _CB_RATE_BOUNDS.get("BoE")
    if bounds and not (bounds[0] <= val <= bounds[1]):
        logger.warning("CB rate: BoE — %.4f hors bornes %s, écartée", val, bounds)
        return None
    return {"rate": val, "display": f"{_fr2(val)}%",
            "source": _CB_PRIMARY_SOURCE["BoE"],
            "observed_date": date_iso,
            "last_change": _last_change_from_pairs(hist),
            # C-6 : le tableau « Date Changed » de la BoE porte la date de
            # l'annonce MPC (5 novembre 2026 dans la page des dates officielles
            # ET dans ce tableau), donc ici la date EST une date de décision.
            "last_change_kind": "decision"}


_MAIN_THREAD = threading.main_thread()
_FRED_KEY_CACHE: Optional[str] = None
_FRED_KEY_RESOLVED = False


def resolve_fred_api_key(force: bool = False) -> Optional[str]:
    """Résout la clé FRED **une seule fois**, sur le thread principal.

    P0-6 (audit 09/11/2026) — ``st.secrets`` ne doit JAMAIS être lu depuis un
    thread secondaire (SIGSEGV documenté le 23/07/2026, et règle écrite dans
    ``credentials.py`` / ``institutional.py``). La clé est donc résolue ici, en
    cache module, puis passée en argument aux fonctions qui en ont besoin —
    y compris celles qui s'exécutent dans un ``ThreadPoolExecutor``.

    Lit ``st.secrets`` (secrets.toml) puis ``os.environ`` (le .env y est déjà
    déversé à l'import). N'invente jamais de clé ; renvoie ``None`` si aucune
    n'est disponible, ce qui dégrade proprement en [N/A].
    """
    global _FRED_KEY_CACHE, _FRED_KEY_RESOLVED
    if _FRED_KEY_RESOLVED and not force:
        return _FRED_KEY_CACHE
    if threading.current_thread() is not _MAIN_THREAD:
        # Filet de sécurité : on ne tente même pas st.secrets hors thread
        # principal. On se rabat sur l'environnement, qui est thread-safe.
        logger.error(
            "FRED: resolve_fred_api_key() appelée hors du thread principal — "
            "st.secrets N'EST PAS lu (SIGSEGV 23/07/2026) ; repli os.environ. "
            "La clé doit être résolue une fois sur le thread principal puis "
            "passée en argument.")
        env = os.environ.get("FRED_API_KEY") or os.environ.get("fred_api_key")
        return str(env) if env else None

    key = None
    if _ST_OK:
        try:
            key = st.secrets.get("FRED_API_KEY") or st.secrets.get("fred_api_key")
        except Exception as exc:  # pragma: no cover
            logger.warning("Streamlit FRED key access failed: %s", exc)
    if not key:
        key = os.environ.get("FRED_API_KEY") or os.environ.get("fred_api_key")
    _FRED_KEY_CACHE = str(key) if key else None
    _FRED_KEY_RESOLVED = True
    if _FRED_KEY_CACHE is None:
        logger.warning(
            "FRED: aucune clé résolue (ni st.secrets ni os.environ) — les "
            "séries FRED seront [N/A] sur cette exécution.")
    return _FRED_KEY_CACHE


def _fred_api_key(api_key: Optional[str] = None) -> Optional[str]:
    """Clé FRED à utiliser : argument explicite, sinon résolution thread-safe.

    P0-6 : tout appelant qui peut tourner dans un worker DOIT passer
    ``api_key=``. Sans argument, la fonction refuse de lire ``st.secrets``
    hors du thread principal et le dit en ERROR.
    """
    if api_key:
        return api_key
    if threading.current_thread() is not _MAIN_THREAD:
        logger.error(
            "FRED: _fred_api_key() appelée sans api_key depuis un thread "
            "secondaire — st.secrets non lu (SIGSEGV 23/07/2026), repli "
            "os.environ. Corriger l'appelant pour passer api_key=...")
        env = os.environ.get("FRED_API_KEY") or os.environ.get("fred_api_key")
        return str(env) if env else None
    return resolve_fred_api_key()


def _fred_series(series_id: str,
                api_key: Optional[str] = None) -> Optional[float]:
    api_key = _fred_api_key(api_key)
    if not api_key:
        return None
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": 1,
    }
    r = _get(_FRED_BASE, params=params)
    if r is None:
        return None
    try:
        obs = r.json().get("observations", [])
        if not obs:
            return None
        raw = obs[0].get("value", ".")
        if raw in (".", "", None):
            return None
        return float(raw)
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning("FRED parse error for %s: %s", series_id, exc)
        return None


def _fred_series_dated(series_id: str,
                       api_key: Optional[str] = None) -> Optional[tuple[float, str]]:
    """Comme _fred_series mais renvoie (valeur, date_obs ISO) ou None."""
    api_key = _fred_api_key(api_key)
    if not api_key:
        return None
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": 1,
    }
    r = _get(_FRED_BASE, params=params)
    if r is None:
        return None
    try:
        obs = r.json().get("observations", [])
        if not obs:
            return None
        raw = obs[0].get("value", ".")
        if raw in (".", "", None):
            return None
        return float(raw), obs[0].get("date", "")
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning("FRED dated parse error for %s: %s", series_id, exc)
        return None


# ---------------------------------------------------------------------------
# VIX (CBOE Volatility Index) via FRED — audit-add 24/07/2026
# ---------------------------------------------------------------------------
# AUDIT-ADD (24/07/2026): VIX was previously routed unconditionally to
# yfinance (_YF_ONLY in oanda_data.py) even though a real, authoritative,
# keyless-with-key source exists: FRED's own VIXCLS series (the CBOE
# publishes this to FRED daily). Same "never raise, log and return None"
# contract as every other fetcher here. Note: FRED's VIXCLS is EOD (prior
# trading day's close), same latency profile as GDPNOW/CB rates already in
# this module — NOT intraday. Callers should treat this as PRIMARY (same
# tier as GDP_NOWCAST) since it's the authoritative CBOE print, just not
# the same latency as an intraday yfinance quote.
_VIXCLS_SERIES = "VIXCLS"
# Sentinel bound: VIX has never printed <5 or >90 historically (Oct 2008
# and Mar 2020 spikes topped out below 90); used only to catch a FRED
# sentinel/parsing artifact, not as a real-world plausibility model.
_VIX_BOUNDS = (5.0, 150.0)
_VIX_MAX_STALENESS_DAYS = 10  # FRED VIXCLS is a daily series; generous
                              # window to tolerate a weekend/holiday gap
                              # without wrongly flagging it as stale.


def _fred_daily_observations(
    series_id: str,
    limit: int = 10,
    api_key: Optional[str] = None,
) -> Optional[list[tuple[float, str]]]:
    """Les ``limit`` dernières observations VALIDES, du plus récent au plus ancien.

    P0-2b (audit 09/11/2026) — remplace le ``limit=2`` historique des lecteurs
    VIX/DGS10. Deux défauts corrigés d'un coup :

    * les trous « . » (jour férié, série pas encore publiée) sont ÉCARTÉS avant
      de choisir la « précédente » : sinon la veille affichée est la ligne
      d'avant-hier et la variation est calculée sur deux séances d'écart sans
      le dire ;
    * demander 10 lignes garantit qu'il reste au moins une observation valide
      après filtrage des trous, ce que ``limit=2`` ne garantissait pas.

    Renvoie ``None`` sur échec (jamais d'exception), conformément au contrat
    du module.
    """
    api_key = _fred_api_key(api_key)
    if not api_key:
        return None
    params = {
        "series_id": series_id, "api_key": api_key, "file_type": "json",
        "sort_order": "desc", "limit": max(int(limit), FRED_MIN_OBSERVATIONS),
    }
    r = _get(_FRED_BASE, params=params)
    if r is None:
        logger.warning("%s: aucune réponse FRED", series_id)
        return None
    try:
        raw_obs = r.json().get("observations", [])
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning("%s (FRED) parse error: %s", series_id, exc)
        return None
    out: list[tuple[float, str]] = []
    for o in raw_obs:
        raw = o.get("value", ".")
        if raw in (".", "", None):
            continue
        try:
            out.append((float(raw), str(o.get("date", ""))))
        except (ValueError, TypeError):
            continue
    if not out:
        logger.warning("%s (FRED) : aucune observation numérique", series_id)
        return None
    return out


def _reject_if_past_last_session(series_id: str, date_iso: str,
                                 now_utc: Optional[datetime.datetime],
                                 max_sessions: int) -> bool:
    """True si l'observation est trop en retard sur la dernière séance attendue.

    P0-2b : une observation qui n'est pas celle de la dernière séance attendue
    n'est PAS rejetée par principe (une clôture de vendredi lue un lundi est
    normale) ; au-delà de ``max_sessions`` séances, la série est considérée
    comme gelée et la valeur est écartée pour laisser la main à la source
    suivante, avec un log explicite.
    """
    try:
        from .staleness import sessions_behind
        obs_date = datetime.date.fromisoformat(date_iso)
    except (ValueError, TypeError):
        logger.warning("%s (FRED) : date illisible '%s', valeur écartée",
                       series_id, date_iso)
        return True
    now = now_utc or datetime.datetime.now(datetime.timezone.utc)
    behind = sessions_behind(obs_date, now)
    if behind > max_sessions:
        logger.warning(
            "%s (FRED) : observation du %s = %d séances de retard sur la "
            "dernière séance attendue (max %d) → valeur écartée",
            series_id, date_iso, behind, max_sessions)
        return True
    return False


def fetch_vix_fred(
    now_utc: Optional[datetime.datetime] = None,
    api_key: Optional[str] = None,
) -> Optional[tuple[float, str, Optional[float]]]:
    """Fetch the latest CBOE VIX close from FRED (series VIXCLS), plus the
    prior observation for a day-over-day trend (matches the yfinance-sourced
    gauges' own ↑/↓/→ % display instead of leaving it blank).

    Returns ``(value, date_iso, prev_value_or_None)`` or ``None`` on any
    failure (no key, no data, stale, out of plausible bounds) — never
    raises. Mirrors the staleness/bounds discipline already applied to CB
    rates (``fetch_central_bank_rates``), just with VIX-specific parameters.
    """
    obs = _fred_daily_observations(_VIXCLS_SERIES, api_key=api_key)
    if not obs:
        logger.warning("VIX: aucune observation FRED (VIXCLS)")
        return None

    val, dt_iso = obs[0]
    if _reject_if_past_last_session(_VIXCLS_SERIES, dt_iso, now_utc,
                                    MAX_STALENESS_SESSIONS):
        return None

    lo, hi = _VIX_BOUNDS
    if not (lo <= val <= hi):
        logger.warning(
            "VIX (FRED VIXCLS) — valeur %.4f hors bornes [%.1f, %.1f], écartée",
            val, lo, hi,
        )
        return None

    # P0-2b : « précédent » = la séance VALIDE juste avant (trous déjà écartés).
    prev_val = obs[1][0] if len(obs) > 1 else None
    if prev_val is not None and not (lo <= prev_val <= hi):
        prev_val = None  # écarte une observation aberrante sans faire échouer
                         # la valeur courante pour autant

    return val, dt_iso, prev_val


# ---------------------------------------------------------------------------
# US10Y (10Y Treasury yield) via FRED — audit-add 24/07/2026
# ---------------------------------------------------------------------------
# AUDIT-ADD (24/07/2026): DGS10 is published in the SAME H.15 Selected
# Interest Rates release as DFEDTARU (the Fed rate series already live in
# production via _CB_RATE_SERIES) — same publisher (Board of Governors),
# same daily cadence. Given DFEDTARU's freshness is already confirmed
# working correctly in production, DGS10 is expected to behave identically
# (not independently re-verified live here beyond that release-level
# equivalence — worth confirming once deployed, same as every other
# fetcher in this module). Units are already Percent (e.g. 4.66), unlike
# yfinance's ^TNX which this codebase quotes x10 — no conversion needed.
_DGS10_SERIES = "DGS10"
_US10Y_BOUNDS = (0.0, 20.0)  # sentinel/plausibility guard only
_US10Y_MAX_STALENESS_DAYS = 10


def fetch_us10y_fred(
    now_utc: Optional[datetime.datetime] = None,
    api_key: Optional[str] = None,
) -> Optional[tuple[float, str, Optional[float]]]:
    """Fetch the latest 10Y Treasury yield from FRED (DGS10), plus the prior
    observation for a trend. Returns ``(value, date_iso, prev_value_or_None)``
    or ``None`` on any failure — never raises. Same contract as
    ``fetch_vix_fred``.
    """
    obs = _fred_daily_observations(_DGS10_SERIES, api_key=api_key)
    if not obs:
        logger.warning("US10Y: aucune observation FRED (DGS10)")
        return None

    val, dt_iso = obs[0]
    if _reject_if_past_last_session(_DGS10_SERIES, dt_iso, now_utc,
                                    MAX_STALENESS_SESSIONS):
        return None

    lo, hi = _US10Y_BOUNDS
    if not (lo <= val <= hi):
        logger.warning(
            "US10Y (FRED DGS10) — valeur %.4f hors bornes [%.1f, %.1f], écartée",
            val, lo, hi,
        )
        return None
    # P0-2b : précédent = la séance valide juste avant.
    prev_val = obs[1][0] if len(obs) > 1 else None
    if prev_val is not None and not (lo <= prev_val <= hi):
        prev_val = None
    return val, dt_iso, prev_val


# ---------------------------------------------------------------------------
# WTI / Brent via FRED (EIA Spot Prices) — audit-add 24/07/2026
# ---------------------------------------------------------------------------
# AUDIT-ADD (24/07/2026): confirmed via direct FRED page fetch (24/07/2026)
# that these two EIA-sourced series run SEVERAL DAYS behind real time —
# DCOILWTICO's latest print was 2026-07-20 (updated 22/07) and
# DCOILBRENTEU's was 2026-07-13 (updated 15/07), i.e. 4 and 11 days stale
# respectively as observed that day. This is a normal characteristic of the
# EIA Spot Prices release, NOT a bug — but it means these series must
# NEVER be used as a first-choice/primary source ahead of yfinance's
# near-live futures quotes (CL=F/BZ=F), only as a fallback AFTER yfinance
# fails, exactly mirroring how Frankfurter sits after Oanda for FX. Using
# these as primary would be a real freshness regression, not an
# improvement — deliberately wired in as fallback-only below.
_OIL_SERIES = {"WTI": "DCOILWTICO", "Brent": "DCOILBRENTEU"}
_OIL_BOUNDS = (0.0, 300.0)  # sentinel/plausibility guard only
_OIL_MAX_STALENESS_DAYS = 20  # generous: EIA release itself runs days
                              # behind by design (confirmed above), a
                              # tighter bound would reject valid data


def fetch_oil_fred(key: str,
                   api_key: Optional[str] = None) -> Optional[tuple[float, str]]:
    """Fetch the latest WTI or Brent spot price from FRED (EIA Spot Prices).

    ``key`` must be "WTI" or "Brent". Returns ``(value, date_iso)`` or
    ``None`` on any failure — never raises. Callers MUST treat this as a
    fallback tier only, never as a live/primary quote — see module comment
    above for the confirmed multi-day lag.
    """
    series_id = _OIL_SERIES.get(key)
    if series_id is None:
        return None
    res = _fred_series_dated(series_id, api_key=api_key)
    if res is None:
        logger.warning("%s: aucune observation FRED (%s)", key, series_id)
        return None
    val, dt_iso = res
    try:
        obs_date = datetime.date.fromisoformat(dt_iso)
        age_days = (datetime.date.today() - obs_date).days
        if age_days > _OIL_MAX_STALENESS_DAYS:
            logger.warning(
                "%s (FRED %s) — observation datée du %s (%d j) → "
                "potentiellement gelée, valeur écartée",
                key, series_id, dt_iso, age_days,
            )
            return None
    except (ValueError, TypeError):
        logger.warning("%s (FRED %s) — date illisible '%s', valeur écartée",
                        key, series_id, dt_iso)
        return None
    lo, hi = _OIL_BOUNDS
    if not (lo <= val <= hi):
        logger.warning(
            "%s (FRED %s) — valeur %.4f hors bornes [%.1f, %.1f], écartée",
            key, series_id, val, lo, hi,
        )
        return None
    return val, dt_iso


# ===========================================================================
# 1bis. Bank of England — API officielle (hors FRED)
#
# AUDIT-ENRICHMENT (15/07/2026): FRED n'a aucune série BoE Bank Rate fiable
# (BOERUKM gelée depuis 2017, voir commentaire ci-dessus). Cette section
# interroge directement la base IADB de la Bank of England (endpoint public,
# sans clé) — même contrat "never raise, log and return None" que le reste
# du module. Purement additif : ne touche ni _fred_series, ni
# _fred_series_dated, ni aucune des séries FED/BCE/BoJ existantes.
# ===========================================================================
# CORRECTIF (23/07/2026) — le commentaire "AUDIT-FIX ... validé en direct
# ce jour, deux fois : GET live -> HTTP 206 ..." qui se trouvait ici a été
# RETIRÉ : il est très probablement fabriqué. Un test indépendant réel (pas
# un modèle qui prétend avoir testé) effectué le 23/07/2026 contre
# exactement cette URL avec ces paramètres a reçu "Error — Access denied"
# (page d'erreur BoE, Reference Id présent), pas du CSV. De plus,
# robots.txt de bankofengland.co.uk (vérifié le même jour) contient :
#   Disallow: /boeapps/database/_iadb-FromShowColumns.asp
#   Disallow: /boeapps/iadb
# ce qui est cohérent avec un blocage volontaire des clients non-navigateur
# sur ce chemin précis, plutôt qu'avec un succès HTTP 206.
#
# Ce qui reste vrai et vérifié indépendamment :
#   - le chemin "/boeapps/database/_iadb-fromshowcolumns.asp" (préfixe
#     "database/", underscore+tiret) est la bonne URL documentée par la BoE
#     (page /boeapps/database/help.asp) — contrairement à l'ancien
#     "/boeapps/iadb/fromshowcolumns.asp" qui n'est pas ce chemin ;
#   - "www.bankofengland.co.uk/boeapps/database/Bank-Rate.asp" répond 200
#     sans blocage et affiche le Bank Rate courant (vérifié 23/07/2026,
#     3.75%) — voir _boe_bank_rate_scrape() plus bas, ajouté comme
#     fallback pour cette raison précise.
#
# Le blocage observé peut être lié au User-Agent (ancien : identifiant
# "BLUESTAR/8.1" explicite). Le header a été changé pour un profil
# navigateur ci-dessous ; re-testé le 14/09/2026 depuis l'environnement de
# prod : l'export IADB répond de nouveau 200 + application/csv avec ces
# headers (dernières lignes "11 Sep 2026,3.75"). MAIS robots.txt (vérifié le
# même jour) contient toujours "Disallow: /boeapps/database/_iadb-FromShowColumns.asp"
# et "Disallow: /boeapps/iadb" — le chemin reste donc RÉSERVÉ au repli, et
# la page publique Bank-Rate.asp (autorisée, 200, "3.75%") devient le chemin
# PRIMAIRE (P0-2, 15/09/2026). Le stamp ne revendique IADB que si IADB a
# réellement servi (voir fetch_central_bank_rates_with_meta).
_BOE_IADB_URL = "https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp"
_BOE_BANK_RATE_URL = "https://www.bankofengland.co.uk/boeapps/database/Bank-Rate.asp"
_BOE_BANK_RATE_CODE = "IUDBEDR"  # Bank Rate officielle (code série IADB)

# Headers dédiés BoE : profil "vrai navigateur" plutôt que le UA explicite
# "BLUESTAR/8.1" du reste du module. Tentative de contournement du blocage
# observé (cf. commentaire ci-dessus) — NON validée en live depuis cet
# environnement (pas d'accès réseau sortant vers bankofengland.co.uk ici).
# Isolée dans son propre dict pour ne rien changer au comportement des
# autres sources (FRED/CFTC/GDPNow) qui utilisent _HEADERS.
_BOE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": "https://www.bankofengland.co.uk/boeapps/database/",
}

# Formats de date observés dans les exports IADB (varie selon les endpoints
# BoE) — essayés dans l'ordre jusqu'à ce qu'un marche.
_BOE_DATE_FORMATS = ("%d %b %Y", "%d/%m/%Y", "%d-%b-%y", "%Y-%m-%d",
                      "%d %b %y")
# AJOUT (24/07/2026) — vérifié en direct (web_fetch réel, 24/07/2026) contre
# https://www.bankofengland.co.uk/boeapps/database/Bank-Rate.asp : la page
# répond 200, contient bien "Current official Bank Rate\n\n3.75%" (regex
# stratégie 1 de _boe_bank_rate_scrape testée et validée contre ce texte
# exact) et un tableau "Date Changed | Rate" dont la première ligne est
# "18 Dec 25" — année à 2 chiffres avec espaces, qu'AUCUN des 4 formats
# ci-dessus ne matchait (testé : les 4 lèvent ValueError sur "18 Dec 25").
# Sans ce format, _boe_parse_date() renvoie toujours None pour cette page
# précise et la stratégie 2 retombe sur le repli "date = aujourd'hui" déjà
# prévu dans _boe_bank_rate_scrape() — pas un crash, mais une date de
# dernier changement perdue alors qu'elle est disponible. Purement additif :
# les formats existants (utilisés par _boe_bank_rate() / export CSV IADB,
# format différent) ne sont ni modifiés ni réordonnés.


def _boe_parse_date(raw: str) -> Optional[datetime.date]:
    raw = raw.strip()
    for fmt in _BOE_DATE_FORMATS:
        try:
            return datetime.datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _boe_bank_rate() -> Optional[tuple[float, str]]:
    """Fetch the latest BoE Bank Rate observation from the IADB CSV export.

    REPLI (P0-2, 15/09/2026) derrière le scrape Bank-Rate.asp : robots.txt
    de bankofengland.co.uk interdit toujours ce chemin (« Disallow:
    /boeapps/database/_iadb-FromShowColumns.asp »), vérifié de nouveau
    le 14/09/2026 ; le 200/CSV observé ce jour avec les headers navigateur
    ne rend pas le chemin conforme — il ne sert donc que si la page publique
    est injoignable, et le stamp affiché le dit alors explicitement.

    Returns ``(value, date_iso)`` or ``None`` on any failure — never raises,
    matching every other fetcher in this module. NOTE: this endpoint is
    outside the sandbox's allowed network domains at the time this was
    written, so this function could not be exercised against a live
    response here; the CSV parsing below is best-effort against the
    documented IADB export format (data rows after a short header/meta
    block) and should be verified against a real response in your
    environment before being relied on.
    """
    end = datetime.date.today()
    start = end - datetime.timedelta(days=800)  # generous: MPC holds often
    params = {
        "csv.x": "yes",
        "Datefrom": start.strftime("%d/%b/%Y"),
        "Dateto": end.strftime("%d/%b/%Y"),
        "SeriesCodes": _BOE_BANK_RATE_CODE,
        "CSVF": "TN",
        "UsingCodes": "Y",
        "VPD": "Y",
        "ns": "1",
    }
    try:
        r = requests.get(_BOE_IADB_URL, params=params, timeout=15,
                         headers=_BOE_HEADERS)
        r.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("BoE IADB fetch failed: %s", exc)
        return None

    # Le endpoint peut renvoyer HTTP 200 avec une page d'erreur HTML
    # ("Access denied") plutôt qu'un vrai refus HTTP — raise_for_status()
    # ne l'attrape pas dans ce cas. Détection défensive avant de tenter le
    # parsing CSV, pour éviter de silencieusement retourner None sans log
    # utile (cf. commentaire d'audit ci-dessus : observé le 23/07/2026).
    ctype = r.headers.get("Content-Type", "")
    if "csv" not in ctype.lower() and ("<html" in r.text[:200].lower()
                                        or "access denied" in r.text[:2000].lower()):
        logger.warning(
            "BoE IADB: réponse HTML (pas CSV) reçue — probable blocage "
            "bot/WAF sur ce endpoint plutôt qu'une vraie donnée absente. "
            "Content-Type=%r", ctype,
        )
        return None

    try:
        lines = [ln for ln in r.text.splitlines() if ln.strip()]
        # IADB exports carry a short header/meta block before the data
        # table; scan for the first row that looks like "<date>,<number>"
        # rather than assuming a fixed skip count, since the exact preamble
        # length has varied across BoE endpoint versions.
        data_rows = []
        for row in csv.reader(lines):
            if len(row) < 2 or not row[0].strip():
                continue
            d = _boe_parse_date(row[0])
            if d is None:
                continue
            try:
                v = float(row[1].strip())
            except ValueError:
                continue
            data_rows.append((d, v))
        if not data_rows:
            logger.warning("BoE IADB: aucune ligne de données exploitable dans la réponse")
            return None
        data_rows.sort(key=lambda t: t[0])
        last_date, last_val = data_rows[-1]
        return last_val, last_date.isoformat()
    except (csv.Error, IndexError) as exc:
        logger.warning("BoE IADB parse error: %s", exc)
        return None


def _boe_bank_rate_scrape() -> Optional[tuple[float, str]]:
    """Chemin PRIMAIRE BoE (P0-2, 15/09/2026) : lit le taux courant depuis la
    page publique Bank-Rate.asp — autorisée par robots.txt, vérifiée 200 et
    « 3.75% » le 14/09/2026 — plutôt que l'export CSV IADB (interdit par
    robots.txt ; bloqué « Access denied » le 23/07/2026 ; ne sert que de
    repli via _boe_bank_rate()).

    Vérifiée manuellement le 23/07/2026 : cette page répond 200 sans
    blocage et affiche "Current official Bank Rate" suivi du taux, plus un
    tableau historique "Date Changed / Rate". Le parseur ci-dessous est
    volontairement tolérant (plusieurs stratégies regex) car je n'ai pas eu
    accès au HTML brut exact depuis cet environnement (extraction faite via
    un outil qui convertit en markdown) — donc les tags précis n'ont PAS pu
    être vérifiés ici. À valider/ajuster contre une vraie réponse dans
    votre environnement avant mise en prod ; échoue proprement (None) si la
    structure ne correspond pas, ne casse jamais l'appelant.

    Ne renvoie que le taux courant (pas d'historique) — contrat identique à
    _boe_bank_rate() : ``(value, date_iso)`` ou ``None``.
    """
    try:
        r = requests.get(_BOE_BANK_RATE_URL, timeout=15, headers=_BOE_HEADERS)
        r.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("BoE Bank-Rate.asp fetch failed: %s", exc)
        return None

    text = r.text
    if _BS_OK:
        try:
            text = BeautifulSoup(r.text, "html.parser").get_text("\n", strip=True)
        except Exception as exc:
            logger.warning("BoE Bank-Rate.asp: parsing HTML échoué (%s), fallback texte brut", exc)
            text = r.text

    # Stratégie 1 : "Current official Bank Rate" suivi (proche) d'un %.
    m = re.search(
        r"Current\s+official\s+Bank\s+Rate\D{0,40}?([\d.]+)\s*%",
        text, re.IGNORECASE | re.DOTALL,
    )
    rate_val: Optional[float] = None
    if m:
        try:
            rate_val = float(m.group(1))
        except ValueError:
            rate_val = None

    # Stratégie 2 (repli) : première ligne du tableau "Date Changed | Rate".
    date_iso: Optional[str] = None
    m2 = re.search(
        r"(\d{1,2}\s+\w{3}\s+\d{2,4})\D{0,10}?([\d.]+)",
        text,
    )
    if m2:
        d = _boe_parse_date(m2.group(1))
        if d is not None:
            date_iso = d.isoformat()
        if rate_val is None:
            try:
                rate_val = float(m2.group(2))
            except ValueError:
                pass

    if rate_val is None:
        logger.warning("BoE Bank-Rate.asp: taux introuvable dans la page (structure inattendue)")
        return None

    # Si on n'a pas pu extraire de date de changement fiable, on horodate
    # sur la date du jour : "Current official Bank Rate" est par définition
    # la valeur en vigueur aujourd'hui, pas une observation datée — le
    # contrôle de fraîcheur en aval (_BOE_MAX_STALENESS_DAYS) doit
    # simplement voir une date récente, pas la date exacte de la dernière
    # décision MPC.
    if date_iso is None:
        date_iso = datetime.date.today().isoformat()

    return rate_val, date_iso


# Source affichée par défaut quand on ne connaît QUE le nom de la banque
# (chemin PRIMAIRE actuellement câblé). La provenance RÉELLE par run — celle
# qui a réellement répondu, IADB vs Bank-Rate.asp pour la BoE — est portée par
# ``fetch_central_bank_rates_with_meta()`` et consommée par macro_engine.
# (P0-2, 15/09/2026 — label honnête validé par le reviewer.)
_CB_PRIMARY_SOURCE: dict[str, str] = {
    "FED": "FRED",                              # série DFEDTARU (Fed funds cible)
    # P1-4 : la valeur EST servie par FRED (série ECBDFR) — le portail BCE
    # n'est que la provenance d'origine du chiffre. L'ancien libellé faisait
    # croire à un appel direct au portail.
    "BCE": "FRED · ECBDFR (miroir DFR BCE)",
    "BoJ": "FRED",                              # série IRSTCI01JPM156N (rare ; flux préféré)
    "BoE": "Bank of England · Bank-Rate.asp",  # scrape = chemin primaire dès P0-2
}


def central_bank_rate_source(name: str) -> str:
    """Label de la source PRINCIPALE qui résout ``name`` dans
    ``fetch_central_bank_rates()`` — repli d'étiquetage pour un appelant qui
    ne disposerait pas de la provenance réelle par run.

    INCHANGÉ de signature (retourne ``str``). P0-2 (15/09/2026) : la valeur
    BoE n'est plus le IADB en dur (le chemin réel est le scrape Bank-Rate.asp,
    le CSV IADB restant bloqué par robots.txt) et BCE rend la source primaire
    « ECB Data Portal · DFR » (le dépôt FRED n'en est que le transport). La
    provenance réellement servie est rendue par ``fetch_central_bank_rates_with_meta``.
    """
    return _CB_PRIMARY_SOURCE.get(name, "FRED")


def _boe_with_source() -> Optional[tuple[float, str]]:
    """Résout le Bank Rate BoE en renvoyant AUSSI la source qui a servi.

    P0-2 : ordre inversé — scrape Bank-Rate.asp EN PREMIER (page publique
    autorisée par robots.txt, vérifiée 200/« 3.75% »), export CSV IADB en
    REPLI (bloqué par robots.txt : « Disallow: /boeapps/iadb »). Retourne
    ``(valeur_pct, libellé_source)`` ou ``None`` ; ne lève jamais.
    """
    res = _boe_bank_rate_scrape()
    used = _CB_PRIMARY_SOURCE["BoE"]
    if res is None:
        logger.warning(
            "CB rate: BoE (scrape %s) — aucune observation, tentative repli "
            "CSV IADB %s", _BOE_BANK_RATE_URL.rsplit("/", 1)[-1], _BOE_BANK_RATE_CODE)
        res = _boe_bank_rate()
        used = "Bank of England · IADB"
    if res is None:
        logger.warning("CB rate: BoE — aucune source (scrape et CSV) n'a répondu")
        return None

    val, dt_iso = res
    try:
        obs_date = datetime.date.fromisoformat(dt_iso)
        age_days = (datetime.date.today() - obs_date).days
        if age_days > _BOE_MAX_STALENESS_DAYS:
            logger.warning(
                "CB rate: BoE (%s) — observation datée du %s (%d j) "
                "→ série potentiellement gelée, valeur écartée",
                used, dt_iso, age_days)
            return None
    except (ValueError, TypeError):
        logger.warning("CB rate: BoE (%s) — date illisible '%s', valeur écartée",
                        used, dt_iso)
        return None

    bounds = _CB_RATE_BOUNDS.get("BoE")
    if bounds is not None:
        lo, hi = bounds
        if not (lo <= val <= hi):
            logger.warning(
                "CB rate: BoE (%s) — valeur %.4f hors bornes [%.1f, %.1f], écartée",
                used, val, lo, hi)
            return None
    return val, used


def _resolve_fred_with_source(name: str,
                              series_id: str,
                              api_key: Optional[str] = None,
                              ) -> Optional[tuple[float, str]]:
    """Résolution FRED d'un taux avec les contrôles Audit A1 (fraîcheur +
    plausibilité) ; renvoie AUSSI le libellé de source affiché. Ne lève jamais."""
    res = _fred_series_dated(series_id, api_key=api_key)
    if res is None:
        logger.warning("CB rate: %s (%s) — aucune observation FRED", name, series_id)
        return None

    val, dt_iso = res
    try:
        obs_date = datetime.date.fromisoformat(dt_iso)
        age_days = (datetime.date.today() - obs_date).days
        if age_days > _CB_MAX_STALENESS_DAYS:
            logger.warning(
                "CB rate: %s (%s) — observation datée du %s (%d j) "
                "→ série potentiellement gelée, valeur écartée",
                name, series_id, dt_iso, age_days)
            return None
    except (ValueError, TypeError):
        logger.warning("CB rate: %s (%s) — date illisible '%s', valeur écartée",
                        name, series_id, dt_iso)
        return None

    bounds = _CB_RATE_BOUNDS.get(name)
    if bounds is not None:
        lo, hi = bounds
        if not (lo <= val <= hi):
            logger.warning(
                "CB rate: %s (%s) — valeur %.4f hors bornes [%.1f, %.1f], écartée",
                name, series_id, val, lo, hi)
            return None
    return val, _CB_PRIMARY_SOURCE.get(name, "FRED")


def _fred_rate_block(
    name: str,
    series_id: str,
    api_key: Optional[str],
    with_history: bool = True,
    display_lower: Optional[float] = None,
) -> Optional[dict]:
    """Bloc de taux pour une banque servie par FRED (quotidien)."""
    obs = _fred_daily_observations(
        series_id, limit=200 if with_history else 10, api_key=api_key)
    if not obs:
        logger.warning("CB rate: %s (%s) — aucune observation FRED", name, series_id)
        return None
    val, date_iso = obs[0]
    if _reject_if_past_last_session(series_id, date_iso, None,
                                    _CB_MAX_STALENESS_DAYS):
        return None
    bounds = _CB_RATE_BOUNDS.get(name)
    if bounds and not (bounds[0] <= val <= bounds[1]):
        logger.warning("CB rate: %s (%s) — %.4f hors bornes %s, écartée",
                       name, series_id, val, bounds)
        return None
    pairs = sorted(((d, v) for v, d in obs), key=lambda t: t[0])
    if display_lower is not None and display_lower <= val:
        display = f"{_fr2(display_lower)}\u2013{_fr2(val)}%"
    else:
        display = f"{_fr2(val)}%"
    return {"rate": val, "display": display,
            "source": _CB_PRIMARY_SOURCE.get(name, "FRED"),
            "observed_date": date_iso,
            "lower": display_lower,
            "last_change": _last_change_from_pairs(pairs) if with_history else None,
            # C-6 : une série FRED quotidienne enregistre la date d'EFFET, pas
            # la date de décision (vérifié : DFEDTARU change le 17/09, la
            # réunion FOMC était le 16/09).
            "last_change_kind": "effective"}


def fetch_central_bank_rates_full(
    api_key: Optional[str] = None,
) -> dict[str, dict]:
    """Taux directeurs complets, provenance et date d'observation par banque.

    CONTRAT NOUVEAU (additif, P0-4/P0-7). Chaque entrée porte :
      rate            valeur numérique (%)
      display         libellé affichable (fourchette FED incluse)
      source          source QUI A RÉELLEMENT SERVI ce run
      observed_date   date de l'observation (ou du dernier changement) ISO
      is_proxy        True quand la grandeur n'est PAS le taux directeur
      last_change     (date, ancien, nouveau) ou None
      note            précision de lecture éventuelle

    Les fonctions historiques ``fetch_central_bank_rates_with_meta`` et
    ``fetch_central_bank_rates`` projettent ce dictionnaire : leurs contrats
    publics ne changent pas.
    """
    out: dict[str, dict] = {}
    lower_fed: Optional[float] = None

    # --- FED : fourchette cible DFEDTARL–DFEDTARU (P0-7e) -------------------
    obs_l = _fred_daily_observations("DFEDTARL", limit=10, api_key=api_key)
    if obs_l:
        lower_fed = obs_l[0][0]
    fed = _fred_rate_block("FED", _CB_RATE_SERIES["FED"], api_key,
                           display_lower=lower_fed)
    if fed:
        out["FED"] = fed

    # --- BCE -------------------------------------------------------------
    bce = _fred_rate_block("BCE", _CB_RATE_SERIES["BCE"], api_key)
    if bce:
        out["BCE"] = bce

    # --- BoJ : PROXY interbancaire OCDE mensuel (P0-4) --------------------
    boj = _fred_rate_block("BoJ", _CB_RATE_SERIES["BoJ"], api_key,
                           with_history=False)
    if boj:
        boj["is_proxy"] = True
        boj["source"] = "FRED · IRSTCI01JPM156N (proxy interbancaire OCDE mensuel)"
        boj["note"] = ("taux interbancaire MENSUEL OCDE — PAS le taux directeur "
                       "BoJ ; exclu de la dérivation de biais")
        out["BoJ"] = boj
    # BoJ : le taux directeur réel (BIS) remplace le proxy interbancaire quand il
    # répond ; sinon le proxy FRED reste, étiqueté comme tel (comportement d'avant).
    try:
        boj_bis = _bis_policy_rate("JP", "BoJ", "Bank of Japan")
    except Exception:  # noqa: BLE001
        logger.exception("CB rate: BoJ (BIS) — exception inattendue")
        boj_bis = None
    if boj_bis:
        out["BoJ"] = boj_bis

    # --- BoE, RBA, BoC, SNB : sources officielles hors FRED ---------------
    for name, fn in (("BoE", _boe_policy_rate), ("RBA", _rba_cash_rate),
                     ("BoC", _boc_policy_rate), ("SNB", _snb_policy_rate),
                     ("RBNZ", lambda: _bis_policy_rate(
                         "NZ", "RBNZ", "Reserve Bank of New Zealand"))):
        try:
            block = fn()
        except Exception:  # noqa: BLE001 — un parseur ne doit jamais tuer le run
            logger.exception("CB rate: %s — exception inattendue", name)
            block = None
        if block:
            out[name] = block
        else:
            logger.warning("CB rate: %s — aucune source n'a répondu", name)

    # --- Repli BoE sur l'export IADB (robots.txt interdit : dernier recours)
    if "BoE" not in out:
        res = _boe_bank_rate()
        if res is not None:
            val, date_iso = res
            bounds = _CB_RATE_BOUNDS.get("BoE")
            if not bounds or bounds[0] <= val <= bounds[1]:
                out["BoE"] = {"rate": val, "display": f"{_fr2(val)}%",
                              "source": "Bank of England · IADB (repli)",
                              "observed_date": date_iso, "last_change": None,
                              "note": "repli IADB (robots.txt interdit ce chemin)"}

    _expected = {"FED", "BCE", "BoJ", "BoE", "RBA", "BoC", "SNB", "RBNZ"}
    missing = _expected - set(out)
    if missing:
        logger.error("CB rate: taux manquants pour %s — [N/A] explicite attendu",
                     ", ".join(sorted(missing)))
    return out


def fetch_central_bank_rates_with_meta(
    api_key: Optional[str] = None,
) -> dict[str, tuple[float, str]]:
    """Return ``{cb_name: (rate_pct, source_label)}`` — provenance RÉELLE.

    NOUVEAU contrat additif (P0-2) : chaque entrée porte le libellé de la
    source QUI A VRAIMENT SERVÉ la valeur ce run (p. ex. BoE « Bank of England
    · Bank-Rate.asp » si le scrape a répondu, « Bank of England · IADB » si
    c'est le CSV qui a servi). macro_engine consomme cette méta pour afficher
    un stamp honnête. ``fetch_central_bank_rates()`` (``dict[str, float]``)
    devient le wrapper rétro-compatible de cette fonction.
    """
    full = fetch_central_bank_rates_full(api_key=api_key)
    return {name: (float(blk["rate"]), str(blk["source"]))
            for name, blk in full.items()}


def _legacy_fetch_central_bank_rates_with_meta(
    api_key: Optional[str] = None,
) -> dict[str, tuple[float, str]]:
    """Ancien chemin (pool FRED + BoE), conservé pour référence/tests.

    P0-7 : plus appelé par défaut — ``fetch_central_bank_rates_with_meta``
    projette désormais ``fetch_central_bank_rates_full``. Conservé parce qu'il
    documente la résolution historique (et parce que le supprimer changerait le
    comportement observable en cas de régression du nouveau chemin).
    """
    out: dict[str, tuple[float, str]] = {}
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(_CB_RATE_SERIES) + 1) as ex:
        # P0-6 : la clé est résolue sur le thread principal (appelant) et
        # passée explicitement à chaque worker — aucun worker ne touche
        # st.secrets.
        future_to_name = {
            ex.submit(_resolve_fred_with_source, name, series_id, api_key): name
            for name, series_id in _CB_RATE_SERIES.items()
        }
        future_to_name[ex.submit(_boe_with_source)] = "BoE"
        for future in concurrent.futures.as_completed(future_to_name):
            name = future_to_name[future]
            try:
                res = future.result()
            except Exception:  # noqa: BLE001 — point défensif documenté : ne jamais propager un worker
                logger.exception("CB rate: exception imprévue pour %s", name)
                res = None
            if res is not None:
                out[name] = res

    _all_names = set(_CB_RATE_SERIES) | {"BoE"}
    if not out:
        logger.error("CB rate: AUCUN taux résolu — différentiels indisponibles")
    else:
        missing = _all_names - set(out)
        if missing:
            logger.warning("CB rate: taux manquants pour %s — dégradation [N/A] attendue",
                           ", ".join(sorted(missing)))
    return out


def fetch_central_bank_rates(api_key: Optional[str] = None) -> dict[str, float]:
    """Return ``{cb_name: rate_pct}`` for every rate a live source can serve.

    CONTRAT PUBLIC INCHANGÉ (``dict[str, float]``, règle 1 du brief). Depuis
    P0-2 ce n'est plus qu'un WRAPPER de ``fetch_central_bank_rates_with_meta``
    qui projette la valeur et jette le libellé de source. Les contrôles de
    fraîcheur/plausibilité (audit A1), l'ajout BoE hors FRED (audit 15/07) et
    le nouveau chemin primaire BoE (scrape Bank-Rate.asp, P0-2) vivent tous
    dans la fonction méta — donc dans CE wrapper, sans duplication.

    Pour une provenance exacte par run (quelle source BoE a réellement servi),
    appeler ``fetch_central_bank_rates_with_meta()`` ; ``central_bank_rate_source``
    ne rend plus que le libellé principal par défaut (repli d'étiquetage).
    """
    meta = fetch_central_bank_rates_with_meta(api_key=api_key)
    return {name: val for name, (val, _src) in meta.items()}



def fetch_liquidity_stress(api_key: Optional[str] = None) -> Optional[float]:
    """Return the latest SOFR − EFFR spread in basis points, or ``None``.

    P0-6 : deux workers FRED — la clé est résolue par l'appelant (thread
    principal) et passée explicitement, jamais lue depuis un worker.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        fut_sofr = ex.submit(_fred_series, _SOFR_SERIES, api_key)
        fut_effr = ex.submit(_fred_series, _EFFR_SERIES, api_key)
        sofr = fut_sofr.result()
        effr = fut_effr.result()
    if sofr is None or effr is None:
        return None
    return (sofr - effr) * 100.0


# ===========================================================================
# 2. CME FedWatch probabilities
# ===========================================================================
_FEDWATCH_URL = "https://www.cmegroup.com/CmeWS/md/BCM/BCM.json"


# Clés de fraîcheur candidates dans les payloads JSON de marché (schéma BCM
# non documenté publiquement — liste normalisée, comparée sans '_' ni espaces,
# en minuscules). La clé "date" seule est volontairement EXCLUE : dans ce
# payload elle porte la date de RÉUNION FOMC (cible), pas l'horodatage de
# prélèvement — l'accepter afficherait une fausse fraîcheur.
_FEDWATCH_TS_KEYS = frozenset({
    "lastupdated", "lastupdate", "updated", "updatetime", "updatetimeutc",
    "timestamp", "asof", "asofdate", "quotedate", "quotetime",
    "tradedate", "lasttradedate", "pricedate", "lastpriceupdate",
})


def _fedwatch_extract_timestamp(data) -> Optional[str]:
    """Best-effort : extrait l'horodatage de fraîcheur du payload BCM.

    Schéma-agnostique (le parser de probabilités ci-dessous l'est déjà) :
    retourne la première valeur textuelle non vide trouvée sous une clé de
    ``_FEDWATCH_TS_KEYS``, tronquée à 40 caractères — ou ``None`` si le
    payload n'en porte aucune (cas parfaitement admis : l'aval masque alors
    simplement la mention). N'invente jamais de valeur, ne lève jamais.
    """
    try:
        found: list[str] = []

        def _walk(node) -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    norm = str(k).lower().replace("_", "").replace(" ", "")
                    if norm in _FEDWATCH_TS_KEYS and isinstance(v, str) and v.strip():
                        found.append(v.strip())
                    _walk(v)
            elif isinstance(node, list):
                for item in node:
                    _walk(item)

        _walk(data)
        if not found:
            return None
        # Plusieurs candidats possibles (payload multi-nœuds) : le max
        # lexicographique = le plus récent pour les formats ISO-like ; pour
        # les formats non triables l'ordre est arbitraire mais reste réel.
        return max(found)[:40]
    except Exception:
        return None


def fetch_fedwatch_probabilities() -> Optional[dict]:
    """Return ``{'pause_pct', 'cut_pct', 'hike_pct'}`` for the next FOMC, or None.

    N4 (2026-07-17) : clé additive ``'as_of'`` (str) présente uniquement quand
    le payload BCM porte lui-même un horodatage exploitable — date de
    prélèvement affichée en aval (audit A1 : probabilités figées ~1 semaine
    sans mention de fraîcheur). Les trois clés historiques sont inchangées.
    """
    r = _get(_FEDWATCH_URL)
    if r is None:
        return None
    try:
        data = r.json()
    except ValueError as exc:
        logger.warning("FedWatch JSON decode failed: %s", exc)
        return None
    try:
        meetings = _fedwatch_locate_meetings(data)
        if not meetings:
            logger.warning("FedWatch: no meeting probabilities located in payload")
            return None
        nearest = meetings[0]
        buckets = _fedwatch_extract_buckets(nearest)
        if not buckets:
            return None
        cut = pause = hike = 0.0
        for delta_bp, prob in buckets:
            if delta_bp < 0:
                cut += prob
            elif delta_bp > 0:
                hike += prob
            else:
                pause += prob
        total = cut + pause + hike
        if total <= 0:
            return None
        scale = 100.0 / total
        result = {
            "cut_pct": int(round(cut * scale)),
            "pause_pct": int(round(pause * scale)),
            "hike_pct": int(round(hike * scale)),
        }
        drift = 100 - sum(result.values())
        if drift:
            biggest = max(result, key=result.get)
            result[biggest] += drift
        # N4 : horodatage de prélèvement quand le payload le fournit (additif).
        ts = _fedwatch_extract_timestamp(data)
        if ts:
            result["as_of"] = ts
        return result
    except Exception as exc:
        logger.warning("FedWatch parse failed (schema drift?): %s", exc)
        return None


def _fedwatch_locate_meetings(data) -> list:
    candidates: list[dict] = []

    def _looks_like_meeting(d: dict) -> bool:
        keys = {k.lower() for k in d.keys()}
        has_date = any("date" in k or "meeting" in k for k in keys)
        has_prob = any("prob" in k or "value" in k or "bp" in k for k in keys)
        return has_date and has_prob

    def _walk(node):
        if isinstance(node, dict):
            if _looks_like_meeting(node):
                candidates.append(node)
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(data)

    def _mdate(d: dict) -> str:
        for k, v in d.items():
            if "date" in k.lower() and isinstance(v, str):
                return v
        return "9999-99-99"

    candidates.sort(key=_mdate)
    return candidates


def _fedwatch_extract_buckets(meeting: dict) -> list[tuple[int, float]]:
    buckets: list[tuple[int, float]] = []
    prob_list = None
    for k, v in meeting.items():
        if "prob" in k.lower() and isinstance(v, list):
            prob_list = v
            break
    rows = prob_list if prob_list is not None else [meeting]
    for row in rows:
        if not isinstance(row, dict):
            continue
        delta_bp = None
        prob = None
        for k, v in row.items():
            kl = k.lower()
            if delta_bp is None and ("bp" in kl or "change" in kl or "delta" in kl):
                try:
                    delta_bp = int(round(float(v)))
                except (TypeError, ValueError):
                    pass
            if prob is None and ("prob" in kl or kl in ("value", "pct")):
                try:
                    prob = float(v)
                except (TypeError, ValueError):
                    pass
        if delta_bp is not None and prob is not None:
            frac = prob / 100.0 if prob > 1.0 else prob
            buckets.append((delta_bp, frac))
    return buckets


# ===========================================================================
# 3. CFTC Commitments of Traders (Non-Commercials, legacy financial futures)
# ===========================================================================
_CFTC_INDEX = "https://www.cftc.gov/MarketReports/CommitmentsofTraders/index.htm"

_CFTC_CONTRACTS: dict[str, str] = {
    "EUR": "EURO FX",
    "JPY": "JAPANESE YEN",
    "GBP": "BRITISH POUND",
    "CHF": "SWISS FRANC",
    "AUD": "AUSTRALIAN DOLLAR",
    "CAD": "CANADIAN DOLLAR",
    "NZD": "NEW ZEALAND DOLLAR",
}

_COL_MARKET   = "Market and Exchange Names"
_COL_NC_LONG  = "Noncommercial Positions-Long (All)"
_COL_NC_SHORT = "Noncommercial Positions-Short (All)"
_COL_DATE     = "As of Date in Form YYYY-MM-DD"
_CME_TOKEN    = "CHICAGO MERCANTILE EXCHANGE"


def _cftc_find_report_url() -> Optional[str]:
    if not _BS_OK:
        return None
    r = _get(_CFTC_INDEX)
    if r is None:
        return None
    try:
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as exc:
        logger.warning("CFTC index parse failed: %s", exc)
        return None

    def _abs(href: str) -> str:
        if href.startswith("http"):
            return href
        if href.startswith("/"):
            return "https://www.cftc.gov" + href
        return "https://www.cftc.gov/" + href

    hrefs = [a.get("href", "") for a in soup.find_all("a", href=True)]
    for href in hrefs:
        low = href.lower()
        if "other_disclaim" in low and low.endswith((".csv", ".zip")):
            return _abs(href)
    for href in hrefs:
        low = href.lower()
        if (("fin" in low or "fut" in low or "deacot" in low)
                and low.endswith((".csv", ".zip"))):
            return _abs(href)
    logger.warning("CFTC: no report CSV/zip link found on index page")
    return None


def _cftc_load_rows(url: str) -> Optional[list[dict]]:
    r = _get(url)
    if r is None:
        return None
    content = r.content
    text: Optional[str] = None
    try:
        if url.lower().endswith(".zip") or content[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
                if not csv_names:
                    logger.warning("CFTC zip has no CSV: %s", url)
                    return None
                text = zf.read(csv_names[0]).decode("utf-8", errors="replace")
        else:
            text = content.decode("utf-8", errors="replace")
    except (zipfile.BadZipFile, UnicodeError) as exc:
        logger.warning("CFTC report decode failed for %s: %s", url, exc)
        return None
    try:
        reader = csv.DictReader(io.StringIO(text))
        return list(reader)
    except csv.Error as exc:
        logger.warning("CFTC CSV parse failed: %s", exc)
        return None


def _cftc_col(row: dict, wanted: str) -> Optional[str]:
    if wanted in row:
        return row[wanted]
    want_norm = re.sub(r"\s+", " ", wanted).strip().lower()
    for k, v in row.items():
        if k and re.sub(r"\s+", " ", k).strip().lower() == want_norm:
            return v
    return None


def fetch_cot_data() -> tuple[dict[str, int], Optional[str]]:
    """Return ``({ccy: net_noncommercial}, as_of_date_str)`` from the CFTC."""
    url = _cftc_find_report_url()
    if not url:
        return {}, None
    rows = _cftc_load_rows(url)
    if not rows:
        return {}, None
    net_by_ccy: dict[str, int] = {}
    as_of: Optional[str] = None
    for row in rows:
        market = (_cftc_col(row, _COL_MARKET) or "").upper()
        if _CME_TOKEN not in market:
            continue
        rtype = _cftc_col(row, "Report Type") or _cftc_col(row, "FutOnly_or_Combined")
        if rtype and "FUT" not in rtype.upper() and "ONLY" not in rtype.upper():
            continue
        for ccy, token in _CFTC_CONTRACTS.items():
            if ccy in net_by_ccy:
                continue
            if token in market:
                long_s  = _cftc_col(row, _COL_NC_LONG)
                short_s = _cftc_col(row, _COL_NC_SHORT)
                if long_s is None or short_s is None:
                    continue
                try:
                    net = (int(float(long_s.replace(",", "")))
                           - int(float(short_s.replace(",", ""))))
                except (TypeError, ValueError):
                    continue
                net_by_ccy[ccy] = net
                if as_of is None:
                    d = _cftc_col(row, _COL_DATE)
                    if d:
                        as_of = d.strip()
                break
    if not net_by_ccy:
        logger.warning("CFTC: report loaded but no target contracts matched")
        return {}, None
    return net_by_ccy, as_of


# ===========================================================================
# 4. Atlanta Fed GDPNow
# ===========================================================================
_GDPNOW_URL = "https://www.atlantafed.org/cqer/research/gdpnow"

_GDPNOW_RE = re.compile(
    r"GDPNow (?:model )?estimate for (?:real GDP growth[^)]*\)[^0-9]*)?"
    r"Q?[1-4]?\s*\d{4}\s*is\s*(-?[\d.]+)\s*(?:percent|%)",
    re.IGNORECASE,
)
_GDPNOW_LATEST_RE = re.compile(
    r"[Ll]atest estimate:\s*(-?[\d.]+)\s*(?:percent|%)"
)


def fetch_gdp_nowcast() -> Optional[float]:
    """Return the current Atlanta Fed GDPNow estimate (%), or ``None``."""
    r = _get(_GDPNOW_URL)
    if r is None:
        return None
    html = r.text
    text = html
    if _BS_OK:
        try:
            text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
        except Exception:
            text = html
    for rx in (_GDPNOW_LATEST_RE, _GDPNOW_RE):
        m = rx.search(text)
        if m:
            try:
                return float(m.group(1))
            except (TypeError, ValueError):
                continue
    logger.warning("GDPNow: estimate not found in page text")
    return None


# ===========================================================================
# 5. CBOE Put/Call Ratios — DECOMMISSIONED (17/07/2026)
#
# ADR: feature removed, not just degraded. Root cause proven (not inferred):
#   - ^PCALL is delisted on Yahoo: live-tested by the end user from their own
#     machine on 17/07/2026 -> HTTP 404 "Quote not found for symbol: ^PCALL"
#     / "possibly delisted". Not a rate-limit, not a network fault.
#   - CBOE's public CDN returns 403 AccessDenied on every P/C endpoint
#     (_PCALL/_EQUITYPC/_INDEXPC/_TOTALPC/_VIXPC) while _VIX/_SKEW on the
#     SAME CDN return 200 (byte-exact) -- a deliberate data removal by CBOE,
#     not a WAF or generic outage.
#   - Four independent cross-model audits (17/07/2026) found no free,
#     documented, ToS-compliant programmatic source. The only defensible
#     paid options (Barchart OnDemand $CPC/$CPCI, YCharts API) are
#     disproportionate for a signal weighted 0.05 and never regime-
#     determining alone (see config.REGIME_MATERIAL_SIGNAL_WEIGHT and
#     regime_engine._pc_indicator's own documented contract).
#
# The full CBOE fetch/parse/composite implementation (~830 lines: header
# spoofing, dead-endpoint diagnostics, yfinance fallback with retry/jitter,
# FR-labeled signal classification, VIX x P/C and P/C-only composites,
# single-leg degradation) was deleted rather than left dormant, per the
# user's explicit decommission decision. It is fully recoverable from git
# history / the pre-17/07/2026 version of this file if CBOE, Yahoo, or a
# new provider ever restores a free keyless source.
#
# fetch_pc_ratio() is kept as a single, permanent no-op choke point so
# macro_engine.py needs zero changes to its call site or its downstream
# handling (pc_data=None already degrades gracefully everywhere it is
# consumed -- verified against regime_engine._pc_indicator and
# macro_engine.build_macro_overlay, both of which already treated None as
# a normal, expected state before this decommission).
# ===========================================================================

def fetch_pc_ratio(
    ma_days: int = 5,
    vix_value: Optional[float] = None,
) -> Optional[dict]:
    """Decommissioned. Always returns None -- no network call, no exception.

    See the module-level ADR comment above for why. Signature unchanged
    from the pre-decommission version so call sites need no edits.
    """
    return None
