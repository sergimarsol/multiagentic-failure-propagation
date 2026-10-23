"""Text-only mlx-lm server for local Qwen3.5 VLM-converted checkpoints.

mlx-lm 0.30.7 filters model.visual.* but not MLX Core's vision_tower.*.
Omit only that unused vision tower in memory; keep strict text-weight validation.
No model files or installed packages are modified. This endpoint is text-only.
Pass the normal mlx_lm.server CLI arguments, including a localhost host/port.
"""
from importlib.metadata import version
import logging


def main():
    if version('mlx-lm') != '0.30.7':
        raise RuntimeError('This compatibility shim is validated for mlx-lm 0.30.7 only')
    from mlx_lm.models import qwen3_5, qwen3_5_moe
    for cls in (qwen3_5.Model, qwen3_5_moe.Model):
        original = cls.sanitize
        def text_only(self, weights, original=original):
            kept = {k: v for k, v in weights.items() if not k.startswith('vision_tower.')}
            if len(kept) != len(weights):
                logging.warning('Text-only checkpoint adapter: omitting %s vision tower tensors', len(weights)-len(kept))
            return original(self, kept)
        cls.sanitize = text_only
    from mlx_lm.server import main as serve
    serve()


if __name__ == '__main__': main()
