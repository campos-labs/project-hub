from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model: str = "gemini-2.5-flash"
    temperature: float = 0.0
    GOOGLE_API_KEY : str

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")