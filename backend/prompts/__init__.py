"""Every prompt sent to a model, as Python constants. Edit the text here; no other code needs to change.

  vision.py  DETECT, READ_SPINE, DESCRIBE_ITEM   used by backend/adapters/openai_vision.py
  agent.py   SYSTEM                              used by backend/live/agent.py (the voice agent's rules)
  coach.py   ASKS                                used by backend/live/coach.py (what the agent says per capture problem)

Placeholders are $name / ${name} (string.Template), so JSON braces like {title, author} need no escaping.
Fill them with `fill(PROMPT, name=value)`; a placeholder without a value raises KeyError, so a half-filled
prompt is never sent. Restart the server after editing a prompt.

Vision responses are cached by prompt text, so changing a vision prompt makes the next run call the model again.
Tool names and argument descriptions stay in backend/live/agent.py (TOOLS): they are part of the executed schema.
"""
from string import Template


def fill(prompt: str, **values) -> str:
    return Template(prompt).substitute(values)
