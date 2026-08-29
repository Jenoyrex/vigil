from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VIGIL_API_", env_file=".env")

    app_name: str = "Vigil API"
    database_url: str = "postgresql+psycopg://vigil:vigil@localhost:5434/vigil"


settings = Settings()
