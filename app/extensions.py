from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# In-memory counters are per-process: fine for a single waitress/dev process. Behind a reverse
# proxy, configure the proxy to pass the real client IP (and set RATELIMIT_STORAGE_URI to a
# shared store if running several workers).
limiter = Limiter(key_func=get_remote_address)
