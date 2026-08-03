from functools import lru_cache
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "sqlite:///./zorgverhaal.db"
    openai_api_key: str = ""
    openai_model: str = "gpt-5.6-terra"
    openai_complex_model: str = "gpt-5.6-sol"
    openai_reasoning_effort: str = "low"
    openai_timeout_seconds: float = 35.0
    openai_max_retries: int = 0
    data_encryption_key: str = ""
    jwt_secret: str = "dev-only-change-me"
    bootstrap_admin_email: str = "admin@example.nl"
    bootstrap_admin_password: str = "change-this-long-password"
    bootstrap_org_name: str = "Kleinschalige Zorg Demo"
    cookie_secure: bool = False
    allowed_origins: str = "http://localhost:8000"
    railway_environment: str = ""
    railway_environment_name: str = ""
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @model_validator(mode="after")
    def normalize_and_validate(self):
        if self.database_url.startswith("postgres://"):
            self.database_url = self.database_url.replace("postgres://", "postgresql+psycopg://", 1)
        elif self.database_url.startswith("postgresql://"):
            self.database_url = self.database_url.replace("postgresql://", "postgresql+psycopg://", 1)
        if self.railway_environment or self.railway_environment_name:
            missing = []
            if not self.openai_api_key: missing.append("OPENAI_API_KEY")
            if not self.data_encryption_key: missing.append("DATA_ENCRYPTION_KEY")
            if self.jwt_secret == "dev-only-change-me": missing.append("JWT_SECRET")
            if self.bootstrap_admin_password == "change-this-long-password": missing.append("BOOTSTRAP_ADMIN_PASSWORD")
            if missing:
                raise ValueError("Ontbrekende/onveilige productievariabelen: " + ", ".join(missing))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
