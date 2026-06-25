"""Application configuration loaded from environment / .env file."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Zabbix
    zabbix_url: str = "https://zabbix.nxlink.com/api_jsonrpc.php"
    zabbix_api_token: str = ""

    # SMAP (PCN / CoMSearch data) via codexCatalog MCP
    smap_api_key: str = ""
    smap_url: str = "https://codex-catalog.nxlink.com/mcp"
    smap_auth_header: str = ""

    # Radio credentials
    radio_username: str = "admin"
    radio_password: str = ""

    # Weather
    weather_api_url: str = "https://api.open-meteo.com"

    # App
    app_host: str = "0.0.0.0"
    app_port: int = 8501

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
