from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://runrealm:runrealm@localhost:5432/runrealm"
    redis_url: str = "redis://localhost:6379/0"

    jwt_secret: str = "dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    jwt_expire_hours: int = 720

    default_timezone: str = "Asia/Kolkata"
    tile_cache_ttl: int = 20          # seconds a territory vector tile is cached
    territory_min_area_m2: float = 500.0
    territory_min_distance_m: float = 200.0
    territory_min_points: int = 20
    territory_loop_close_m: float = 50.0


settings = Settings()
