"""Configuration registry, validation and persistence."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from aria2curl import configcmd
from aria2curl.config import (
    CATEGORY_ORDER,
    FIELDS,
    Config,
    coerce,
    default_config_path,
    display_value,
    resolve_field,
    toml_literal,
)
from aria2curl.errors import ConfigError, UsageError


def test_registry_is_well_formed() -> None:
    names = [field.name for field in FIELDS]
    assert len(names) == len(set(names)), "duplicate configuration key"
    for field in FIELDS:
        assert field.category in CATEGORY_ORDER, field.name
        assert field.help, field.name
        # every default must survive its own validator
        assert coerce(field, field.default) == field.default
        if field.choices:
            assert field.default in field.choices, field.name


def test_aliases_and_sections_resolve() -> None:
    assert resolve_field("split").name == "split"
    assert resolve_field("chunks").name == "split"
    assert resolve_field("download.split").name == "split"
    assert resolve_field("aria2curl.split").name == "split"
    assert resolve_field("max-connections").name == "max_connection_per_server"
    assert resolve_field("display.engine").name == "engine"
    assert resolve_field("progress").name == "engine"


def test_unknown_key_suggests_alternatives() -> None:
    with pytest.raises(ConfigError) as excinfo:
        resolve_field("splitt")
    assert "split" in str(excinfo.value)


@pytest.mark.parametrize(
    "key,value,expected",
    [
        ("auto_resume", "yes", True),
        ("auto_resume", "OFF", False),
        ("split", "16", 16),
        ("refresh_per_second", "2.5", 2.5),
        ("engine", "json", "json"),
        ("headers", "X-A: 1, X-B: 2", ["X-A: 1, X-B: 2"]),
        ("headers", "X-A: 1\nX-B: 2", ["X-A: 1", "X-B: 2"]),
        ("extra_args", "--foo=1,--bar=2", ["--foo=1", "--bar=2"]),
        ("extra_args", '["--foo=1"]', ["--foo=1"]),
    ],
)
def test_coercion(key: str, value: str, expected: object) -> None:
    field = resolve_field(key)
    assert coerce(field, value) == expected


@pytest.mark.parametrize(
    "key,value",
    [
        ("split", "0"),
        ("split", "65"),
        ("split", "abc"),
        ("engine", "nope"),
        ("auto_resume", "maybe"),
        ("extra_args", "[not json"),
    ],
)
def test_invalid_values_are_rejected(key: str, value: str) -> None:
    with pytest.raises(ConfigError):
        coerce(resolve_field(key), value)


def test_toml_literals_round_trip() -> None:
    for field in FIELDS:
        literal = toml_literal(field, field.default)
        assert tomllib.loads(f"x = {literal}")["x"] == field.default


def test_defaults_and_provenance() -> None:
    cfg = Config.defaults()
    assert cfg["split"] == 5
    assert cfg.source_of("split") == "default"
    assert cfg.non_default() == {}


def test_set_get_unset_reset(config_file: Path) -> None:
    cfg = Config.load(config_file)
    cfg.set("split", "12")
    assert cfg["split"] == 12
    assert cfg.source_of("split") == "cli"
    assert cfg.non_default() == {"split": 12}
    cfg.unset("split")
    assert cfg["split"] == 5
    cfg.set("split", 9)
    cfg.reset()
    assert cfg.non_default() == {}
    assert cfg.dirty, "reset must be persisted by save()"
    cfg.save()
    assert not cfg.dirty


def test_save_and_reload_round_trip(config_file: Path) -> None:
    cfg = Config.load(config_file)
    cfg.set("split", 16)
    cfg.set("auto_resume", False)
    cfg.set("headers", ["X-Test: 1", "Accept: */*"])
    cfg.set("engine", "json")
    target = cfg.save()
    assert target == config_file
    assert config_file.is_file()

    reloaded = Config.load(config_file)
    assert reloaded["split"] == 16
    assert reloaded["auto_resume"] is False
    assert reloaded["headers"] == ["X-Test: 1", "Accept: */*"]
    assert reloaded.source_of("split") == "file"
    assert reloaded["engine"] == "json"


def test_file_accepts_flat_and_sectioned_keys(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        'split = 7\n\n[download]\nauto_resume = false\n\n[display]\nengine = "plain"\n',
        encoding="utf-8",
    )
    cfg = Config.load(path)
    assert (cfg["split"], cfg["auto_resume"], cfg["engine"]) == (7, False, "plain")


def test_unknown_key_in_file_is_fatal(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("nonsense = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        Config.load(path)


def test_duplicate_key_in_file_is_fatal(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("split = 1\n[download]\nsplit = 2\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        Config.load(path)


def test_invalid_toml_is_fatal(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("split = = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        Config.load(path)


def test_env_overrides_file(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text("split = 3\n", encoding="utf-8")
    monkeypatch.setenv("ARIA2CURL_SPLIT", "9")
    cfg = Config.load(config_file)
    assert cfg["split"] == 9
    assert cfg.source_of("split") == "env"


def test_cli_overrides_win(config_file: Path) -> None:
    cfg = Config.load(config_file, overrides={"split": "11", "chunks": "12"})
    assert cfg["split"] == 12
    assert cfg.source_of("split") == "cli"


def test_empty_config_env_disables_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARIA2CURL_CONFIG", "")
    assert default_config_path() is None
    cfg = Config.load()
    assert cfg.path is None
    with pytest.raises(ConfigError):
        cfg.save()


def test_xdg_config_home_is_honoured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("ARIA2CURL_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert default_config_path() == tmp_path / "xdg" / "aria2curl" / "config.toml"


@pytest.mark.parametrize(
    "argv,expected",
    [
        (["split", "16"], [("split", "16")]),
        (["split=16"], [("split", "16")]),
        (["split", "16", "auto_resume", "false"], [("split", "16"), ("auto_resume", "false")]),
        (["a=1", "b=2"], [("a", "1"), ("b", "2")]),
    ],
)
def test_parse_assignments(argv: list[str], expected: list[tuple[str, str]]) -> None:
    assert configcmd.parse_assignments(argv) == expected


@pytest.mark.parametrize("argv", [["split"], ["a=1", "dangling"]])
def test_parse_assignments_rejects_incomplete(argv: list[str]) -> None:
    with pytest.raises(UsageError):
        configcmd.parse_assignments(argv)


def test_config_set_and_unset_through_the_cli(config_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = Config.load(config_file)
    assert configcmd.run(["set", "split", "16", "auto_resume", "false"], cfg) == 0
    assert config_file.is_file()
    assert "split = 16" in capsys.readouterr().out
    assert Config.load(config_file)["split"] == 16

    assert configcmd.run(["unset", "split"], cfg) == 0
    assert Config.load(config_file)["split"] == 5


def test_config_get_is_scriptable(config_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = Config.load(config_file)
    cfg.set("split", 16)
    assert configcmd.run(["get", "split"], cfg) == 0
    assert capsys.readouterr().out.strip() == "16"


def test_config_list_json(config_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import json

    cfg = Config.load(config_file)
    assert configcmd.run(["list", "--json"], cfg) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["values"]["split"]["value"] == 5
    assert payload["values"]["split"]["source"] == "default"
    assert payload["values"]["split"]["aria2"] == "--split"


def test_display_value_helpers() -> None:
    assert display_value(resolve_field("auto_resume"), True) == "true"
    assert display_value(resolve_field("headers"), []) == "(empty)"
    assert display_value(resolve_field("dir"), "") == "(empty)"
    assert display_value(resolve_field("refresh_per_second"), 10.0) == "10"


# ------------------------------------------------------------- system layer


def test_system_config_path_defaults_and_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from aria2curl.config import DEFAULT_SYSTEM_CONFIG, system_config_path

    monkeypatch.delenv("ARIA2CURL_SYSTEM_CONFIG", raising=False)
    assert system_config_path() == Path(DEFAULT_SYSTEM_CONFIG)
    monkeypatch.setenv("ARIA2CURL_SYSTEM_CONFIG", str(tmp_path / "sys.toml"))
    assert system_config_path() == tmp_path / "sys.toml"
    monkeypatch.setenv("ARIA2CURL_SYSTEM_CONFIG", "")
    assert system_config_path() is None


def test_system_layer_is_layered_under_the_user_file(tmp_path: Path) -> None:
    system = tmp_path / "etc" / "aria2curl" / "config.toml"
    user = tmp_path / "home" / "config.toml"
    system.parent.mkdir(parents=True)
    user.parent.mkdir(parents=True)
    system.write_text('[engine]\ncurl_path = "/usr/bin/curl"\nsplit = 8\n', encoding="utf-8")
    user.write_text("split = 12\n", encoding="utf-8")

    cfg = Config.load(user, system_path=system, use_env=False)
    assert cfg["curl_path"] == "/usr/bin/curl"
    assert cfg.source_of("curl_path") == "system"
    assert cfg["split"] == 12, "the user layer must win"
    assert cfg.source_of("split") == "file"
    assert cfg.system_path == system


def test_system_layer_alone_applies(tmp_path: Path) -> None:
    system = tmp_path / "sys.toml"
    system.write_text("split = 3\n", encoding="utf-8")
    cfg = Config.load(tmp_path / "missing-user.toml", system_path=system, use_env=False)
    assert cfg["split"] == 3
    assert cfg.source_of("split") == "system"


def test_environment_beats_the_system_layer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    system = tmp_path / "sys.toml"
    system.write_text("split = 3\n", encoding="utf-8")
    monkeypatch.setenv("ARIA2CURL_SPLIT", "5")
    cfg = Config.load(tmp_path / "missing.toml", system_path=system)
    assert cfg["split"] == 5
    assert cfg.source_of("split") == "env"


def test_broken_system_file_is_reported(tmp_path: Path) -> None:
    system = tmp_path / "sys.toml"
    system.write_text("nonsense = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        Config.load(tmp_path / "missing.toml", system_path=system, use_env=False)


def test_saving_an_explicit_layer_does_not_adopt_it(tmp_path: Path) -> None:
    cfg = Config.load(tmp_path / "user.toml", system_path=tmp_path / "sys.toml", use_env=False)
    cfg.set("split", 7)
    target = cfg.save(tmp_path / "elsewhere.toml", adopt=False)
    assert target == tmp_path / "elsewhere.toml"
    assert cfg.path == tmp_path / "user.toml"
