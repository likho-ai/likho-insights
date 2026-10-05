"""Configuration, read from environment variables and the .env files of the current environment.

LIKHO_ENV (development, staging or production; default development) picks the files. They are
read in this order, each one overriding the one before, and a real environment variable wins
over all of them:

    .env  .env.local  .env.<LIKHO_ENV>  .env.<LIKHO_ENV>.local

The .env.<LIKHO_ENV> files are committed and hold no secrets; the .local files are ignored by
git and hold the secrets of that environment on this machine - here above all the model's key.
"""

import os

from pydantic_settings import BaseSettings, SettingsConfigDict

LIKHO_ENV = os.environ.get("LIKHO_ENV", "development")
ENV_FILES = (".env", ".env.local", f".env.{LIKHO_ENV}", f".env.{LIKHO_ENV}.local")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILES, extra="ignore")

    likho_env: str = LIKHO_ENV
    log_level: str = "INFO"

    grpc_port: int = 5050
    http_port: int = 4050  # /healthz, /readyz and /metrics

    # Defaults match the likho-infra local stack.
    mongo_url: str = "mongodb://localhost:27017"
    mongo_database: str = "likho_insights"
    nats_url: str = "nats://localhost:4222"
    nats_connect_timeout_seconds: float = 120.0
    otel_exporter_otlp_endpoint: str = ""

    # Where transcripts are fetched from.
    transcription_grpc_addr: str = "localhost:5020"
    rpc_timeout_seconds: float = 10.0

    # The model. Without a key no model is configured: nothing is analysed and no text leaves.
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5-5"
    model_timeout_seconds: float = 120.0
    model_max_output_tokens: int = 2000
    # The longest transcript sent, in characters of its Hinglish layer; longer ones are cut with a note.
    max_transcript_chars: int = 24000
    # The language the summary and the reasons are written in.
    report_language: str = "English"

    # The auditor's form: the checks and the scored points. The company's own lives in
    # config/qa.local.json (ignored by git); the example is what the repository ships.
    qa_form_file: str = "config/qa.example.json"

    # Take likho.transcription.completed from the bus and analyse every transcript (when a model is configured).
    consumers_enabled: bool = True
    consumer_group: str = "likho-insights"
    # "new" starts at the transcripts completed from now on; "all" takes the stream from its start.
    consumer_start: str = "new"
