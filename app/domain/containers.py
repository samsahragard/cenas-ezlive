from __future__ import annotations

import math
from collections import Counter

CONTAINER_SIZES = [
    ("half gallon", 64),
    ("quart", 32),
    ("pint", 16),
    ("half pint", 8),
]

def container_for_oz(total_oz: float) -> str:
    """single best-fit container for the given oz."""
    if total_oz <= 0:
        return "none"
    if total_oz <= 8:
        return "half pint"
    if total_oz <= 16:
        return "pint"
    if total_oz <= 32:
        return "quart"
    if total_oz <= 64:
        return "half gallon"
    return containers_for_oz(total_oz)

def containers_for_oz(total_oz: float) -> str:
    """Returns the minimum number of containers needed to hold total_oz."""
    if total_oz <= 0:
        return "none"
    
    n = math.ceil(total_oz / 64)
    chosen = []
    remaining = total_oz

    for i in range(n):
        if i < n - 1:
            chosen.append("half gallon")
            remaining -= 64
        else:
            for name, capacity in reversed(CONTAINER_SIZES):
                if remaining <= capacity:
                    chosen.append(name)
                    break
                
    counts = Counter(chosen)
    ordered = [f'{counts[name]}x {name}' for name, _ in CONTAINER_SIZES if name in counts]
    return " + ".join(ordered) if ordered else "none"