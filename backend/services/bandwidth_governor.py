from backend.services.outbound_bandwidth_service import *
from backend.services import outbound_bandwidth_service as _impl

# Preserve module identity for monkeypatching in tests.
import sys as _sys
_sys.modules[__name__] = _impl
