"""Celery tasks for data ingestion.

These tasks can be scheduled via Celery Beat or triggered manually.
"""

from datetime import datetime

import structlog
from celery import shared_task

from apps.ingest.services import ScoreboardIngestionService, TeamIngestionService

logger = structlog.get_logger(__name__)

# All major leagues to refresh in scheduled tasks.
# Covers all 17 ESPN sports with primary professional leagues.
ALL_LEAGUES_CONFIG: list[tuple[str, str]] = [
    # Football
    ("football", "nfl"),
    ("football", "college-football"),
    ("football", "cfl"),
    ("football", "ufl"),
    # Basketball
    ("basketball", "nba"),
    ("basketball", "wnba"),
    ("basketball", "mens-college-basketball"),
    ("basketball", "womens-college-basketball"),
    ("basketball", "nba-development"),
    # Baseball
    ("baseball", "mlb"),
    ("baseball", "college-baseball"),
    # Hockey
    ("hockey", "nhl"),
    ("hockey", "mens-college-hockey"),
    # Soccer (major leagues)
    ("soccer", "eng.1"),
    ("soccer", "usa.1"),
    ("soccer", "esp.1"),
    ("soccer", "ger.1"),
    ("soccer", "ita.1"),
    ("soccer", "fra.1"),
    ("soccer", "mex.1"),
    ("soccer", "uefa.champions"),
    ("soccer", "uefa.europa"),
    ("soccer", "usa.nwsl"),
    # MMA
    ("mma", "ufc"),
    # Golf
    ("golf", "pga"),
    ("golf", "lpga"),
    ("golf", "liv"),
    # Tennis
    ("tennis", "atp"),
    ("tennis", "wta"),
    # Racing
    ("racing", "f1"),
    ("racing", "irl"),
    ("racing", "nascar-premier"),
    # Rugby (numeric slugs)
    ("rugby", "164205"),   # Rugby World Cup
    ("rugby", "180659"),   # Six Nations
    ("rugby", "267979"),   # Gallagher Premiership
    ("rugby", "242041"),   # Super Rugby Pacific
    # Rugby League
    ("rugby-league", "3"),
    # Lacrosse
    ("lacrosse", "pll"),
    ("lacrosse", "nll"),
    # Australian Football
    ("australian-football", "afl"),
    # Cricket
    ("cricket", "icc.t20"),
    ("cricket", "ipl"),
    # Volleyball
    ("volleyball", "fivb.w"),
    ("volleyball", "fivb.m"),
]


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=3,
    acks_late=True,
)
def refresh_scoreboard_task(
    self,
    sport: str,
    league: str,
    date: str | None = None,
) -> dict:
    """Celery task to refresh scoreboard data.

    Args:
        sport: Sport slug (e.g., "basketball")
        league: League slug (e.g., "nba")
        date: Optional date in YYYYMMDD format

    Returns:
        Dict with ingestion results
    """
    logger.info(
        "starting_scoreboard_refresh_task",
        sport=sport,
        league=league,
        date=date,
        task_id=self.request.id,
    )

    service = ScoreboardIngestionService()
    result = service.ingest_scoreboard(sport, league, date)

    logger.info(
        "completed_scoreboard_refresh_task",
        sport=sport,
        league=league,
        date=date,
        created=result.created,
        updated=result.updated,
        errors=result.errors,
        task_id=self.request.id,
    )

    return result.to_dict()


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=3,
    acks_late=True,
)
def refresh_teams_task(
    self,
    sport: str,
    league: str,
) -> dict:
    """Celery task to refresh team data.

    Args:
        sport: Sport slug (e.g., "basketball")
        league: League slug (e.g., "nba")

    Returns:
        Dict with ingestion results
    """
    logger.info(
        "starting_teams_refresh_task",
        sport=sport,
        league=league,
        task_id=self.request.id,
    )

    service = TeamIngestionService()
    result = service.ingest_teams(sport, league)

    logger.info(
        "completed_teams_refresh_task",
        sport=sport,
        league=league,
        created=result.created,
        updated=result.updated,
        errors=result.errors,
        task_id=self.request.id,
    )

    return result.to_dict()


@shared_task(bind=True)
def refresh_all_teams_task(self) -> dict:
    """Celery task to refresh team data for all configured leagues.

    Covers all 17 ESPN sports. Failures per league are logged and
    aggregated; the task completes even if individual leagues fail.

    Returns:
        Dict with aggregated results by league key (sport/league)
    """
    results = {}

    for sport, league in ALL_LEAGUES_CONFIG:
        try:
            service = TeamIngestionService()
            result = service.ingest_teams(sport, league)
            results[f"{sport}/{league}"] = result.to_dict()
        except Exception as e:
            logger.error(
                "league_teams_refresh_failed",
                sport=sport,
                league=league,
                error=str(e),
            )
            results[f"{sport}/{league}"] = {"error": str(e)}

    return results


@shared_task(bind=True)
def refresh_daily_scoreboards_task(self) -> dict:
    """Celery task to refresh today's scoreboards for all leagues.

    Covers all 17 ESPN sports. Failures per league are logged and
    aggregated; the task completes even if individual leagues fail.

    Returns:
        Dict with aggregated results by league key (sport/league)
    """
    today = datetime.now().strftime("%Y%m%d")
    results = {}

    for sport, league in ALL_LEAGUES_CONFIG:
        try:
            service = ScoreboardIngestionService()
            result = service.ingest_scoreboard(sport, league, today)
            results[f"{sport}/{league}"] = result.to_dict()
        except Exception as e:
            logger.error(
                "league_scoreboard_refresh_failed",
                sport=sport,
                league=league,
                date=today,
                error=str(e),
            )
            results[f"{sport}/{league}"] = {"error": str(e)}

    return results

