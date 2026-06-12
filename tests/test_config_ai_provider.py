from classin_toolkit.config import AppConfig, load_config
from classin_toolkit.intelligence.claude_client import resolve_model, resolve_provider


def _config_data(**anthropic):
    return {
        "academy": {"name": "Test Academy", "timezone": "Asia/Seoul"},
        "classin": {
            "school_id": "sid",
            "secret_key": "secret",
            "webhook_secret": "webhook",
        },
        "notion": {
            "token": "secret_test",
            "databases": {
                "students": "students",
                "lessons": "lessons",
                "reports": "reports",
            },
        },
        "anthropic": {"api_key": "sk-ant-test", **anthropic},
    }


def test_gemini_key_auto_selects_provider_and_model() -> None:
    cfg = AppConfig.model_validate(_config_data(api_key="AIza-test"))

    assert resolve_provider(cfg) == "gemini"
    assert resolve_model(cfg) == "gemini-3.5-flash"


def test_anthropic_key_wins_over_gemini_default_model() -> None:
    cfg = AppConfig.model_validate(_config_data(model="gemini-3.5-flash"))

    assert resolve_provider(cfg) == "anthropic"
    assert resolve_model(cfg) == "claude-sonnet-4-6"


def test_load_config_applies_profile_local_overrides(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    profile_path = tmp_path / "profile.local.yaml"
    config_path.write_text(
        """
academy:
  name: Config Academy
classin:
  school_id: sid
  secret_key: secret
notion:
  token: secret_test
  databases:
    students: students
    lessons: lessons
    reports: reports
anthropic:
  api_key: sk-ant-config
  provider: auto
  model: claude-sonnet-4-6
  report_model: claude-opus-4-7
""".strip(),
        encoding="utf-8",
    )
    profile_path.write_text(
        """
academy_name: Profile Academy
anthropic_api_key: AIza-profile
ai_provider: gemini
ai_model: gemini-3.5-flash
ai_report_model: gemini-3.5-flash
neis_api_key: neis-profile
""".strip(),
        encoding="utf-8",
    )
    load_config.cache_clear()

    cfg = load_config(config_path)

    assert cfg.academy.name == "Profile Academy"
    assert cfg.anthropic.api_key == "AIza-profile"
    assert cfg.anthropic.provider == "gemini"
    assert cfg.anthropic.model == "gemini-3.5-flash"
    assert cfg.neis.api_key == "neis-profile"
