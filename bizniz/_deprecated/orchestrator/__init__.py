"""Deprecated v1 orchestrator. See ``bizniz/_deprecated/README.md``.

Empty __init__: load specific submodules directly. The internal
imports across this package reference the pre-v2-cut paths and
loading them all from __init__ would crash. New code should not
import from here at all; the only legitimate consumer right now is
``BiznizConfig`` reaching for ``ModelProgression`` until the v2
ServiceImplementer makes that obsolete.
"""
