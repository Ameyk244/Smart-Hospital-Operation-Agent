"""Dev server entrypoint: `python run.py`, instead of `uvicorn app.main:app`
directly.

Why this exists and the CLI doesn't work here: `uvicorn app.main:app` calls
`asyncio.run(self.serve())` *before* it imports the app string — so by the
time `app/main.py`'s own Windows event-loop-policy fix runs (see that
module's comment), uvicorn's event loop already exists under the old
(Proactor) policy, and setting a new policy at that point has no effect on
an already-created loop. Only an entrypoint that sets the policy before
calling into uvicorn at all — this one — can fix it for the served-app case.
`app/main.py`'s own fix still matters separately, for anything that imports
it directly (tests, one-off scripts) without going through uvicorn.
"""

import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import uvicorn  # noqa: E402

if __name__ == "__main__":
    # reload=False deliberately: uvicorn's reload mode runs the actual
    # server in a *subprocess*, which would need this same event-loop-policy
    # fix applied inside itself (a separate Python process, so it doesn't
    # inherit this one's policy) — not worth the added complexity for a dev
    # convenience feature this project doesn't otherwise need.
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)
