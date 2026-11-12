"""End-to-end pipeline glue: data -> engine -> render -> validate.

Kept free of Streamlit so it can be driven from the UI, a cron job, or tests.

v9.0 changes:
  * Integrates staleness/coverage report (audit C2/C3/C5).
  * Integrates regime engine (Priority 4).
  * Integrates interpretation layer (Priority 3).
  * Blocks publication on ERROR-level validation issues.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import pytz

from .calendar_layer import build_calendar
from . import config as _C
from .config import TZ_CET
from .macro_engine import build_context
from .oanda_data import build_market_snapshot
from .models import BriefingContext, ValidationIssue
from .renderer import render_html
from .validation import validate_context, validate_html

logger = logging.getLogger(__name__)


def generate_briefing(
    now_utc: Optional[datetime] = None,
    overrides: Optional[dict] = None,
    mode: str = "Normal",
    allow_proxy_levels: bool = True,
    raw_calendar: Optional[list] = None,
    market_snapshot=None,
) -> tuple[str, BriefingContext, list[ValidationIssue]]:
    """Run the full pipeline.

    Returns ``(html, context, issues)``. ``raw_calendar`` / ``market_snapshot``
    can be injected for tests (offline / deterministic).
    """
    now_utc = now_utc or datetime.now(pytz.UTC)
    # B-2 : même fuseau que l'en-tête/pied de page (config.TZ_CET), sinon un
    # événement de 06:00 UTC s'affiche 07:00 en section 2 et 08:00 en en-tête
    # pendant l'heure d'été.
    calendar = build_calendar(now_utc=now_utc, raw_data=raw_calendar,
                              display_timezone=TZ_CET.key)
    market = market_snapshot or build_market_snapshot(
        now_utc=now_utc,
        overrides=(overrides or {}).get("market"),
        allow_proxy_levels=allow_proxy_levels,
    )
    ctx = build_context(now_utc, market, calendar, overrides, mode, allow_proxy_levels)
    context_issues = validate_context(ctx)
    # P0-2/P0-3 : on AJOUTE les constats du validateur à ceux déjà collectés
    # par build_context (écart de contrôle croisé, couche indisponible…).
    # Écraser ici supprimait purement et simplement ces constats.
    ctx.issues = list(ctx.issues) + context_issues  # visible to render_html's footer
    html = render_html(ctx, snapshot=getattr(_C, "SNAPSHOT_MODE", False))
    # Les issues retournées (et affichées par app.py) doivent inclure celles
    # collectées pendant build_context, pas seulement celles du validateur.
    issues = list(ctx.issues) + validate_html(html)
    ctx.issues = issues
    errors = [i for i in issues if i.severity == "ERROR"]
    if errors:
        logger.warning("Validation produced %d error(s): %s",
                       len(errors), [e.message for e in errors])
    return html, ctx, issues
