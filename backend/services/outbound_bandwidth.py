import sys
from backend.services import outbound_bandwidth_service as _impl
sys.modules[__name__] = _impl