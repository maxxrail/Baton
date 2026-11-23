"""Load versioned prompt files from `prompts/`.

Prompts never live as long strings in Python. Code asks for one by name (`load_prompt("router")`
reads `prompts/router.md`) and gets its text plus a version, `router@<8 hex of sha256>`, which
traces record so every model call can be tied to the exact prompt that produced it.

Templates use `string.Template` placeholders (`$name`). Values are inserted verbatim, so text
like "$100" inside a value is safe; a literal dollar sign in the template itself is written `$$`.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from string import Template

from baton.config import get_settings


class PromptNotFoundError(FileNotFoundError):
    pass


@dataclass(frozen=True)
class Prompt:
    name: str
    text: str
    version: str

    def render(self, **values: str) -> str:
        """Fill `$placeholders`. A missing value raises `KeyError` rather than leaking `$x`."""
        return Template(self.text).substitute(values)


def prompt_version(name: str, text: str) -> str:
    return f"{name}@{hashlib.sha256(text.encode('utf-8')).hexdigest()[:8]}"


def load_prompt(name: str, prompts_dir: Path | None = None) -> Prompt:
    directory = prompts_dir or get_settings().prompts_dir
    path = directory / f"{name}.md"
    if not path.is_file():
        raise PromptNotFoundError(f"prompt {name!r} not found at {path}")
    # Normalise line endings so the version is the same on Windows and Linux checkouts.
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    return Prompt(name=name, text=text, version=prompt_version(name, text))
