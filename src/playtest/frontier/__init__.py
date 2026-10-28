"""Frontier escalation transport (#33): one packet and the Muse provider.

The packet exposes measured records and executable choices. It never ranks, recommends or
substitutes an action, and it never carries S1's rationale or distribution. The provider sends
one schema-constrained request and decodes the answer without repairing it.
"""

from __future__ import annotations

from .packet import Packet, build_packet
from .provider import MODEL, URL, Result, build_request, decode_response, redact_media, send_meta

__all__ = [
    "MODEL",
    "URL",
    "Packet",
    "Result",
    "build_packet",
    "build_request",
    "decode_response",
    "redact_media",
    "send_meta",
]
