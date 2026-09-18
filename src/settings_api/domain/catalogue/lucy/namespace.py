"""The one string every entry in this package repeats.

It lives in its own module so that a group module and the package's ``__init__`` can both
read it without the package importing a group module that imports the package back. A
namespace typed twice is a namespace that can be typed wrongly once: ``_assemble`` would
catch it at import, by name, but a constant in one place cannot disagree with itself.
"""

from __future__ import annotations

NAMESPACE = "lucy"
"""Every entry in this package declares this, and the package offers it to the assembler."""
