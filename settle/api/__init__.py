"""The three FastAPI routes. SPEC §16.

`POST /webhooks/razorpay` is the only one with behaviour this checkpoint;
`POST /voice/extract` and `GET /` are declared and return 501 so the route table
is the one SPEC §16 fixes rather than one that grows to fit.
"""
