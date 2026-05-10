"""GPU name normalisation and VRAM lookup."""
from __future__ import annotations

import re

# Canonical GPU names → VRAM in GB
VRAM_MAP: dict[str, int] = {
    "H100 SXM":         80,
    "H100 NVL":         94,
    "H100 PCIe":        80,
    "H100":             80,
    "A100 80GB SXM":    80,
    "A100 80GB PCIe":   80,
    "A100 80GB":        80,
    "A100 40GB SXM":    40,
    "A100 40GB PCIe":   40,
    "A100 40GB":        40,
    "A100":             80,
    "A10G":             24,
    "A10":              24,
    "A40":              48,
    "A6000":            48,
    "RTX 6000 Ada":     48,
    "L40S":             48,
    "L40":              48,
    "L4":               24,
    "V100 32GB SXM":    32,
    "V100 32GB":        32,
    "V100 16GB":        16,
    "V100":             16,
    "T4":               16,
    "K80":              12,
    "P100":             16,
    "RTX 4090":         24,
    "RTX 4080":         16,
    "RTX 3090":         24,
    "RTX 3080":         10,
    "MI300X":          192,
    "MI250X":          128,
    "MI210":            64,
}

# Raw name fragments → canonical key (longest match wins)
_RAW_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # H100 variants
    (re.compile(r"h100.*nvl",           re.I), "H100 NVL"),
    (re.compile(r"h100.*sxm",           re.I), "H100 SXM"),
    (re.compile(r"h100.*pcie",          re.I), "H100 PCIe"),
    (re.compile(r"h100",                re.I), "H100 SXM"),
    # A100 variants
    (re.compile(r"a100.*80.*sxm|a100.*sxm.*80", re.I), "A100 80GB SXM"),
    (re.compile(r"a100.*80.*pcie|a100.*pcie.*80", re.I), "A100 80GB PCIe"),
    (re.compile(r"a100.*80",            re.I), "A100 80GB"),
    (re.compile(r"a100.*40.*sxm|a100.*sxm.*40", re.I), "A100 40GB SXM"),
    (re.compile(r"a100.*40.*pcie|a100.*pcie.*40", re.I), "A100 40GB PCIe"),
    (re.compile(r"a100.*40",            re.I), "A100 40GB"),
    (re.compile(r"a100",                re.I), "A100 80GB"),
    # Other accelerators
    (re.compile(r"a10g",                re.I), "A10G"),
    (re.compile(r"\ba10\b",             re.I), "A10"),
    (re.compile(r"\ba40\b",             re.I), "A40"),
    (re.compile(r"a6000",               re.I), "A6000"),
    (re.compile(r"6000 ada|rtx.*6000",  re.I), "RTX 6000 Ada"),
    (re.compile(r"l40s",                re.I), "L40S"),
    (re.compile(r"\bl40\b",             re.I), "L40"),
    (re.compile(r"\bl4\b",              re.I), "L4"),
    (re.compile(r"v100.*32",            re.I), "V100 32GB"),
    (re.compile(r"v100.*16",            re.I), "V100 16GB"),
    (re.compile(r"v100",                re.I), "V100 16GB"),
    (re.compile(r"\bt4\b",              re.I), "T4"),
    (re.compile(r"\bk80\b",             re.I), "K80"),
    (re.compile(r"\bp100\b",            re.I), "P100"),
    (re.compile(r"4090",                re.I), "RTX 4090"),
    (re.compile(r"4080",                re.I), "RTX 4080"),
    (re.compile(r"3090",                re.I), "RTX 3090"),
    (re.compile(r"3080",                re.I), "RTX 3080"),
    (re.compile(r"mi300x",              re.I), "MI300X"),
    (re.compile(r"mi250x",              re.I), "MI250X"),
    (re.compile(r"mi210",               re.I), "MI210"),
]

# GPU models we care about for filtering (None = accept all)
TARGET_GPU_FAMILIES = {"H100", "A100"}


def normalize_gpu_name(raw: str) -> str:
    """Map a raw provider GPU string to a canonical name."""
    raw = raw.strip()
    for pattern, canonical in _RAW_PATTERNS:
        if pattern.search(raw):
            return canonical
    return raw


def lookup_vram(canonical: str) -> int:
    """Return VRAM in GB for a canonical GPU name, 0 if unknown."""
    if canonical in VRAM_MAP:
        return VRAM_MAP[canonical]
    # Fallback: check if any key is a substring
    for key, vram in VRAM_MAP.items():
        if key.lower() in canonical.lower():
            return vram
    # Try to extract GB figure from name itself
    m = re.search(r"(\d+)\s*gb", canonical, re.I)
    if m:
        return int(m.group(1))
    return 0


def is_target_gpu(canonical: str) -> bool:
    """True if this GPU belongs to a family we track (H100 or A100)."""
    return any(f in canonical for f in TARGET_GPU_FAMILIES)
