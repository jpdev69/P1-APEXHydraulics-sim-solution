import json
from pathlib import Path

from .state import RunState


def dumps(state):
    return json.dumps(state.to_dict(), sort_keys=True, indent=2)


def write_snapshot(state, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps(state) + "\n", encoding="utf-8")


def read_snapshot(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return RunState.from_dict(data)
