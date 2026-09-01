"""The Razorpay edge. SPEC §16.

One client, one singleton, credentials from the environment and nowhere else.

Real vs synthetic, enforced by the type
--------------------------------------
Every `PaymentLinkRecord` carries a `source`, and the model validates the pairing
rather than trusting the caller to be careful:

    RAZORPAY_TEST_MODE   a real object in Razorpay's test mode. `plink_...`,
                         resolvable `short_url`, exists in the dashboard.
    MOCK_SANDBOX         constructed locally. `MOCK_plink_...`, and a `short_url`
                         on the reserved `.invalid` TLD (RFC 2606) which is
                         guaranteed never to resolve.

A mock link must never be presentable as a real one, so the difference is visible
in the id itself and not only in a field somebody has to remember to read. Pasting
a mock id into the Razorpay dashboard finds nothing; clicking a mock `short_url`
fails to resolve. Both failures are loud, which is the point — the quiet version
of this mistake is a demo that shows a synthetic id and calls it a payment.

Mock mode is the default
------------------------
`RAZORPAY_MOCK_MODE` defaults to true, so the repo runs for anyone who clones it
without credentials. A judge with no keys gets a working demo; a judge with keys
gets the real thing by setting one variable. The failure mode we refuse is the
opposite default, where a missing key produces a plausible-looking id.

Live keys are refused outright. This project moves no money.
"""

import hashlib
import os
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Final

import razorpay
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "LinkSource",
    "PaymentLinkRecord",
    "RazorpayClient",
    "RazorpayCredentialsMissing",
    "RazorpayLiveModeRefused",
    "get_client",
    "reset_client",
    "scrub",
]

# The id prefix a synthetic record wears. Razorpay ids are `plink_` plus 14
# base62 characters and never contain an underscore after the prefix, so this
# cannot collide with a real one and cannot be mistaken for one by eye.
MOCK_ID_PREFIX: Final[str] = "MOCK_plink_"

# RFC 2606 reserves `.invalid` precisely so it can never resolve. A mock link is
# therefore not merely labelled un-clickable, it is un-clickable.
MOCK_URL_HOST: Final[str] = "mock-sandbox.invalid"

TEST_KEY_PREFIX: Final[str] = "rzp_test_"
LIVE_KEY_PREFIX: Final[str] = "rzp_live_"

REDACTED: Final[str] = "<redacted>"


class LinkSource(str, Enum):
    """Where a payment link record came from. Never inferred, always recorded."""

    RAZORPAY_TEST_MODE = "RAZORPAY_TEST_MODE"
    MOCK_SANDBOX = "MOCK_SANDBOX"


class RazorpayCredentialsMissing(RuntimeError):
    """Real mode was asked for and the environment cannot supply it."""


class RazorpayLiveModeRefused(RuntimeError):
    """A live key was supplied. `settle` moves no money; there is no live path."""


class PaymentLinkRecord(BaseModel):
    """One payment link, real or synthetic, with its provenance attached.

    Frozen and `extra="forbid"` for the same reason as `settle/schema/`: a record
    that can be edited after the fact is a record that can be relabelled.
    """

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    short_url: str = Field(min_length=1)
    amount_paise: int = Field(ge=100)
    description: str
    notes: dict[str, str]
    status: str = Field(min_length=1)
    created_at: AwareDatetime
    source: LinkSource

    @model_validator(mode="after")
    def _source_matches_shape(self) -> "PaymentLinkRecord":
        """The label and the object must agree.

        This is the whole discipline in one place. A record claiming
        RAZORPAY_TEST_MODE while carrying a locally-minted id is the failure this
        package exists to make impossible, so it is a validation error rather
        than a convention.
        """
        if "case_id" not in self.notes or not self.notes["case_id"]:
            raise ValueError(
                "notes must carry case_id — it is the only join back from a "
                "webhook to the case that caused the link (SPEC §16)"
            )

        if self.source is LinkSource.MOCK_SANDBOX:
            if not self.id.startswith(MOCK_ID_PREFIX):
                raise ValueError(f"a MOCK_SANDBOX id must start with {MOCK_ID_PREFIX!r}")
            if MOCK_URL_HOST not in self.short_url:
                raise ValueError(f"a MOCK_SANDBOX short_url must sit on {MOCK_URL_HOST!r}")
        else:
            if not self.id.startswith("plink_"):
                raise ValueError("a RAZORPAY_TEST_MODE id must start with 'plink_'")
            if self.id.startswith(MOCK_ID_PREFIX) or MOCK_URL_HOST in self.short_url:
                raise ValueError("a mock record may not be labelled RAZORPAY_TEST_MODE")
        return self

    @property
    def case_id(self) -> str:
        """The case this link was raised for. Guaranteed present by validation."""
        return self.notes["case_id"]

    @property
    def is_real(self) -> bool:
        """True only for an object that exists in Razorpay's test mode."""
        return self.source is LinkSource.RAZORPAY_TEST_MODE


def scrub(text: str, *secrets: str) -> str:
    """Remove credentials from a string bound for a log, ledger or exception.

    Applied to every message that crosses out of this module. The SDK raises
    exceptions built from the request it made, and that request carries a Basic
    auth header; without this, a 401 from Razorpay writes the secret into a
    traceback.
    """
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return text


class RazorpayClient:
    """The only module in `settle/` permitted to import the razorpay SDK (RZP-3).

    Keeping the SDK behind one seam is what makes the rest of the codebase
    testable without credentials, and what makes "which code can reach
    Razorpay?" a question with a one-line answer.
    """

    __slots__ = ("_mock_mode", "_key_id", "_key_secret", "_sdk")

    def __init__(
        self,
        *,
        mock_mode: bool | None = None,
        key_id: str | None = None,
        key_secret: str | None = None,
    ) -> None:
        self._mock_mode = _mock_mode_from_env() if mock_mode is None else mock_mode
        self._key_id = (key_id if key_id is not None else os.environ.get("RAZORPAY_KEY_ID", "")).strip()
        self._key_secret = (
            key_secret if key_secret is not None else os.environ.get("RAZORPAY_KEY_SECRET", "")
        ).strip()
        self._sdk: razorpay.Client | None = None

        if self._mock_mode:
            return

        if not self._key_id or not self._key_secret:
            raise RazorpayCredentialsMissing(
                "RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET must both be set when "
                "RAZORPAY_MOCK_MODE is false. Set RAZORPAY_MOCK_MODE=true to run "
                "against the local mock instead."
            )
        if self._key_id.startswith(LIVE_KEY_PREFIX):
            raise RazorpayLiveModeRefused(
                "a live key was supplied. settle moves no money and has no live "
                "path; use a rzp_test_ key."
            )
        if not self._key_id.startswith(TEST_KEY_PREFIX):
            raise RazorpayCredentialsMissing(
                f"RAZORPAY_KEY_ID does not start with {TEST_KEY_PREFIX!r}; refusing "
                "to guess what mode it is for."
            )
        self._sdk = razorpay.Client(auth=(self._key_id, self._key_secret))

    # -- provenance --------------------------------------------------------

    @property
    def mock_mode(self) -> bool:
        return self._mock_mode

    @property
    def source(self) -> LinkSource:
        """The label every record this client mints will carry."""
        return LinkSource.MOCK_SANDBOX if self._mock_mode else LinkSource.RAZORPAY_TEST_MODE

    @property
    def key_id_fingerprint(self) -> str:
        """Enough to tell two keys apart in a log, not enough to be one.

        A key id is not a secret in the way the key secret is, but it identifies
        the merchant account, and there is no reason for it to be in a committed
        artefact.
        """
        if not self._key_id:
            return "none"
        return f"{TEST_KEY_PREFIX}…{hashlib.sha256(self._key_id.encode()).hexdigest()[:8]}"

    def __repr__(self) -> str:
        # Explicit, because the default repr of an object holding a secret in a
        # slot is one `print(client)` away from putting it in a terminal capture.
        return f"RazorpayClient(source={self.source.value}, key_id={self.key_id_fingerprint})"

    __str__ = __repr__

    # -- the one operation -------------------------------------------------

    def create_payment_link(
        self,
        *,
        amount_paise: int,
        description: str,
        notes: dict[str, str],
    ) -> PaymentLinkRecord:
        """Raise one payment link for one case.

        `notes` must carry `case_id`. Razorpay echoes notes back on every webhook
        for the link, so it is the only join from an inbound event to the case
        that caused it — SPEC §16's "join on case_id" has no other anchor.
        """
        case_id = notes.get("case_id", "")
        if not case_id:
            raise ValueError(
                "notes must carry case_id: without it an inbound webhook cannot "
                "be joined back to a case (SPEC §16)"
            )

        if self._mock_mode:
            return self._mock_link(amount_paise=amount_paise, description=description, notes=notes)

        assert self._sdk is not None  # constructor guarantees it outside mock mode
        try:
            response: dict[str, Any] = self._sdk.payment_link.create(
                {
                    "amount": amount_paise,
                    "currency": "INR",
                    "description": description,
                    "notes": dict(notes),
                    "reference_id": case_id,
                    "reminder_enable": False,
                }
            )
        except Exception as error:  # noqa: BLE001 — re-raised scrubbed, never swallowed
            raise RuntimeError(
                "razorpay payment_link.create failed: "
                + scrub(f"{type(error).__name__}: {error}", self._key_secret, self._key_id)
            ) from None

        return PaymentLinkRecord(
            id=str(response["id"]),
            short_url=str(response["short_url"]),
            amount_paise=int(response["amount"]),
            description=str(response.get("description") or description),
            notes={str(k): str(v) for k, v in (response.get("notes") or notes).items()},
            status=str(response["status"]),
            created_at=datetime.fromtimestamp(int(response["created_at"]), tz=timezone.utc),
            source=LinkSource.RAZORPAY_TEST_MODE,
        )

    def _mock_link(
        self, *, amount_paise: int, description: str, notes: dict[str, str]
    ) -> PaymentLinkRecord:
        """A synthetic record, deterministic in its inputs.

        Deterministic so a mock demo is reproducible: the same case and amount
        produce the same id on any machine, which is the property that lets the
        no-credentials path be committed as a fixture.
        """
        digest = hashlib.sha256(
            f"{notes['case_id']}|{amount_paise}|{description}".encode()
        ).hexdigest()[:14]
        return PaymentLinkRecord(
            id=f"{MOCK_ID_PREFIX}{digest}",
            short_url=f"https://{MOCK_URL_HOST}/l/{digest}",
            amount_paise=amount_paise,
            description=description,
            notes=dict(notes),
            status="created",
            # Fixed rather than `now()`: a mock record with a wall-clock stamp is
            # not reproducible, and reproducibility is the only thing the mock
            # path has going for it.
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            source=LinkSource.MOCK_SANDBOX,
        )


def _mock_mode_from_env() -> bool:
    """Default true. See the module docstring for why the default runs that way."""
    raw = os.environ.get("RAZORPAY_MOCK_MODE", "true").strip().lower()
    return raw not in {"false", "0", "no", "off"}


_CLIENT: RazorpayClient | None = None


def get_client() -> RazorpayClient:
    """The singleton. One client, built once, from the environment."""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = RazorpayClient()
    return _CLIENT


def reset_client() -> None:
    """Drop the singleton so the next `get_client()` re-reads the environment.

    Exists for tests, which need to cross the mock/real boundary in one process.
    """
    global _CLIENT
    _CLIENT = None
