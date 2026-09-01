"""CP12 — the Razorpay client. SPEC §16.

RZP-1 is the labelling discipline: the default path mints synthetic records, and
a synthetic record can never wear a real record's label. This is the one idea
worth taking from the competing repos in the track — a payment link created is
not revenue recovered, and a mock link is not a payment link.

RZP-2 is the rule that credentials never leave the module. The failure it guards
is mundane and common: the SDK raises an exception built from the request it
made, that request carries a Basic auth header, and the traceback lands in a
committed artefact.

RZP-3 keeps the SDK behind one seam, so "what code can reach Razorpay?" has a
one-line answer.
"""

import ast
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from settle.api import webhook
from settle.integrations import razorpay_client as rzp
from settle.integrations.razorpay_client import (
    LinkSource,
    PaymentLinkRecord,
    RazorpayClient,
    RazorpayCredentialsMissing,
    RazorpayLiveModeRefused,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

FAKE_KEY_ID = "rzp_test_FAKEKEYID12345"
FAKE_KEY_SECRET = "FAKEsecretNOTaREALone123"
FAKE_WEBHOOK_SECRET = "cp12_webhook_secret_not_a_real_one"

NOTES = {"case_id": "case_000123", "arm": "EDGE"}


@pytest.fixture(autouse=True)
def _clean_singleton() -> None:
    rzp.reset_client()
    yield
    rzp.reset_client()


# --------------------------------------------------------------------------
# RZP-1 — mock mode is labelled MOCK_SANDBOX, never TEST_MODE
# --------------------------------------------------------------------------

def test_RZP_1_mock_mode_returns_mock_sandbox_never_test_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default path, and the one a judge with no credentials gets."""
    monkeypatch.delenv("RAZORPAY_MOCK_MODE", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)

    # Mock mode is the default, so the repo runs for anyone who clones it.
    client = rzp.get_client()
    assert client.mock_mode is True
    assert client.source is LinkSource.MOCK_SANDBOX

    record = client.create_payment_link(
        amount_paise=249900, description="settle CP12 demo", notes=dict(NOTES)
    )
    assert record.source is LinkSource.MOCK_SANDBOX
    assert record.source is not LinkSource.RAZORPAY_TEST_MODE
    assert record.is_real is False
    assert record.case_id == "case_000123"

    # Unmistakable by eye, not only by field. A real Razorpay id is `plink_`
    # plus 14 base62 characters and can never look like this.
    assert record.id.startswith(rzp.MOCK_ID_PREFIX)
    assert not record.id.startswith("plink_")
    # RFC 2606 reserves `.invalid`, so the URL cannot resolve for anyone.
    assert rzp.MOCK_URL_HOST in record.short_url
    assert record.short_url.endswith(hashlib.sha256(
        f"case_000123|249900|settle CP12 demo".encode()
    ).hexdigest()[:14])

    # Deterministic: the committed mock demo is reproducible on any machine.
    again = rzp.RazorpayClient(mock_mode=True).create_payment_link(
        amount_paise=249900, description="settle CP12 demo", notes=dict(NOTES)
    )
    assert again.id == record.id
    assert again.created_at == record.created_at

    # Explicitly-set true is the same path.
    monkeypatch.setenv("RAZORPAY_MOCK_MODE", "true")
    rzp.reset_client()
    assert rzp.get_client().source is LinkSource.MOCK_SANDBOX


def test_RZP_1_a_mock_record_cannot_be_relabelled_as_real() -> None:
    """The discipline is enforced by the model, not by remembering to check.

    A `PaymentLinkRecord` claiming RAZORPAY_TEST_MODE while carrying a locally
    minted id is the exact mistake the labelling exists to prevent, so it is a
    validation error rather than a convention someone can forget.
    """
    mock = rzp.RazorpayClient(mock_mode=True).create_payment_link(
        amount_paise=100000, description="d", notes=dict(NOTES)
    )

    # Relabelling a real mock record. The id gives it away first, which is the
    # order that matters: the id is the field a human reads.
    with pytest.raises(ValidationError, match="must start with 'plink_'"):
        PaymentLinkRecord.model_validate(
            mock.model_dump() | {"source": LinkSource.RAZORPAY_TEST_MODE}
        )

    # A mock URL under a real-looking id is caught by the second half of the
    # rule, so neither field alone can smuggle a mock through.
    with pytest.raises(ValidationError, match="may not be labelled RAZORPAY_TEST_MODE"):
        PaymentLinkRecord(
            id="plink_QrEaLlOoKiNg",
            short_url=f"https://{rzp.MOCK_URL_HOST}/l/abc",
            amount_paise=100000,
            description="d",
            notes=dict(NOTES),
            status="created",
            created_at=mock.created_at,
            source=LinkSource.RAZORPAY_TEST_MODE,
        )

    with pytest.raises(ValidationError, match="must start with 'plink_'"):
        PaymentLinkRecord(
            id="MOCK_plink_deadbeef0001",
            short_url="https://rzp.io/i/looksReal",
            amount_paise=100000,
            description="d",
            notes=dict(NOTES),
            status="created",
            created_at=mock.created_at,
            source=LinkSource.RAZORPAY_TEST_MODE,
        )

    # And the reverse: a real-shaped id cannot be filed as a mock either.
    with pytest.raises(ValidationError, match="MOCK_SANDBOX id must start"):
        PaymentLinkRecord(
            id="plink_QrEaLlOoKiNg",
            short_url="https://rzp.io/i/abc",
            amount_paise=100000,
            description="d",
            notes=dict(NOTES),
            status="created",
            created_at=mock.created_at,
            source=LinkSource.MOCK_SANDBOX,
        )

    # Frozen, so a record cannot be relabelled after it is built.
    with pytest.raises(ValidationError):
        mock.source = LinkSource.RAZORPAY_TEST_MODE  # type: ignore[misc]


def test_RZP_1_notes_must_carry_case_id() -> None:
    """The only join from an inbound webhook back to a case (SPEC §16)."""
    client = rzp.RazorpayClient(mock_mode=True)
    with pytest.raises(ValueError, match="case_id"):
        client.create_payment_link(amount_paise=100000, description="d", notes={"arm": "EDGE"})
    with pytest.raises(ValueError, match="case_id"):
        client.create_payment_link(amount_paise=100000, description="d", notes={"case_id": ""})


def test_RZP_1_real_mode_requires_credentials_and_refuses_live_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No silent fallback to mock, and no live path at all.

    Falling back to mock on a missing key is the dangerous direction: it produces
    a plausible-looking record when the operator believed they were live.
    """
    monkeypatch.setenv("RAZORPAY_MOCK_MODE", "false")
    monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)
    with pytest.raises(RazorpayCredentialsMissing):
        rzp.get_client()

    with pytest.raises(RazorpayLiveModeRefused):
        RazorpayClient(mock_mode=False, key_id="rzp_live_SOMETHING", key_secret="x")

    with pytest.raises(RazorpayCredentialsMissing):
        RazorpayClient(mock_mode=False, key_id="not_a_razorpay_key", key_secret="x")


# --------------------------------------------------------------------------
# RZP-2 — no credential in a log line, a ledger entry, or an error message
# --------------------------------------------------------------------------

def _all_secrets() -> tuple[str, ...]:
    return (FAKE_KEY_SECRET, FAKE_KEY_ID, FAKE_WEBHOOK_SECRET)


def test_RZP_2_no_credential_appears_in_an_error_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The realistic leak: the SDK raises an exception built from the request it
    made, and that request carries the Basic auth header."""
    client = RazorpayClient(
        mock_mode=False, key_id=FAKE_KEY_ID, key_secret=FAKE_KEY_SECRET
    )

    class ExplodingResource:
        @staticmethod
        def create(_payload: dict) -> dict:
            raise RuntimeError(
                "401 Unauthorized for https://api.razorpay.com/v1/payment_links "
                f"auth=({FAKE_KEY_ID}, {FAKE_KEY_SECRET})"
            )

    class ExplodingSDK:
        payment_link = ExplodingResource()

    client._sdk = ExplodingSDK()  # type: ignore[assignment]

    with pytest.raises(RuntimeError) as caught:
        client.create_payment_link(
            amount_paise=249900, description="settle CP12 demo", notes=dict(NOTES)
        )

    rendered = f"{caught.value}\n{caught.value!r}"
    for secret in (FAKE_KEY_SECRET, FAKE_KEY_ID):
        assert secret not in rendered, "a credential reached the exception message"
    assert rzp.REDACTED in rendered
    # Still diagnosable: the scrub removes the credential, not the error.
    assert "401" in rendered and "payment_links" in rendered

    # `from None` — the original exception must not ride along in __cause__ or
    # __context__ carrying the unscrubbed text into a traceback.
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True


def test_RZP_2_no_credential_appears_in_a_repr_or_a_log_line(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`print(client)` is one keystroke away from a terminal capture in a demo."""
    import logging

    client = RazorpayClient(
        mock_mode=False, key_id=FAKE_KEY_ID, key_secret=FAKE_KEY_SECRET
    )

    rendered = f"{client!r} {client!s} {client}"
    for secret in (FAKE_KEY_SECRET, FAKE_KEY_ID):
        assert secret not in rendered
    assert "rzp_test_…" in rendered  # a fingerprint, enough to tell keys apart

    with caplog.at_level(logging.DEBUG):
        logging.getLogger("settle.demo").info("client=%s", client)
        logging.getLogger("settle.demo").info(
            "fingerprint=%s", client.key_id_fingerprint
        )
    for secret in (FAKE_KEY_SECRET, FAKE_KEY_ID):
        assert secret not in caplog.text

    assert rzp.scrub(f"boom {FAKE_KEY_SECRET}", *_all_secrets()) == f"boom {rzp.REDACTED}"


def test_RZP_2_no_credential_appears_in_a_ledger_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ledger is committed. A credential written into it is a credential in
    git history, which is a rotation, not a deletion."""
    import hmac

    from settle.audit.chain import read_entries

    ledger_path = tmp_path / "audit_edge.jsonl"
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", FAKE_WEBHOOK_SECRET)
    monkeypatch.setenv("RAZORPAY_KEY_ID", FAKE_KEY_ID)
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", FAKE_KEY_SECRET)
    monkeypatch.setenv("SETTLE_EDGE_LEDGER", str(ledger_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'settle.db'}")
    webhook.reset_edge_state()

    try:
        record = rzp.RazorpayClient(mock_mode=True).create_payment_link(
            amount_paise=249900, description="settle CP12 demo", notes=dict(NOTES)
        )
        # The record itself is committed to out/razorpay_demo.json.
        assert not any(s in record.model_dump_json() for s in _all_secrets())

        event = {
            "entity": "event",
            "event": "payment_link.paid",
            "payload": {
                "payment_link": {"entity": {"id": record.id, "notes": dict(NOTES)}}
            },
            "created_at": 1788200000,
        }
        body = json.dumps(event).encode()
        signature = hmac.new(
            FAKE_WEBHOOK_SECRET.encode(), body, hashlib.sha256
        ).hexdigest()

        from tests.test_webhook import call

        response = call(
            "POST",
            "/webhooks/razorpay",
            body=body,
            headers={
                "x-razorpay-event-id": "evt_rzp2_000001",
                "x-razorpay-signature": signature,
            },
        )
        assert response.status == 200

        written = ledger_path.read_text(encoding="utf-8")
        assert written.strip()
        for secret in _all_secrets():
            assert secret not in written, "a credential reached the ledger"
        # The signature is derived from the secret and is safe to keep, but we
        # do not store it either — there is no reader for it and it is one more
        # thing to reason about.
        assert signature not in written
        assert read_entries(ledger_path)[0].payload["signature_verified"] is True

        # The response body a caller sees carries none of it either.
        assert not any(s in response.body.decode() for s in _all_secrets())
    finally:
        webhook.reset_edge_state()


def test_RZP_2_no_credential_is_hardcoded_in_the_source() -> None:
    """Credentials come from the environment. A key literal under `settle/` is a
    key in git history."""
    offenders = []
    for path in sorted((REPO_ROOT / "settle").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for marker in ("rzp_test_", "rzp_live_"):
            for line in source.splitlines():
                if marker in line and not _is_prefix_constant(line, marker):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {line.strip()}")
    assert not offenders, offenders


def _is_prefix_constant(line: str, marker: str) -> bool:
    """A bare `rzp_test_` prefix constant is the check itself, not a key.

    Anything with characters after the prefix inside the quotes is a key.
    """
    stripped = line.strip()
    if stripped.startswith("#"):
        return True
    for quote in ('"', "'"):
        needle = f"{quote}{marker}"
        index = stripped.find(needle)
        while index != -1:
            closing = stripped.find(quote, index + len(needle))
            if closing == -1 or stripped[index + len(needle) : closing] != "":
                return False
            index = stripped.find(needle, closing)
    return True


# --------------------------------------------------------------------------
# RZP-3 — one module reaches the SDK
# --------------------------------------------------------------------------

def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package_parts = path.relative_to(REPO_ROOT).with_suffix("").parts[:-1]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = list(package_parts[: len(package_parts) - (node.level - 1)]) if node.level else []
            module = ".".join([*base, node.module] if node.module else base)
            found.add(module)
            found.update(f"{module}.{alias.name}" for alias in node.names)
    return found


SDK_OWNER = "settle/integrations/razorpay_client.py"


def test_RZP_3_the_client_is_the_only_module_importing_the_sdk() -> None:
    """One seam. It is what lets every other module be tested without keys, and
    what makes "which code can reach Razorpay?" a one-line answer."""
    offenders = {}
    for path in sorted((REPO_ROOT / "settle").rglob("*.py")):
        relative = str(path.relative_to(REPO_ROOT))
        if relative == SDK_OWNER:
            continue
        if any(name == "razorpay" or name.startswith("razorpay.") for name in _imports(path)):
            offenders[relative] = "imports the razorpay SDK"
    assert not offenders, offenders

    # Positively: the owner does import it, so the test cannot pass by the SDK
    # having been dropped everywhere.
    assert "razorpay" in _imports(REPO_ROOT / SDK_OWNER)

    # And nothing reaches it sideways. An unstated exception is how an
    # invariant dies, so `importlib` and `sys.modules` are banned too.
    for path in sorted((REPO_ROOT / "settle").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        relative = str(path.relative_to(REPO_ROOT))
        assert "sys.modules[\"razorpay\"" not in source, relative
        assert "import_module(\"razorpay" not in source, relative
        assert "__import__(\"razorpay" not in source, relative
