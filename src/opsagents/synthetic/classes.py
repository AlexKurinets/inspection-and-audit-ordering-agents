"""A made-up classification manual: codes, descriptions, hazard grade and rates per $100 of payroll.

The codes are invented for this repo and do not follow any real rating bureau's scheme.
"""

# code: (description, hazard, WC rate, GL rate)
CLASS_MANUAL: dict[str, tuple[str, str, float, float]] = {
    "C-210": ("Roofing, residential", "high", 18.50, 6.10),
    "C-215": ("Roofing, commercial", "high", 16.20, 5.40),
    "C-320": ("Carpentry, residential", "standard", 9.80, 3.20),
    "C-330": ("Drywall installation", "standard", 7.40, 2.40),
    "C-410": ("Electrical wiring", "standard", 4.60, 1.90),
    "C-420": ("Plumbing", "standard", 5.10, 2.10),
    "C-510": ("Landscaping", "standard", 6.20, 2.30),
    "C-520": ("Tree trimming", "high", 14.30, 4.80),
    "C-610": ("Demolition", "high", 21.00, 7.20),
    "C-710": ("Painting", "standard", 6.90, 2.50),
    "C-901": ("Clerical office", "standard", 0.35, 0.20),
    "C-905": ("Outside sales", "standard", 0.60, 0.30),
}

TRADES = ["C-210", "C-215", "C-320", "C-330", "C-410", "C-420", "C-510", "C-520", "C-610", "C-710"]
TRADE_WEIGHTS = [10, 4, 16, 9, 12, 12, 12, 5, 3, 17]


def description(code: str) -> str:
    return CLASS_MANUAL[code][0]
