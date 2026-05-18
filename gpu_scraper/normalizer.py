"""GPU name normalisation, VRAM lookup, and region canonicalisation."""
from __future__ import annotations

import re

# Canonical GPU names → VRAM in GB
VRAM_MAP: dict[str, int] = {
    "H200 SXM":         141,
    "H200 NVL":         141,
    "H200":             141,
    "H100 SXM":          80,
    "H100 NVL":          94,
    "H100 PCIe":         80,
    "H100":              80,
    "A100 80GB SXM":     80,
    "A100 80GB PCIe":    80,
    "A100 80GB":         80,
    "A100 40GB SXM":     40,
    "A100 40GB PCIe":    40,
    "A100 40GB":         40,
    "A100":              80,
    "A10G":              24,
    "A10":               24,
    "A40":               48,
    "A6000":             48,
    "RTX 6000 Ada":      48,
    "L40S":              48,
    "L40":               48,
    "L4":                24,
    "V100 32GB SXM":     32,
    "V100 32GB":         32,
    "V100 16GB":         16,
    "V100":              16,
    "T4":                16,
    "K80":               12,
    "P100":              16,
    "RTX 4090":          24,
    "RTX 4080":          16,
    "RTX 3090":          24,
    "RTX 3080":          10,
    "MI300X":           192,
    "MI355X":           288,
    "MI250X":           128,
    "MI210":             64,
    "B300":             288,
    "B200":             180,
    "GB200":            192,
    "GB300":            288,
}

# Raw name fragments → canonical key (first match wins; more-specific first)
_RAW_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # H200 variants
    (re.compile(r"h200.*nvl",           re.I), "H200 NVL"),
    (re.compile(r"h200.*sxm",           re.I), "H200 SXM"),
    (re.compile(r"h200",                re.I), "H200 SXM"),
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
    # Other data-centre GPUs
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
    # Consumer GPUs
    (re.compile(r"4090",                re.I), "RTX 4090"),
    (re.compile(r"4080",                re.I), "RTX 4080"),
    (re.compile(r"3090",                re.I), "RTX 3090"),
    (re.compile(r"3080",                re.I), "RTX 3080"),
    # AMD
    (re.compile(r"mi355x",              re.I), "MI355X"),
    (re.compile(r"mi300x",              re.I), "MI300X"),
    (re.compile(r"mi250x",              re.I), "MI250X"),
    (re.compile(r"mi210",               re.I), "MI210"),
    # Next-gen NVIDIA
    (re.compile(r"gb300",               re.I), "GB300"),
    (re.compile(r"gb200",               re.I), "GB200"),
    (re.compile(r"\bb300\b",            re.I), "B300"),
    (re.compile(r"\bb200\b",            re.I), "B200"),
]

# GPU models we care about for default filtering
TARGET_GPU_FAMILIES = {"H100", "A100", "H200", "B200", "B300"}


# ---------------------------------------------------------------------------
# Region canonicalisation: provider-specific → canonical bucket
# ---------------------------------------------------------------------------
# Canonical format: "<continent>-<direction>", e.g. "us-east", "eu-west"
# Special values: "global", "unknown"

_REGION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # US — explicit Azure-style compound names first (no separator)
    (re.compile(r"^eastus\d*$", re.I), "us-east"),
    (re.compile(r"^westus\d*$", re.I), "us-west"),
    (re.compile(r"^centralus$", re.I), "us-central"),
    (re.compile(r"^southcentralus$", re.I), "us-south"),
    (re.compile(r"^northcentralus$", re.I), "us-central"),
    # US — general patterns
    (re.compile(r"us.?(east|ashburn|virginia|ohio|n\.virginia|us-east)", re.I), "us-east"),
    (re.compile(r"us.?(west|oregon|california|nevada|phoenix|seattle|us-west)", re.I), "us-west"),
    (re.compile(r"us.?(central|iowa|dallas|texas|chicago|us-central)", re.I), "us-central"),
    (re.compile(r"us.?(south|atlanta|miami|houston)", re.I), "us-south"),
    (re.compile(r"virginia|ohio|ashburn|n\.virginia|new.?york", re.I), "us-east"),
    (re.compile(r"\boregon\b|\bseattle\b|\bphoenix\b|\blas.?vegas\b", re.I), "us-west"),
    (re.compile(r"^(us|united states|usa)$", re.I), "us-east"),  # bare "US"
    (re.compile(r"united states", re.I), "us-east"),
    # Canada
    (re.compile(r"canada|toronto|montreal|ca-central", re.I), "ca-central"),
    # Europe
    (re.compile(r"eu.?(west|ireland|london|amsterdam|paris|belgium)", re.I), "eu-west"),
    (re.compile(r"eu.?(north|sweden|finland|stockholm)", re.I), "eu-north"),
    (re.compile(r"eu.?(central|frankfurt|munich|prague|zurich|warsaw)", re.I), "eu-central"),
    (re.compile(r"(europe|eu)(?!.*north|central)", re.I), "eu-west"),
    (re.compile(r"(uk|london|ireland|amsterdam|paris|france|germany|czech)", re.I), "eu-west"),
    # Asia-Pacific
    (re.compile(r"(ap|asia).?(northeast|japan|tokyo|korea|seoul)", re.I), "ap-northeast"),
    (re.compile(r"(ap|asia).?(southeast|singapore|indonesia|thailand)", re.I), "ap-southeast"),
    (re.compile(r"(ap|asia).?(south|india|mumbai|hyderabad|bangalore)", re.I), "ap-south"),
    (re.compile(r"australia|sydney|melbourne", re.I), "ap-southeast"),
    (re.compile(r"japan|tokyo|osaka", re.I), "ap-northeast"),
    (re.compile(r"korea|seoul", re.I), "ap-northeast"),
    (re.compile(r"singapore", re.I), "ap-southeast"),
    (re.compile(r"india|mumbai|bangalore|hyderabad", re.I), "ap-south"),
    # Middle East / Africa
    (re.compile(r"me-|middle.?east|uae|dubai|bahrain|israel|tel.?aviv", re.I), "me-central"),
    # Latin America
    (re.compile(r"brazil|sao.?paulo|sa-east", re.I), "sa-east"),
    # Global / catch-all
    (re.compile(r"global", re.I), "global"),
]


def canonicalize_region(region: str) -> str:
    """Map a provider-specific region string to a canonical bucket.

    Returns one of: us-east, us-west, us-central, us-south, ca-central,
    eu-west, eu-north, eu-central, ap-northeast, ap-southeast, ap-south,
    me-central, sa-east, global, unknown.
    """
    if not region or region.strip().lower() in ("", "unknown", "n/a"):
        return "unknown"
    s = region.strip()
    for pattern, canonical in _REGION_PATTERNS:
        if pattern.search(s):
            return canonical
    return "unknown"


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
    for key, vram in VRAM_MAP.items():
        if key.lower() in canonical.lower():
            return vram
    m = re.search(r"(\d+)\s*gb", canonical, re.I)
    if m:
        return int(m.group(1))
    return 0


def is_target_gpu(canonical: str) -> bool:
    return any(f in canonical for f in TARGET_GPU_FAMILIES)
