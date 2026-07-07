from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    ADMIN_LOGIN: str
    ADMIN_PASSWORD: str
    SECRET_KEY: str
    DB_HOST: str
    DB_PORT: int
    MARIADB_USER: str
    MARIADB_PASSWORD: str
    MARIADB_DATABASE: str
    REDIS_HOST: str
    BOT_TOKEN: str
    OWNER_ID: str
    POLLING: bool

    @property
    def database_url(self): return (
        f"mysql+aiomysql://{self.MARIADB_USER}:{self.MARIADB_PASSWORD}" f"@{self.DB_HOST}:{self.DB_PORT}/{self.MARIADB_DATABASE}")

    class Config:
        env_file = ".env"


settings = Settings()
DATABASE_URL = settings.database_url