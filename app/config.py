from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql://postgres:postgres@localhost:5432/cashcompass"

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
