"""The FastAPI application. Three routes, exactly the three in SPEC §16.

    POST /webhooks/razorpay   signature-verified webhook receiver
    POST /voice/extract       upload audio, returns full extraction trace
    GET  /                    serves the static viewer

Two of them are stubs returning 501 this checkpoint. They are declared anyway,
because a route table that grows to fit whatever gets built is a route table that
stops being a contract — and 501 Not Implemented is the honest status for a route
that exists and does not work yet, where 404 would claim it does not exist.

Run it:

    uvicorn settle.api.app:app --port 8000
"""

from typing import Final

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from settle.api.webhook import router as webhook_router

__all__ = ["app"]

NOT_IMPLEMENTED: Final[int] = 501

app = FastAPI(
    title="settle",
    description=(
        "A recovery agent that is correct when it cannot trust what it is told "
        "about outcomes. Simulated at scale, real at the edges."
    ),
    version="0.12.0",
)

app.include_router(webhook_router)


@app.post("/voice/extract")
async def voice_extract() -> JSONResponse:
    """Stub. The extraction trace lands with the voice lab in D5."""
    return JSONResponse(
        {
            "reason_code": "NOT_IMPLEMENTED",
            "detail": "POST /voice/extract is declared in SPEC §16 and arrives with the voice lab.",
        },
        status_code=NOT_IMPLEMENTED,
    )


@app.get("/")
async def viewer() -> JSONResponse:
    """Stub. `viewer/index.html` is a single hand-written file, built in D5."""
    return JSONResponse(
        {
            "reason_code": "NOT_IMPLEMENTED",
            "detail": "GET / serves viewer/index.html, which arrives in D5.",
        },
        status_code=NOT_IMPLEMENTED,
    )
