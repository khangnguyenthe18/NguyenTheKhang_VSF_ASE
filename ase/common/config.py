"""Cấu hình qua env (prefix `ASE_`). Không chứa error catalog hay bảng giá (nằm ở knowledge/)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ASE_", env_file=".env", extra="ignore")

    data_dir: Path = ROOT / "data"
    llm_provider: str = "anthropic"
    llm_model: str = "claude-haiku-5-5"
    triage_mode: str = "single_shot"
    llm_budget_usd: float = Field(default=30, ge=0)
    llm_run_budget_usd: float = Field(default=2, ge=0)
    llm_max_output_tokens: int = 800
    allow_external_llm_on_masked: bool = True

    # Notifier settings (NOTIFIER_PLAN §7)
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_tls: Literal["none", "starttls", "ssl"] = "none"
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    mail_from: str | None = "ase-alerts@ase.local"
    mail_to: str | None = "ops@ase.local"
    teams_url: str = "http://localhost:8000"
    feedback_base_url: str = "http://localhost:8000"
    notifier_db: Path = ROOT / "data" / "notifier.sqlite"
    preview_dir: Path = ROOT / "data" / "outbox_preview"
    notify_mode: Literal["dry_run", "live"] = "live"
    max_alerts_per_run: int = Field(default=20, ge=1)

    # Dashboard settings (DASHBOARD_PLAN §6)
    cabinet_online_window_seconds: int = Field(default=120, ge=1)
    station_warn_fail_rate: float = Field(default=0.10, ge=0.0, le=1.0)
    ready_battery_soc_pct: float = Field(default=90.0, ge=0.0, le=100.0)
    # Tốc độ sạc giả định (%SOC/giờ) để ước lượng SOC hiện tại từ lần đọc telemetry cuối.
    battery_charge_rate_pct_per_hour: float = Field(default=60.0, ge=0.0)
    # Key Vietmap cho nền bản đồ. CHỈ dùng browser key (key này lộ ra trình duyệt; giới hạn HTTP
    # referer trong console Vietmap). Server key (VIETMAP_SERVER_API_KEY) không đưa vào trang.
    vietmap_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("ASE_VIETMAP_API_KEY", "VIETMAP_BROWSER_API_KEY"),
    )

    @field_validator("smtp_user", mode="before")
    @classmethod
    def _empty_user_to_none(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("vietmap_api_key", mode="before")
    @classmethod
    def _empty_vietmap_key_to_none(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("smtp_password", mode="before")
    @classmethod
    def _empty_secret_to_none(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        if isinstance(v, SecretStr) and not v.get_secret_value().strip():
            return None
        return v

    @field_validator("smtp_port", mode="before")
    @classmethod
    def _empty_port_to_default(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return 1025
        return v

    @field_validator("smtp_host", "smtp_tls", "mail_from", "mail_to", "notify_mode", mode="before")
    @classmethod
    def _empty_str_to_default(cls, v: object, info: object) -> object:
        if isinstance(v, str) and not v.strip():
            defaults = {
                "smtp_host": "localhost",
                "smtp_tls": "none",
                "mail_from": "ase-alerts@ase.local",
                "mail_to": "ops@ase.local",
                "notify_mode": "live",
            }
            field_name = getattr(info, "field_name", "")
            return defaults.get(field_name, v)
        return v

    @model_validator(mode="after")
    def _derive_defaults(self) -> Settings:
        if "notifier_db" not in self.model_fields_set:
            self.notifier_db = self.data_dir / "notifier.sqlite"
        if "preview_dir" not in self.model_fields_set:
            self.preview_dir = self.data_dir / "outbox_preview"
        return self

    def profile_dir(self, profile: str) -> Path:
        return self.data_dir / profile


def get_settings() -> Settings:
    return Settings()
