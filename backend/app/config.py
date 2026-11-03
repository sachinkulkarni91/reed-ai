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
    bot_display_name: str = "Reed AI Notetaker"
    bot_headless: bool = True
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
