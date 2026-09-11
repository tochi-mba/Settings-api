"""Hand-written fakes that satisfy the real protocols.

There is no ``unittest.mock`` in this suite. A fake that satisfies the real shape fails to
type-check when the shape changes; a patched attribute does not.
"""
