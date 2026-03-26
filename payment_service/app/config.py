from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:///./payment_service.db"
    bank_api_base_url: str = "https://bank.api"
    bank_api_timeout: float = 10.0
    bank_api_retries: int = 3

    class Config:
        env_file = ".env"


settings = Settings()
