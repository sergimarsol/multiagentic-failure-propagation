"""Local exact-request judge response cache. No network, credentials, or tokenization."""
import json
import os
from pathlib import Path
import tempfile
from datetime import datetime, timezone

from .models import fingerprint

CACHE_VERSION = "judge-exact-request-v1"


def request_identity(messages, config):
    # Includes exact message strings (including order/whitespace), endpoint and all
    # public settings. Transport v1 is B's complete(..., [], json_output=True).
    return {"version": CACHE_VERSION, "transport": "OpenAICompatibleClient.complete-v1",
            "messages": messages, "config": config, "tools": [], "json_output": True}


class JudgeCache:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def get(self, request):
        key = fingerprint(request)
        try:
            record = json.loads((self.directory / (key + '.json')).read_text())
            if (record['request'] != request or record['key'] != key
                    or record['response_hash'] != fingerprint(record['response'])):
                return None
            return record
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def put(self, request, response, *, origin=None):
        key = fingerprint(request)
        value = {"key": key, "request": request, "response": response,
                 "response_hash": fingerprint(response), "origin": origin,
                 "created_utc": datetime.now(timezone.utc).isoformat()}
        # First writer wins, so concurrent exact requests cannot replace an earlier
        # estimate. Corrupt entries are treated as misses, never silently repaired.
        fd, temp = tempfile.mkstemp(prefix='.entry-', dir=self.directory)
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(value, f, ensure_ascii=False, allow_nan=False)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.link(temp, self.directory / (key + '.json'))
                return True
            except FileExistsError:
                return False
        finally:
            Path(temp).unlink(missing_ok=True)


def seed_cache(cache, analysis):
    """Import only validated successful responses with recorded public model settings."""
    from .work_judgments import validate_judgment
    from .judge_evidence import expand_packet
    root = Path(analysis)
    reports = [root / 'judgments.json'] if (root / 'judgments.json').is_file() else sorted(root.glob('run_*/judgments.json'))
    counts = {'imported': 0, 'existing': 0, 'skipped': 0}
    if not reports:
        raise ValueError('no saved judgment reports found')
    for path in reports:
        report = json.loads(path.read_text())
        config = report.get('judge_config')
        if not config or config.get('plan_format') != 'json_object':
            raise ValueError('saved judge settings are missing or incompatible')
        for call in json.loads((path.parent / 'judge_calls.json').read_text()):
            try:
                if call['status'] != 'success':
                    counts['skipped'] += 1
                    continue
                messages = call['messages']
                packet = json.loads(messages[1]['content'])
                if fingerprint(packet) != call.get('request_packet_hash', call['packet_hash']):
                    raise ValueError('packet hash mismatch')
                response = call['response']
                choice = response['choices'][0]
                if choice['finish_reason'] != 'stop':
                    raise ValueError('incomplete response')
                validate_judgment(json.loads(choice['message']['content']), expand_packet(packet))
                identity = request_identity(messages, config)
                added = cache.put(identity, response, origin=str(path.parent / 'judge_calls.json'))
                counts['imported' if added else 'existing'] += 1
            except (KeyError, IndexError, TypeError, ValueError):
                counts['skipped'] += 1
    return counts
