from pathlib import Path

from srv.themes import load


def test_built_in_themes_include_default_and_variants():
    themes = load(Path(__file__).parent.parent / "themes")
    assert {t["id"] for t in themes} >= {"phosphor", "amber", "blue", "light"}
    assert all(t["colors"].get("--bg") for t in themes)


def test_custom_yaml_supports_comments_and_filters_unknown_properties(tmp_path):
    (tmp_path / "custom.yaml").write_text("""
# My local theme
name: Purple Test
colors:
  bg: '#120018' # comments are valid
  primary: '#d35cff'
  definitely-not-a-css-variable: red
""")
    [theme] = load(tmp_path)
    assert theme["id"] == "custom"
    assert theme["colors"] == {"--bg": "#120018", "--primary": "#d35cff"}


def test_invalid_theme_is_reported_without_breaking_other_files(tmp_path):
    (tmp_path / "bad.yaml").write_text("colors: [not, a, mapping]")
    [theme] = load(tmp_path)
    assert theme["error"]
