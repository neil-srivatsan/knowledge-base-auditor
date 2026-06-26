"""Configuration loading from YAML files and environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv


@dataclass
class NotionSourceConfig:
    root_page_id: str | None = None
    database_id: str | None = None


@dataclass
class ConfluenceSourceConfig:
    base_url: str = ""
    email: str = ""
    api_token: str = ""
    space_key: str | None = None
    page_id: str | None = None


@dataclass
class TimestampAnalyzerConfig:
    warning_days: int = 90
    critical_days: int = 180


@dataclass
class SimilarityAnalyzerConfig:
    threshold: float = 0.80


@dataclass
class VersionRefsAnalyzerConfig:
    current_versions: dict[str, str] = field(default_factory=dict)
    patterns: list[str] = field(default_factory=lambda: [r"v\d+\.\d+(\.\d+)?"])


@dataclass
class AnalyzersConfig:
    timestamp: TimestampAnalyzerConfig = field(default_factory=TimestampAnalyzerConfig)
    similarity: SimilarityAnalyzerConfig = field(default_factory=SimilarityAnalyzerConfig)
    version_refs: VersionRefsAnalyzerConfig = field(default_factory=VersionRefsAnalyzerConfig)


@dataclass
class Config:
    notion: NotionSourceConfig = field(default_factory=NotionSourceConfig)
    confluence: ConfluenceSourceConfig = field(default_factory=ConfluenceSourceConfig)
    analyzers: AnalyzersConfig = field(default_factory=AnalyzersConfig)
    notion_api_key: str = ""
    database_url: str = "sqlite:///kbaudit.db"

    @classmethod
    def load(cls, config_path: str | Path | None = None) -> Config:
        load_dotenv()

        raw: dict = {}
        if config_path and Path(config_path).exists():
            with open(config_path) as f:
                raw = yaml.safe_load(f) or {}

        notion_raw = raw.get("notion", {})
        notion_cfg = NotionSourceConfig(
            root_page_id=notion_raw.get("root_page_id"),
            database_id=notion_raw.get("database_id"),
        )

        confluence_raw = raw.get("confluence", {})
        confluence_cfg = ConfluenceSourceConfig(
            base_url=os.environ.get(
                "CONFLUENCE_BASE_URL", confluence_raw.get("base_url", ""),
            ),
            email=os.environ.get(
                "CONFLUENCE_EMAIL", confluence_raw.get("email", ""),
            ),
            api_token=os.environ.get(
                "CONFLUENCE_API_TOKEN", confluence_raw.get("api_token", ""),
            ),
            space_key=os.environ.get(
                "CONFLUENCE_SPACE_KEY", confluence_raw.get("space_key"),
            ),
            page_id=os.environ.get(
                "CONFLUENCE_PAGE_ID", confluence_raw.get("page_id"),
            ),
        )

        analyzers_raw = raw.get("analyzers", {})
        ts_raw = analyzers_raw.get("timestamp", {})
        sim_raw = analyzers_raw.get("similarity", {})
        vr_raw = analyzers_raw.get("version_refs", {})

        analyzers_cfg = AnalyzersConfig(
            timestamp=TimestampAnalyzerConfig(
                warning_days=ts_raw.get("warning_days", 90),
                critical_days=ts_raw.get("critical_days", 180),
            ),
            similarity=SimilarityAnalyzerConfig(
                threshold=sim_raw.get("threshold", 0.80),
            ),
            version_refs=VersionRefsAnalyzerConfig(
                current_versions=vr_raw.get("current_versions", {}),
                patterns=vr_raw.get("patterns", [r"v\d+\.\d+(\.\d+)?"]),
            ),
        )

        api_key = os.environ.get("NOTION_API_KEY", "")
        db_url = os.environ.get("DATABASE_URL", raw.get("database_url", "sqlite:///kbaudit.db"))

        return cls(
            notion=notion_cfg,
            confluence=confluence_cfg,
            analyzers=analyzers_cfg,
            notion_api_key=api_key,
            database_url=db_url,
        )
