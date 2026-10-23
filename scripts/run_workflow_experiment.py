"""D configured entry point; executes B unchanged. No model service is started."""
import argparse
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments.workflow_bridge import workflow_batch, summarize_workflow_runs


def main():
    p = argparse.ArgumentParser(description=__doc__)
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument('--config', type=Path, help='JSON list of D experiment configs')
    source.add_argument('--summarize', type=Path, nargs='+', help='Configured run directories; originals remain untouched')
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--api-key-env', default='OPENAI_COMPATIBLE_API_KEY')
    p.add_argument('--scripted', action='store_true', help='Explicit development fixture; requires synthetic=true')
    a = p.parse_args()
    if a.summarize:
        rows = summarize_workflow_runs(a.summarize, a.output)
    else:
        configs = json.loads(a.config.read_text())
        factory = None
        if a.scripted:
            if any(c.get('synthetic') is not True for c in configs):
                p.error('--scripted requires synthetic=true for all configs')
            # Reuse B's existing dev client; this mode requires the tests checkout.
            from tests.test_workflow import FakeModel
            def factory(model_config):
                client = FakeModel()
                client.config = model_config
                return client
        rows = workflow_batch(configs, a.output, client_factory=factory,
                              api_key=os.environ.get(a.api_key_env, 'ollama'))
    return int(any(r['error'] or not r['summary'].get('trace_complete') or
                   r['summary'].get('execution_errors') for r in rows))


if __name__ == '__main__':
    raise SystemExit(main())
