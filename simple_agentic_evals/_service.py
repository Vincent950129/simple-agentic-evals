"""Service endpoint baked into this SDK build.

This keeps the service URL on the *server* side rather than in user code: it ships
with the SDK so ``EvalClient()`` -- with no arguments -- connects to the hosted
evaluation service out of the box. Callers just ``pip install`` the SDK and use it;
they never handle (or even see) the link.

Resolution order in :class:`EvalClient` is: explicit ``base_url`` argument ->
``$EVAL_SERVICE_URL`` -> this ``DEFAULT_BASE_URL`` -> ``http://localhost:8077``. So a
developer can still point at a local service by setting ``$EVAL_SERVICE_URL`` (or
passing ``base_url=``) without touching this file. The service can also override this
value at wheel-build time via ``$EVAL_SERVICE_PUBLIC_URL`` (see ``_build_sdk_wheel``).
"""

DEFAULT_BASE_URL = "https://idealist-unwritten-astronaut.ngrok-free.dev"
