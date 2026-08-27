from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://localhost/work_cube"

    work_cube_llm_model: str = "ollama/llama3.2:1b"
    work_cube_embedding_model: str = "ollama/nomic-embed-text"
    ollama_api_base: str = "http://localhost:11434"


settings = Settings()
