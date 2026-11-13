from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Claude
    anthropic_api_key: str = ""
    reed_model: str = "claude-opus-5"
    reed_effort: str = "high"
    vision_speaker_names: bool = True  # read names from video frames for uploads

    # Transcription
    transcription_provider: str = "whisper"  # whisper | sarvam | deepgram | mock
    whisper_model: str = "base"
    hf_token: str = ""  # Hugging Face token for pyannote speaker diarization
    deepgram_api_key: str = ""
    deepgram_model: str = "nova-3"
    sarvam_api_key: str = ""
    sarvam_model: str = "saaras:v3"
    sarvam_mode: str = "codemix"      # transcribe | translate | verbatim | translit | codemix
    sarvam_language: str = "unknown"  # BCP-47 like hi-IN / en-IN, or "unknown" to auto-detect

    # Trello
    trello_api_key: str = ""
    trello_token: str = ""
    trello_list_id: str = ""
    trello_auto_push: bool = False

    # Bot
    bot_display_name: str = "KPMG Notely"
    bot_headless: bool = True
    # Signed-in bot identity: a Playwright storage-state file (cookies) made by
    # scripts/bot_login.py, or its JSON pasted into BOT_STORAGE_STATE_JSON on a
    # host with no disk access. Unset = join as an anonymous guest.
    bot_storage_state: str = "data/bot-auth.json"
    bot_storage_state_json: str = ""
    # Auto-join: watch the bot account's calendar (Microsoft Graph) and join
    # Teams invites when they start. Needs an Entra app registration (client id)
    # and a one-time sign-in via scripts/calendar_login.py.
    auto_join_enabled: bool = False
    ms_client_id: str = ""
    calendar_poll_seconds: int = 60
    calendar_token_file: str = "data/graph-token.json"
    server_port: int = 8010  # where this app listens; the bot's ingest socket points here
    bot_max_meeting_minutes: int = 180

    # Server
    database_url: str = "sqlite:///./data/reed.db"
    media_dir: str = "./data/media"
    cors_origins: str = "http://localhost:5173"

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def media_path(self) -> Path:
        p = Path(self.media_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
