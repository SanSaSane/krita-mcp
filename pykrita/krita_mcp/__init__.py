"""Krita MCP bridge -- exposes Krita to an MCP client over loopback HTTP."""

from krita import Krita

from .extension import KritaMcpExtension

Krita.instance().addExtension(KritaMcpExtension(Krita.instance()))
