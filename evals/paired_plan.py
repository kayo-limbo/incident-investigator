"""Experiment controls live here, never in the investigator's evidence."""
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator


class CollectionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    split: Literal["development", "heldout"]
    duration: float = Field(ge=1, le=30, allow_inf_nan=False)
    repeats: int = Field(strict=True, ge=1, le=3)
    cases: dict[str, tuple[tuple[StrictInt, StrictInt, StrictInt], tuple[StrictInt, StrictInt, StrictInt]]]

    @model_validator(mode="after")
    def bounded(self):
        allowed = {"stable", "downstream", "capacity", "arrival", "restart", "compound"}
        if not self.cases or not set(self.cases) <= allowed:
            raise ValueError("unknown_or_empty_cases")
        for name, (a, b) in self.cases.items():
            for delay, pool, rate in (a, b):
                if not (0 <= delay <= 1000 and 1 <= pool <= 100 and 1 <= rate <= 100 and
                        math.ceil(rate * self.duration) <= 1000):
                    raise ValueError("unsafe_plan_parameters")
            changes = (b[0] != a[0], b[1] != a[1], b[2] != a[2])
            expected = {"stable": (False, False, False), "restart": (False, False, False),
                        "downstream": (True, False, False), "capacity": (False, True, False),
                        "arrival": (False, False, True), "compound": (False, True, True)}[name]
            if changes != expected or b[0] < a[0] or b[1] > a[1] or b[2] < a[2]:
                raise ValueError("mechanism_parameter_mismatch")
        return self


def disjoint(development, heldout):
    if development.split != "development" or heldout.split != "heldout":
        raise ValueError("invalid_split_pair")
    if set(development.cases.values()) & set(heldout.cases.values()):
        raise ValueError("overlapping_parameter_pairs")
