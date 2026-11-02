"""Every prompt sent to a model, as Python constants. Edit the text here; no other code needs to change.

  vision.py  READ_SPINE, DESCRIBE_ITEM   used by backend/adapters/openai_vision.py
  agent.py   SYSTEM                              used by backend/live/agent.py instructions() (the voice agent's rules)
  coach.py   GUIDE_ASKS, BOOK_ASKS, ...           used by backend/live/guide.py (what the agent says to guide the sweep)

Placeholders are $name / ${name} (string.Template), so JSON braces like {title, author} need no escaping.
Fill them with `fill(PROMPT, name=value)`; a placeholder without a value raises KeyError, so a half-filled
prompt is never sent. Restart the server after editing a prompt.

Vision responses are cached by prompt text, so changing a vision prompt makes the next run call the model again.
Tool names and argument descriptions stay in backend/live/voice_pipecat.py (make_tools): they are part of the executed schema.
"""
from string import Template


def fill(prompt: str, **values) -> str:
    return Template(prompt).substitute(values)
