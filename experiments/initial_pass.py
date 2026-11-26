"""Share one initial pass among the recovery methods of a scenario and repeat.

The first trial of a group records every model reply of its initial pass. Later trials of
the group replay those replies, and the simulator is deterministic, so every method starts
from the same initial pass: the same fault, or the same refusal before it. Calls made after
the recovery notice always go to the model.
"""
import json
from pathlib import Path
from time import perf_counter

from .models import detached, fingerprint
from .recorder import atomic_json


def group_file(directory, config):
    """One recording per repeat, task, scenario and workflow settings; methods share it."""
    controls = {k: config.get(k) for k in ("task_id", "scenario_id", "repeat_index", "comparison_group_id",
                                           "workflow", "fault", "synthetic")}
    name = f"r{config['repeat_index']}-{config['task_id']}-{config['scenario_id']}-{fingerprint(controls)[:12]}.json"
    return Path(directory) / name


class SharedInitialPass:
    """Model client wrapper: replay the recorded initial pass, then pass calls through."""
    def __init__(self, client, path):
        self.client, self.config, self.path = client, client.config, Path(path)
        self.records = json.loads(self.path.read_text()) if self.path.exists() else []
        self.position, self.active, self.diverged = 0, True, False
        self.replayed = self.live = 0
        self.replayed_ms = 0.0

    def complete(self, messages, tools, *, json_output=False):
        if not self.active:
            return self.client.complete(messages, tools, json_output=json_output)
        request = fingerprint({"messages": messages, "tools": tools, "json_output": json_output})
        if not self.diverged and self.position < len(self.records):
            record = self.records[self.position]
            if record["request"] == request:
                self.position += 1
                self.replayed += 1
                self.replayed_ms += record["latency_ms"]
                return detached(record["response"])
            # A different request: this pass no longer follows the recording. Keep the recording.
            self.diverged = True
        tick = perf_counter()
        response = self.client.complete(messages, tools, json_output=json_output)
        self.live += 1
        if not self.diverged:
            self.records.append({"request": request, "response": detached(response),
                                 "latency_ms": (perf_counter() - tick) * 1000})
            self.position += 1
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_json(self.path, self.records)
        return response

    def end_initial_pass(self):
        """Stop replaying; return what happened, for the trace."""
        self.active = False
        return {"recording": self.path.name, "replayed_calls": self.replayed, "live_calls": self.live,
                "diverged": self.diverged, "replayed_latency_ms": self.replayed_ms}
