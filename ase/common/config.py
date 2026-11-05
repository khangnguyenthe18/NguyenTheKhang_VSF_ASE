"""Cấu hình qua env (prefix `ASE_`). Không chứa error catalog hay bảng giá (nằm ở knowledge/)."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
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

    def profile_dir(self, profile: str) -> Path:
        return self.data_dir / profile


def get_settings() -> Settings:
    return Settings()
