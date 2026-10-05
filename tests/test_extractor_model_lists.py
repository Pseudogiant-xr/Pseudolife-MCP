"""One list of extractor modes and dreamer models, wherever it is written.

The installers, the Claude shim's autostart scripts, the dreaming guide, the
Console's dreamer menu, the Extractor panel's suggestions and the Claude
shim's /models listing each name the models a CLI shim mode offers, and the
installers and the guide each name the extractor modes. A model release
added in one place only would leave an installer refusing a model the guide
offers, or the reverse, so every list is parsed here and compared as a set.
"""
from __future__ import annotations

from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODEL = r"(?:claude|gpt)-[a-z0-9][a-z0-9.-]*[a-z0-9]"


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9][a-z0-9.-]*[a-z0-9]", text))


def _family(models: set[str], prefix: str) -> set[str]:
    # claude-only / claude-fallback are modes, not models.
    return {model for model in models if model.startswith(prefix)
            and not model.endswith(("-only", "-fallback"))}


def _sh_list(name: str) -> set[str]:
    match = re.search(rf'(?m)^{name}="([^"]*)"$', _read("ops/install.sh"))
    assert match, f"ops/install.sh has no {name}=\"...\" list"
    return set(match.group(1).split())


def _ps_list(name: str) -> set[str]:
    match = re.search(rf"(?ms)^\${name} = @\((.*?)\)$", _read("ops/install.ps1"))
    assert match, f"ops/install.ps1 has no ${name} = @(...) list"
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def _menu(text: str, pattern: str) -> set[str]:
    """Names offered by the interactive menus: `  N) name ...` lines."""
    return set(re.findall(rf"(?m)^\s*(?:echo|Write-Host) \"  \d\) ({pattern})\b", text))


def _autostart(rel: str, family: str = "Claude") -> set[str]:
    # The help comment may wrap: read from the label to "(the list ...".
    match = re.search(rf"(?s)#\s+{family} models:(.*?)\(the list", _read(rel))
    assert match, f"{rel} has no '# {family} models: ... (the list ...)' help text"
    return set(re.findall(MODEL, match.group(1)))


def _guide_section() -> str:
    text = _read("docs/guide/dreaming.md")
    assert "\n## Extractor modes and dreamer models\n" in text
    return text.split("\n## Extractor modes and dreamer models\n", 1)[1].split("\n## ", 1)[0]


def _guide_entries(family: str) -> list[tuple[str, str]]:
    """(model, text) per `  - \\`model\\`: text` item under `- Family:`.
    One item per model: an id mentioned inside another item's prose (the
    5.5 entry names claude-opus-5) does not count as an entry."""
    match = re.search(rf"(?m)^- {family}:\n((?:  .*\n)+)", _guide_section())
    assert match, f"dreaming.md has no '- {family}:' list"
    entries = re.findall(rf"(?m)^  - `({MODEL})`:(.*(?:\n    .*)*)", match.group(1))
    assert entries, f"dreaming.md's {family} list has no '  - `model`:' items"
    return entries


def _guide_models(family: str) -> set[str]:
    return {model for model, _ in _guide_entries(family)}


def _guide_default(family: str) -> str:
    defaults = [model for model, text in _guide_entries(family)
                if text.lstrip().startswith("the default")]
    assert len(defaults) == 1, f"dreaming.md's {family} list names {defaults} as the default"
    return defaults[0]


def _guide_modes() -> set[str]:
    return set(re.findall(r"(?m)^\| `([a-z-]+)` \|", _guide_section()))


def _console_models() -> set[str]:
    block = _read("frontend/src/lib/dreamer.ts").split(
        "export const DREAMER_MODELS = [", 1)[1].split("];", 1)[0]
    return set(re.findall(rf'id: "({MODEL})"', block))


def _panel_suggestions() -> set[str]:
    block = _read("pseudolife_memory/web/config_io.py").split(
        '"path": "memory.dream.extractor_model_override"', 1)[1].split('"help"', 1)[0]
    return set(re.findall(rf'"({MODEL})"', block))


def _shim_models(rel: str = "evals/claude_shim.py") -> set[str]:
    block = _read(rel).split('"/v1/models", "/models"', 1)[1].split("])]", 1)[0]
    return set(re.findall(rf'"({MODEL})"', block))


def test_the_claude_model_list_is_one_list():
    installer = _sh_list("CLAUDE_MODELS")
    assert "claude-opus-5" in installer
    sources = {
        "ops/install.ps1 $claudeModels": _ps_list("claudeModels"),
        "ops/install.sh menu": _family(_menu(_read("ops/install.sh"), MODEL), "claude-"),
        "ops/install.ps1 menu": _family(_menu(_read("ops/install.ps1"), MODEL), "claude-"),
        "ops/install-shim-autostart.sh help": _autostart("ops/install-shim-autostart.sh"),
        "ops/install-shim-autostart.ps1 help": _autostart("ops/install-shim-autostart.ps1"),
        "docs/guide/dreaming.md": _guide_models("Claude"),
        "dreamer.ts DREAMER_MODELS": _family(_console_models(), "claude-"),
        "config_io extractor_model_override": _family(_panel_suggestions(), "claude-"),
        "evals/claude_shim.py /models": _shim_models(),
    }
    for where, models in sources.items():
        assert models == installer, f"{where} differs from install.sh CLAUDE_MODELS"


def test_the_openai_model_list_is_one_list():
    installer = _sh_list("OPENAI_MODELS")
    assert "gpt-5.6-terra" in installer
    sources = {
        "ops/install.ps1 $openaiModels": _ps_list("openaiModels"),
        "ops/install.sh menu": _family(_menu(_read("ops/install.sh"), MODEL), "gpt-"),
        "ops/install.ps1 menu": _family(_menu(_read("ops/install.ps1"), MODEL), "gpt-"),
        "ops/install-codex-shim-autostart.sh help": _autostart(
            "ops/install-codex-shim-autostart.sh", "OpenAI"),
        "ops/install-codex-shim-autostart.ps1 help": _autostart(
            "ops/install-codex-shim-autostart.ps1", "OpenAI"),
        "docs/guide/dreaming.md": _guide_models("OpenAI"),
        "dreamer.ts DREAMER_MODELS": _family(_console_models(), "gpt-"),
        "config_io extractor_model_override": _family(_panel_suggestions(), "gpt-"),
        "evals/codex_shim.py /models": _shim_models("evals/codex_shim.py"),
    }
    for where, models in sources.items():
        assert models == installer, f"{where} differs from install.sh OPENAI_MODELS"


def _one(pattern: str, rel: str) -> str:
    match = re.search(pattern, _read(rel))
    assert match, f"{rel}: no match for {pattern!r}"
    return match.group(1)


# Maintainer decisions: claude-opus-5-5 promoted 2026-09-29 on the paired
# ladder gate (evals/results/ladder-opus55-paired-verdict-threshold.json);
# gpt-5.6-terra stays until a ladder run measures GPT-6.
@pytest.mark.parametrize("family,prefix,sh_autostart,ps_autostart,default", [
    ("Claude", "claude-", "ops/install-shim-autostart.sh", "ops/install-shim-autostart.ps1",
     "claude-opus-5-5"),
    ("OpenAI", "gpt-", "ops/install-codex-shim-autostart.sh",
     "ops/install-codex-shim-autostart.ps1", "gpt-5.6-terra"),
])
def test_every_place_names_the_same_default(family, prefix, sh_autostart, ps_autostart,
                                            default):
    """A default is promoted in one change: the autostart scripts' own
    default, the installers' menu entry 1 (Enter) and non-interactive choice,
    and the guide's '(the default' model all agree."""
    names = {
        f"{sh_autostart} MODEL=": _one(rf'(?m)^MODEL="({prefix}[^"]+)"$', sh_autostart),
        f"{ps_autostart} $Model =": _one(rf'\[string\]\$Model = "({prefix}[^"]+)"',
                                          ps_autostart),
        # (?!only|fallback): the extractor-mode menu's "1) claude-only" is a mode.
        "ops/install.sh menu 1)": _one(rf'(?m)^\s*echo "  1\) ({prefix}(?!only\b|fallback\b)[\w.-]+)',
                                       "ops/install.sh"),
        "ops/install.ps1 menu 1)": _one(
            rf'(?m)^\s*Write-Host "  1\) ({prefix}(?!only\b|fallback\b)[\w.-]+)',
                                        "ops/install.ps1"),
        "ops/install.sh non-interactive": _one(
            rf'(?m)^\s*else\n\s*MODEL=({prefix}[\w.-]+)\n', "ops/install.sh"),
        "ops/install.ps1 non-interactive": _one(
            rf'\}} else \{{\n\s*\$Model = "({prefix}[\w.-]+)"', "ops/install.ps1"),
        "docs/guide/dreaming.md": _guide_default(family),
        # the fallback an empty --model (installer pass-through) lands on
        f"{sh_autostart} empty --model": _one(
            rf'(?m)^\[ -n "\$MODEL" \] \|\| MODEL="({prefix}[^"]+)"$', sh_autostart),
    }
    assert set(names.values()) == {default}, names


MODE = r"sidecar|endpoint(?:-fallback)?|(?:claude|openai)-(?:only|fallback)"


def test_the_extractor_mode_list_is_one_list():
    installer = _sh_list("EXTRACTOR_MODES")
    assert {"sidecar", "claude-only", "endpoint"} <= installer
    sh = _read("ops/install.sh")
    ps = _read("ops/install.ps1")
    usage = sh.split("# >>> usage >>>", 1)[1].split("# <<< usage <<<", 1)[0]
    ps_header = ps.split("param(", 1)[0]
    ps_validate = ps.split("param(", 1)[1].split("[string]$Extractor", 1)[0]
    sources = {
        "ops/install.ps1 $extractorModes": _ps_list("extractorModes"),
        "ops/install.sh menu": _menu(sh, MODE),
        "ops/install.ps1 menu": _menu(ps, MODE),
        "ops/install.sh usage": set(re.findall(rf"(?m)^#   ({MODE}) ", usage)),
        "ops/install.ps1 header": set(re.findall(rf"(?m)^#   ({MODE}) ", ps_header)),
        "ops/install.ps1 -Extractor ValidateSet": set(re.findall(
            rf'"({MODE})"', ps_validate)),
        "docs/guide/dreaming.md": _guide_modes(),
    }
    for where, modes in sources.items():
        assert modes == installer, f"{where} differs from install.sh EXTRACTOR_MODES"


@pytest.mark.parametrize("old", ["sonnet-only", "sonnet-fallback", "codex-only",
                                 "codex-fallback"])
def test_the_old_mode_names_survive_only_as_aliases(old):
    """The model-era names stay accepted (both installers, and -Extractor's
    ValidateSet) but are listed nowhere as modes."""
    ps = _read("ops/install.ps1")
    assert f'"{old}"' in ps.split("param(", 1)[1].split("[string]$Extractor", 1)[0]
    assert old not in _sh_list("EXTRACTOR_MODES")
    assert old not in _ps_list("extractorModes")
    assert old not in _guide_modes()


def _menu_block(text: str, start: str, end: str) -> str:
    assert start in text, start
    return text.split(start, 1)[1].split(end, 1)[0]


# (installer, block start, block end, offered line, chosen line, Enter line)
_SH_MENU = (r'(?m)^\s*echo "  (\d)\) ([\w.-]+)',
            r'(?m)^\s*(?:""\|)?(\d)\) (?:MODEL|EXTRACTOR)=([\w.-]+) ;;',
            r'(?m)^\s*""\|1\) (?:MODEL|EXTRACTOR)=([\w.-]+) ;;')
_PS_MENU = (r'(?m)^\s*Write-Host "  (\d)\) ([\w.-]+)',
            r'(?m)^\s*"(\d)" \{ \$(?:Model|Extractor) = "([\w.-]+)" \}',
            r'(?m)^\s*\{ \$_ -in "", "1" \} \{ \$(?:Model|Extractor) = "([\w.-]+)" \}')
_MENUS = [
    ("ops/install.sh", _SH_MENU, 'echo "Which dream extractor', "\nfi\n", False),
    ("ops/install.sh", _SH_MENU, 'echo "Which Claude model', 'step "Dreamer model', True),
    ("ops/install.sh", _SH_MENU, 'echo "Which GPT model', 'step "Dreamer model', True),
    ("ops/install.ps1", _PS_MENU, 'Write-Host "Which dream extractor', "\n}\n", False),
    ("ops/install.ps1", _PS_MENU, 'Write-Host "Which Claude model', 'Step "Dreamer model', True),
    ("ops/install.ps1", _PS_MENU, 'Write-Host "Which GPT model', 'Step "Dreamer model', True),
]


@pytest.mark.parametrize("rel,patterns,start,end,enter", _MENUS,
                         ids=lambda v: v if isinstance(v, str) and "Which" in v else None)
def test_each_menu_number_picks_the_name_it_shows(rel, patterns, start, end, enter):
    """A menu line `N) name` and the case arm for N must name the same thing,
    and Enter must pick entry 1, so reordering a menu cannot leave the
    number a user types pointing at a different model."""
    offered_pat, chosen_pat, enter_pat = patterns
    block = _menu_block(_read(rel).replace("\r\n", "\n"), start, end)
    offered = dict(re.findall(offered_pat, block))
    chosen = dict(re.findall(chosen_pat, block))
    if enter:
        [pick] = re.findall(enter_pat, block)
        chosen["1"] = pick
    assert offered and offered == chosen, (rel, start, offered, chosen)
