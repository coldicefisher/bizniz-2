"""
ModelProgression

Manages an ordered list of models that the orchestrator can escalate through
when code repair stalls.
"""

from typing import List, Optional


DEFAULT_PROGRESSION = ["gpt-4o-mini", "gpt-4o", "gpt-5"]


class ModelProgression:
    """
    Tracks an ordered list of model names and the current position.
    The orchestrator calls escalate() when repair stalls to move to
    a more capable (and more expensive) model.
    """

    def __init__(self, models: Optional[List[str]] = None):
        if models is not None and len(models) == 0:
            raise ValueError("Model progression must contain at least one model.")
        self._models = models if models is not None else list(DEFAULT_PROGRESSION)
        self._index = 0

    @property
    def current_model(self) -> str:
        return self._models[self._index]

    @property
    def is_at_max(self) -> bool:
        return self._index >= len(self._models) - 1

    def escalate(self) -> Optional[str]:
        """
        Move to the next model in the progression.
        Returns the new model name, or None if already at max.
        """
        if self.is_at_max:
            return None
        self._index += 1
        return self._models[self._index]

    def reset(self):
        """Reset to the first (cheapest) model."""
        self._index = 0

    def set_start(self, model_name: str):
        """Set the starting position to a specific model in the progression.
        If model_name is not in the list, does nothing."""
        try:
            self._index = self._models.index(model_name)
        except ValueError:
            pass

    def __repr__(self) -> str:
        return f"ModelProgression({self._models}, current={self.current_model})"
