"""Configuration. Secrets live outside git: ~/.config/school-agent/ (0700) or real environment variables."""
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse
import os
import socket
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

DEFAULT_SECRETS_DIR = Path.home() / '.config' / 'school-agent'
LOCAL_HOSTS = {'localhost', '127.0.0.1', '::1'}


def is_local_address(host):
    """True for loopback or any address assigned to THIS machine (e.g. its Tailscale IP). Remote hosts are never local."""
    if not host:
        return False
    if host in LOCAL_HOSTS:
        return True
    try:
        target = {ai[4][0] for ai in socket.getaddrinfo(host, None)}
        mine = {ai[4][0] for ai in socket.getaddrinfo(socket.gethostname(), None)}
        for ip in target:
            if ip.startswith('127.') or ip == '::1':
                return True
            probe = socket.socket(socket.AF_INET6 if ':' in ip else socket.AF_INET, socket.SOCK_DGRAM)
            try:
                probe.bind((ip, 0))        # binding only succeeds for an address owned by this host
                return True
            except OSError:
                pass
            finally:
                probe.close()
        return bool(target & mine)
    except OSError:
        return False


def _bool(value, default=False):
    if value is None or value == '':
        return default
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


@dataclass(frozen=True)
class Config:
    canvas_base_url: str = ''
    canvas_token: str = ''
    canvas_browser_state: str = ''      # Playwright storage_state file (outside git); optional fallback
    browser_export_json: str = ''
    ai_base_url: str = ''
    ai_api_key: str = ''
    ai_model: str = ''
    ai_provider: str = 'openai'         # openai (any OpenAI-compatible endpoint) | anthropic
    ai_num_ctx: int = 4096
    ai_max_chars: int = 8000            # per-source cap after date-window selection
    ai_prefilter: bool = True           # skip sources with no date-like text (no model call)
    ocr_enabled: bool = True            # local OCR of image-only PDFs/images when the `ocr` extra is installed
    ai_data_approved: bool = False      # explicit approval to send school text to a NON-local provider
    dry_run: bool = True
    todoist_api_token: str = ''
    todoist_project_id: str = ''
    todoist_dedupe_all_projects: bool = True
    google_calendar_id: str = ''        # dedicated school calendar (writable)
    google_readonly_calendar_ids: tuple = ()   # e.g. Canvas subscription calendar, checked for collisions only
    secrets_dir: str = str(DEFAULT_SECRETS_DIR)
    alert_webhook_url: str = ''
    db_path: str = 'state/agent.sqlite'
    state_dir: str = 'state'
    timezone: str = 'America/New_York'
    lookahead_days: int = 90
    max_document_mb: int = 15
    min_confidence: float = 0.85
    extra_document_hosts: tuple = ('docs.google.com', 'drive.google.com')

    @property
    def ai_is_local(self):
        host = urlparse(self.ai_base_url or ('http://127.0.0.1:11434' if self.ai_provider == 'ollama' else '')).hostname
        return is_local_address(host)

    @property
    def ai_allowed(self):
        """Documents may only be sent to a configured provider that is local or explicitly approved."""
        has_endpoint = bool(self.ai_base_url) or self.ai_provider in ('anthropic', 'ollama')
        return bool(has_endpoint and self.ai_model and (self.ai_is_local or self.ai_data_approved))

    @classmethod
    def from_env(cls):
        secrets = Path(os.environ.get('SCHOOL_AGENT_SECRETS_DIR', DEFAULT_SECRETS_DIR))
        for candidate in (secrets / 'env', Path('.env')):
            if candidate.exists():
                load_dotenv(candidate, override=False)

        def get(key, default=''):
            return os.environ.get(key, default).strip()
        tz = get('TIMEZONE', 'America/New_York')
        ZoneInfo(tz)
        csv = lambda v: tuple(x.strip() for x in v.split(',') if x.strip())
        return cls(
            canvas_base_url=get('CANVAS_BASE_URL').rstrip('/'), canvas_token=get('CANVAS_TOKEN'),
            canvas_browser_state=get('CANVAS_BROWSER_STATE'), browser_export_json=get('BROWSER_EXPORT_JSON'),
            ai_base_url=get('AI_BASE_URL').rstrip('/'), ai_api_key=get('AI_API_KEY'), ai_model=get('AI_MODEL'),
            ai_provider=get('AI_PROVIDER', 'openai').lower(), ai_num_ctx=int(get('AI_NUM_CTX', '4096')),
            ai_max_chars=int(get('AI_MAX_CHARS', '8000')), ai_prefilter=_bool(get('AI_PREFILTER'), True), ocr_enabled=_bool(get('OCR_ENABLED'), True), ai_data_approved=_bool(get('AI_DATA_APPROVED')),
            dry_run=get('DRY_RUN', 'true').lower() != 'false',
            todoist_api_token=get('TODOIST_API_TOKEN'), todoist_project_id=get('TODOIST_PROJECT_ID'),
            todoist_dedupe_all_projects=_bool(get('TODOIST_DEDUPE_ALL_PROJECTS'), True),
            google_calendar_id=get('GOOGLE_CALENDAR_ID'),
            google_readonly_calendar_ids=csv(get('GOOGLE_READONLY_CALENDAR_IDS')),
            secrets_dir=str(secrets), alert_webhook_url=get('ALERT_WEBHOOK_URL'),
            db_path=get('DB_PATH', 'state/agent.sqlite'), state_dir=get('STATE_DIR', 'state'),
            timezone=tz, lookahead_days=int(get('LOOKAHEAD_DAYS', '90')),
            max_document_mb=int(get('MAX_DOCUMENT_MB', '15')),
            min_confidence=float(get('MIN_CONFIDENCE', '0.85')),
        )
