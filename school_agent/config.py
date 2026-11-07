from dataclasses import dataclass
import os
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

@dataclass(frozen=True)
class Config:
    canvas_base_url: str
    canvas_token: str
    browser_export_json: str
    ai_base_url: str
    ai_api_key: str
    ai_model: str
    dry_run: bool
    todoist_api_token: str
    todoist_project_id: str
    google_access_token: str
    google_calendar_id: str
    db_path: str
    timezone: str
    lookahead_days: int
    max_document_mb: int

    @classmethod
    def from_env(cls):
        load_dotenv()
        def get(key, default=""):
            return os.environ.get(key, default).strip()
        tz = get("TIMEZONE", "America/New_York")
        ZoneInfo(tz)
        return cls(
            get("CANVAS_BASE_URL").rstrip("/"), get("CANVAS_TOKEN"),
            get("BROWSER_EXPORT_JSON"), get("AI_BASE_URL").rstrip("/"),
            get("AI_API_KEY"), get("AI_MODEL"), get("DRY_RUN", "true").lower() != "false",
            get("TODOIST_API_TOKEN"), get("TODOIST_PROJECT_ID"),
            get("GOOGLE_ACCESS_TOKEN"), get("GOOGLE_CALENDAR_ID"),
            get("DB_PATH", "state/agent.sqlite"), tz,
            int(get("LOOKAHEAD_DAYS", "90")), int(get("MAX_DOCUMENT_MB", "15"))
        )
