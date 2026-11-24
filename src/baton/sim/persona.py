"""The simulated customer: an LLM playing a scenario's persona.

It has its own system prompt (`prompts/persona.md`) and never sees the agent's prompt, tools or
traces: only what the agent wrote to it, exactly like a real customer. Roles are mirrored for
its model: the agent's replies are the `user` messages and the persona's own lines are the
`assistant` messages.

The persona ends the conversation by writing `[DONE]` (optionally after a closing line).
"""

from dataclasses import dataclass, field
from pathlib import Path

from baton.llm.client import LLMClient
from baton.llm.types import Msg
from baton.prompts import Prompt, load_prompt
from baton.sim.pricing import usage_of
from baton.sim.results import Usage
from baton.sim.suite import Persona

PERSONA_PROMPT = "persona"
DONE = "[DONE]"
OPENING_CUE = "(The chat window is open. Write your first message to the assistant.)"
SILENT_CUE = "(The assistant hasn't replied: a human teammate will take over.)"


@dataclass(frozen=True)
class PersonaLine:
    text: str  # what the customer sends; empty when they ended the conversation
    done: bool
    closing: str = ""  # anything written alongside [DONE]; it isn't sent to the agent
    usage: Usage = field(default_factory=Usage)


def render_system(prompt: Prompt, persona: Persona) -> str:
    knows = "\n".join(f"- {k}: {v}" for k, v in persona.knows.items()) or "- nothing specific"
    return prompt.render(
        name=persona.name,
        traits=", ".join(persona.traits) or "an ordinary customer",
        language=persona.language or "English",
        goal=persona.goal,
        knows=knows,
        pressure=persona.pressure or "you accept a clear, polite answer",
    )


def parse_line(raw: str) -> tuple[str, bool, str]:
    """(message to send, done, closing words). Strips quotes a model sometimes adds."""
    text = raw.strip().strip('"').strip()
    if DONE in text:
        closing = text.replace(DONE, "").strip()
        return "", True, closing
    return text, False, ""


class PersonaPlayer:
    def __init__(self, llm: LLMClient, persona: Persona, prompts_dir: Path | None = None) -> None:
        self.llm = llm
        self.persona = persona
        self.prompt = load_prompt(PERSONA_PROMPT, prompts_dir)
        self.system = render_system(self.prompt, persona)
        self._messages: list[Msg] = []

    @property
    def version(self) -> str:
        return self.prompt.version

    def first(self) -> PersonaLine:
        if self.persona.opening:
            self._messages = [
                Msg(role="user", content=OPENING_CUE),
                Msg(role="assistant", content=self.persona.opening),
            ]
            return PersonaLine(text=self.persona.opening, done=False)
        self._messages = [Msg(role="user", content=OPENING_CUE)]
        return self._speak()

    def reply_to(self, agent_text: str) -> PersonaLine:
        self._messages.append(Msg(role="user", content=agent_text.strip() or SILENT_CUE))
        return self._speak()

    def _speak(self) -> PersonaLine:
        resp = self.llm.complete(self.system, list(self._messages), None, max_tokens=1000)
        text, done, closing = parse_line(resp.text)
        if not done and not text:
            # An empty message is no use to anyone; treat it as the customer leaving.
            done = True
        self._messages.append(Msg(role="assistant", content=resp.text.strip() or DONE))
        return PersonaLine(
            text=text, done=done, closing=closing, usage=usage_of(resp.model, resp.usage)
        )
