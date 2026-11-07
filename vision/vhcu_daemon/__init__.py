"""VHCU Python daemon (P0).

The daemon owns capture + local diffing. It speaks the line protocol on
stdin/stdout and exposes an authenticated loopback image plane for previews.
"""

__version__ = "0.1.0"
PROTOCOL_VERSION = 1
