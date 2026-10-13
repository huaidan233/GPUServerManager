from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable
from uuid import uuid4

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerificationError, VerifyMismatchError
from argon2.low_level import Type
from cryptography.fernet import Fernet, InvalidToken

from gsm_api.identity_store import (
    ConsumedCodeError,
    ExpiredCodeError,
    IdentityNotFoundError,
    IdentityStore,
    normalize_username,
)
from gsm_api.member_domain import MemberStatus


class AuthenticationError(RuntimeError):
    pass


class PasswordPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class LoginResult:
    session_token: str
    csrf_token: str
    member: dict
    idle_expires_at: datetime
    absolute_expires_at: datetime
    requires_password_change: bool = False


@dataclass(frozen=True)
class SessionResolution:
    member: dict
    session: dict
    requires_password_change: bool = False


@dataclass(frozen=True)
class MfaEnrollment:
    secret: str
    provisioning_uri: str
    recovery_codes: tuple[str, ...]


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class AuthService:
    def __init__(
        self,
        store: IdentityStore,
        *,
        now: Callable[[], datetime] | None = None,
        allowed_origins: set[str] | None = None,
        mfa_encryption_key: str | None = None,
        session_hash_key: str | None = None,
        password_hasher: PasswordHasher | None = None,
    ) -> None:
        self._store = store
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._allowed_origins = allowed_origins or _allowed_origins_from_environment()
        self._hasher = password_hasher or PasswordHasher(type=Type.ID)
        self._dummy_password_hash = self._hasher.hash(secrets.token_urlsafe(24))
        key = mfa_encryption_key or os.environ.get("GSM_MFA_ENCRYPTION_KEY")
        if not key:
            if os.environ.get("GSM_ENV") == "production":
                raise RuntimeError("GSM_MFA_ENCRYPTION_KEY is required in production")
            key = Fernet.generate_key().decode("ascii")
        try:
            self._mfa_cipher = Fernet(key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise RuntimeError("GSM_MFA_ENCRYPTION_KEY must be a Fernet key") from exc
        configured_session_key = session_hash_key or os.environ.get("GSM_SESSION_HASH_KEY")
        if not configured_session_key and os.environ.get("GSM_ENV") == "production":
            raise RuntimeError("GSM_SESSION_HASH_KEY is required in production")
        if configured_session_key and len(configured_session_key.encode("utf-8")) < 32:
            raise RuntimeError("GSM_SESSION_HASH_KEY must contain at least 32 bytes")
        self._session_hash_key = (
            configured_session_key.encode("utf-8")
            if configured_session_key
            else None
        )

    def set_password(
        self,
        member_id: str,
        password: str,
        *,
        requires_password_change: bool = False,
    ) -> dict:
        member = self._store.get_member(member_id)
        if member is None:
            raise AuthenticationError("invalid credentials")
        self._validate_password(member, password)
        existing = self._store.get_password_credential(member_id)
        version = int(existing["credential_version"]) + 1 if existing else 1
        timestamp = self._now()
        record = {
            "member_id": member_id,
            "password_hash": self._hasher.hash(password),
            "password_changed_at": timestamp.isoformat(),
            "requires_password_change": requires_password_change,
            "credential_version": version,
        }
        saved = self._store.save_password_credential(record)
        if existing:
            self._store.revoke_member_sessions(
                member_id,
                "credential changed",
                timestamp,
            )
        return saved

    def activate(self, username: str, code: str, password: str) -> dict:
        member = self._store.get_member_by_username(username)
        self._validate_password(
            member or {"username": normalize_username(username)},
            password,
        )
        if member is None:
            raise AuthenticationError("invalid activation")
        member_status = member.get("status")
        if member_status == MemberStatus.INVITED.value:
            purpose = "account_activation"
        elif member_status in {
            MemberStatus.ACTIVE.value,
            MemberStatus.PENDING_EXIT.value,
        }:
            purpose = "password_reset"
        else:
            raise AuthenticationError("invalid activation")
        try:
            consumed = self._store.consume_one_time_code(
                hash_secret(code),
                purpose=purpose,
                now=self._now(),
            )
        except (ConsumedCodeError, ExpiredCodeError, IdentityNotFoundError) as exc:
            raise AuthenticationError("invalid activation") from exc
        if consumed.get("member_id") != member["id"] or (
            member.get("member_kind", "student") == "student"
            and not member.get("current_program_id")
        ):
            raise AuthenticationError("invalid activation")
        self.set_password(member["id"], password)
        if purpose == "password_reset":
            return member
        return self._store.update_member(
            member["id"],
            member["version"],
            {
                "status": MemberStatus.ACTIVE.value,
                "updated_at": self._now().isoformat(),
            },
        )

    def validate_public_origin(self, origin: str | None) -> None:
        if origin not in self._allowed_origins:
            raise AuthenticationError("request origin is not allowed")

    def verify_password(self, member_id: str, password: str) -> bool:
        credential = self._store.get_password_credential(member_id)
        if credential is None:
            self._verify_dummy(password)
            return False
        try:
            return self._hasher.verify(credential["password_hash"], password)
        except (VerifyMismatchError, VerificationError, InvalidHash):
            return False

    def enroll_admin_mfa(
        self,
        member_id: str,
        *,
        account_name: str,
        issuer: str = "GPU Server Manager",
        initial_setup: bool = False,
    ) -> MfaEnrollment:
        member = self._store.get_member(member_id)
        if (
            member is None
            or member.get("role") != "admin"
            or member.get("status") not in (
                {MemberStatus.ACTIVE.value, MemberStatus.INVITED.value}
                if initial_setup else {MemberStatus.ACTIVE.value}
            )
        ):
            raise AuthenticationError("active administrator account required")
        secret = pyotp.random_base32(length=32)
        recovery_codes = tuple(secrets.token_urlsafe(9) for _ in range(8))
        encrypted = self._mfa_cipher.encrypt(secret.encode("ascii")).decode("ascii")
        self._store.save_mfa_credential(
            {
                "member_id": member_id,
                "encrypted_secret": encrypted,
                "recovery_code_hashes": [hash_secret(code) for code in recovery_codes],
                "updated_at": self._now().isoformat(),
            }
        )
        return MfaEnrollment(
            secret=secret,
            provisioning_uri=pyotp.TOTP(secret).provisioning_uri(
                name=account_name,
                issuer_name=issuer,
            ),
            recovery_codes=recovery_codes,
        )

    def login(
        self,
        username: str,
        password: str,
        *,
        source: str,
        totp_code: str | None = None,
        browser_family: str = "Other",
        os_family: str = "Other",
    ) -> LoginResult:
        timestamp = self._now()
        normalized = normalize_username(username)
        source = source[:128]
        attempt_id = self._reserve_login_attempt(normalized, source, timestamp)
        member = self._store.get_member_by_username(normalized)
        valid_password = self._verify_login_password(member, password)
        credential = self._store.get_password_credential(member["id"]) if member else None
        initial_setup = bool(credential and credential.get("initial_password")
                             and credential.get("requires_password_change"))
        eligible = bool(
            member
            and member.get("status")
            in ({MemberStatus.ACTIVE.value, MemberStatus.PENDING_EXIT.value, MemberStatus.INVITED.value}
                if initial_setup else {MemberStatus.ACTIVE.value, MemberStatus.PENDING_EXIT.value})
        )
        valid_mfa = bool(
            member
            and valid_password
            and eligible
            and (
                member.get("role") != "admin"
                or (initial_setup and (member.get("status") == MemberStatus.INVITED.value
                                       or self._store.get_mfa_credential(member["id"]) is None))
                or self._verify_admin_mfa(member["id"], totp_code)
            )
        )
        if not (member and valid_password and eligible and valid_mfa):
            raise AuthenticationError("invalid credentials")

        credential = self._store.get_password_credential(member["id"])
        if credential is None:
            raise AuthenticationError("invalid credentials")
        self._store.mark_login_attempt_succeeded(attempt_id)
        return self._create_session(
            member,
            credential,
            timestamp,
            browser_family=browser_family,
            os_family=os_family,
        )

    def resolve_session(
        self,
        token: str,
        *,
        csrf_token: str | None,
        origin: str | None,
        mutate: bool,
        allow_password_change: bool = False,
    ) -> SessionResolution:
        timestamp = self._now()
        session = self._store.get_session_by_hash(self._session_token_hash(token))
        return self._resolve_session_record(session, timestamp, csrf_token=csrf_token,
            origin=origin, mutate=mutate, allow_password_change=allow_password_change)

    def resolve_hashed_session(self, token_hash: str) -> SessionResolution:
        """Internal use by scoped app grants; the hash is never accepted from a browser."""
        return self._resolve_session_record(self._store.get_session_by_hash(token_hash), self._now(),
            csrf_token=None, origin=None, mutate=False, allow_password_change=False)

    def _resolve_session_record(self, session, timestamp, *, csrf_token, origin, mutate, allow_password_change):
        if session is None or session.get("revoked_at"):
            raise AuthenticationError("invalid session")
        try:
            idle_expires = _parse_datetime(session["idle_expires_at"])
            absolute_expires = _parse_datetime(session["absolute_expires_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AuthenticationError("invalid session") from exc
        if timestamp > idle_expires or timestamp > absolute_expires:
            raise AuthenticationError("invalid session")

        member = self._store.get_member(session["member_id"])
        credential = self._store.get_password_credential(session["member_id"])
        if (
            member is None
            or credential is None
            or member.get("status")
            not in {MemberStatus.ACTIVE.value, MemberStatus.PENDING_EXIT.value, MemberStatus.INVITED.value}
            or int(credential["credential_version"])
            != int(session["credential_version"])
        ):
            raise AuthenticationError("invalid session")

        must_change = bool(credential.get("requires_password_change"))
        if member.get("status") == MemberStatus.INVITED.value and not (
            must_change and credential.get("initial_password")
        ):
            raise AuthenticationError("invalid session")
        if must_change and not allow_password_change:
            raise AuthenticationError("initial password must be changed")

        if mutate:
            if not csrf_token or not hmac.compare_digest(
                hash_secret(csrf_token),
                str(session["csrf_secret_hash"]),
            ):
                raise AuthenticationError("invalid session")
            if origin not in self._allowed_origins:
                raise AuthenticationError("invalid session")

        new_idle = min(timestamp + timedelta(hours=12), absolute_expires)
        session = self._store.update_session(
            session["id"],
            {
                "last_seen_at": timestamp.isoformat(),
                "idle_expires_at": new_idle.isoformat(),
            },
        )
        return SessionResolution(member=member, session=session, requires_password_change=must_change)

    def require_recent_admin_auth(
        self,
        token: str,
        *,
        within: timedelta = timedelta(minutes=10),
        allow_agent_settings_mfa: bool = False,
    ) -> SessionResolution:
        resolved = self.resolve_session(
            token,
            csrf_token=None,
            origin=None,
            mutate=False,
        )
        if resolved.member.get("role") != "admin":
            raise AuthenticationError("recent administrator authentication required")
        try:
            reauthenticated_at = _parse_datetime(
                resolved.session["reauthenticated_at"]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AuthenticationError(
                "recent administrator authentication required"
            ) from exc
        if self._now() - reauthenticated_at > within:
            if allow_agent_settings_mfa:
                try:
                    verified_at = _parse_datetime(resolved.session["agent_settings_authenticated_at"])
                    if timedelta(0) <= self._now() - verified_at <= within:
                        return resolved
                except (KeyError, TypeError, ValueError):
                    pass
            raise AuthenticationError("recent administrator authentication required")
        return resolved

    def reauthenticate_system_update(self, token: str, *, totp_code: str) -> SessionResolution:
        resolved = self.resolve_session(token, csrf_token=None, origin=None, mutate=False)
        member = resolved.member
        if member.get("role") != "admin":
            raise AuthenticationError("administrator access required")
        key = "system-update:" + member["id"]
        attempt = self._reserve_login_attempt(key, key, self._now())
        credential = self._store.get_mfa_credential(member["id"])
        if credential is None or not (len(totp_code) == 6 and totp_code.isascii() and totp_code.isdigit()):
            raise AuthenticationError("invalid verification code")
        try:
            secret = self._mfa_cipher.decrypt(credential["encrypted_secret"].encode("ascii")).decode("ascii")
        except (InvalidToken, KeyError, UnicodeError):
            raise AuthenticationError("invalid verification code") from None
        verifier = pyotp.TOTP(secret)
        current_step = verifier.timecode(self._now())
        accepted_step = next((step for step in (current_step, current_step - 1, current_step + 1)
                              if pyotp.utils.strings_equal(verifier.generate_otp(step), totp_code)), None)
        if accepted_step is None or not self._store.consume_update_totp_step(
                member["id"], accepted_step, encrypted_secret=credential["encrypted_secret"]):
            raise AuthenticationError("invalid verification code")
        self._store.mark_login_attempt_succeeded(attempt)
        return resolved

    def reauthenticate_agent_settings(self, token: str, *, totp_code: str) -> SessionResolution:
        resolved = self.resolve_session(token, csrf_token=None, origin=None, mutate=False)
        member = resolved.member
        if member.get("role") != "admin":
            raise AuthenticationError("administrator access required")
        attempt = self._reserve_login_attempt("agent-settings:" + member["id"], "agent-settings:" + member["id"], self._now())
        if not (len(totp_code) == 6 and totp_code.isascii() and totp_code.isdigit()
                and self._verify_admin_mfa(member["id"], totp_code)):
            raise AuthenticationError("invalid verification code")
        self._store.mark_login_attempt_succeeded(attempt)
        session = self._store.update_session(resolved.session["id"], {"agent_settings_authenticated_at": self._now().isoformat()})
        return SessionResolution(member=member, session=session)

    def reauthenticate(
        self,
        token: str,
        *,
        password: str,
        totp_code: str,
    ) -> SessionResolution:
        resolved = self.resolve_session(
            token,
            csrf_token=None,
            origin=None,
            mutate=False,
        )
        member = resolved.member
        if (
            member.get("role") != "admin"
            or not self.verify_password(member["id"], password)
            or not self._verify_admin_mfa(member["id"], totp_code)
        ):
            raise AuthenticationError("invalid credentials")
        session = self._store.update_session(
            resolved.session["id"],
            {"reauthenticated_at": self._now().isoformat()},
        )
        return SessionResolution(member=member, session=session)

    def logout(self, token: str) -> None:
        session = self._store.get_session_by_hash(self._session_token_hash(token))
        if session is None or session.get("revoked_at"):
            return
        self._store.update_session(
            session["id"],
            {
                "revoked_at": self._now().isoformat(),
                "revoke_reason": "logout",
            },
        )

    def change_password(
        self,
        member_id: str,
        *,
        current_password: str,
        new_password: str,
        totp_code: str | None = None,
    ) -> dict:
        if not self.verify_password(member_id, current_password):
            raise AuthenticationError("invalid credentials")
        credential = self._store.get_password_credential(member_id)
        if credential and credential.get("initial_password") and credential.get("requires_password_change"):
            member = self._store.get_member(member_id)
            if member is None:
                raise AuthenticationError("invalid credentials")
            if hmac.compare_digest(current_password.encode(), new_password.encode()):
                raise PasswordPolicyError("new password must differ from the initial password")
            self._validate_password(member, new_password)
            if member.get("role") == "admin" and not self._verify_admin_mfa(member_id, totp_code):
                raise AuthenticationError("administrator MFA enrollment must be confirmed")
            saved = {
                "member_id": member_id, "password_hash": self._hasher.hash(new_password),
                "password_changed_at": self._now().isoformat(), "requires_password_change": False,
                "credential_version": int(credential["credential_version"]) + 1,
            }
            try:
                return self._store.complete_initial_password_change(
                    member_id, expected_member_version=member["version"],
                    expected_credential_version=credential["credential_version"],
                    credential=saved, now=self._now(),
                )
            except Exception as exc:
                from gsm_api.identity_store import StaleIdentityError
                if isinstance(exc, StaleIdentityError):
                    raise AuthenticationError("account changed; sign in again") from exc
                raise
        return self.set_password(member_id, new_password)

    def confirm_password(self, member_id: str, password: str) -> None:
        if not self.verify_password(member_id, password):
            raise AuthenticationError("invalid credentials")

    def revoke_other_sessions(
        self,
        member_id: str,
        current_session_id: str,
        *,
        password: str,
    ) -> int:
        self.confirm_password(member_id, password)
        return self._store.revoke_member_sessions(
            member_id,
            "revoked by member",
            self._now(),
            except_session_id=current_session_id,
        )

    def _create_session(
        self,
        member: dict,
        credential: dict,
        timestamp: datetime,
        *,
        browser_family: str,
        os_family: str,
    ) -> LoginResult:
        session_token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        idle_expires_at = timestamp + timedelta(hours=12)
        absolute_expires_at = timestamp + timedelta(days=7)
        self._store.save_session(
            {
                "id": uuid4().hex,
                "member_id": member["id"],
                "token_hash": self._session_token_hash(session_token),
                "csrf_secret_hash": hash_secret(csrf_token),
                "created_at": timestamp.isoformat(),
                "last_seen_at": timestamp.isoformat(),
                "idle_expires_at": idle_expires_at.isoformat(),
                "absolute_expires_at": absolute_expires_at.isoformat(),
                "credential_version": credential["credential_version"],
                "browser_family": _session_family(
                    browser_family,
                    {"Chromium", "Firefox", "Safari", "Edge", "Other"},
                ),
                "os_family": _session_family(
                    os_family,
                    {"Windows", "macOS", "Linux", "iOS", "Android", "Other"},
                ),
                "reauthenticated_at": (
                    timestamp.isoformat()
                    if member.get("role") == "admin"
                    else None
                ),
                "revoked_at": None,
                "revoke_reason": None,
            }
        )
        return LoginResult(
            session_token=session_token,
            csrf_token=csrf_token,
            member=member,
            idle_expires_at=idle_expires_at,
            absolute_expires_at=absolute_expires_at,
            requires_password_change=bool(credential.get("requires_password_change")),
        )

    def _session_token_hash(self, token: str) -> str:
        if self._session_hash_key is None:
            return hash_secret(token)
        return hmac.new(
            self._session_hash_key,
            token.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _verify_login_password(self, member: dict | None, password: str) -> bool:
        if member is None:
            self._verify_dummy(password)
            return False
        credential = self._store.get_password_credential(member["id"])
        valid = self.verify_password(member["id"], password)
        return bool(
            valid
            and credential
            and (not credential.get("requires_password_change", False)
                 or credential.get("initial_password", False))
        )

    def _verify_dummy(self, password: str) -> None:
        try:
            self._hasher.verify(self._dummy_password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHash):
            return

    def _verify_admin_mfa(self, member_id: str, code: str | None) -> bool:
        if not code:
            return False
        if self._store.consume_mfa_recovery_code(
            member_id,
            hash_secret(code),
            now=self._now(),
        ):
            return True
        credential = self._store.get_mfa_credential(member_id)
        if credential is None:
            return False
        try:
            secret = self._mfa_cipher.decrypt(
                credential["encrypted_secret"].encode("ascii")
            ).decode("ascii")
        except (InvalidToken, KeyError, UnicodeError):
            return False
        return bool(
            pyotp.TOTP(secret).verify(
                code,
                for_time=self._now(),
                valid_window=1,
            )
        )

    @staticmethod
    def _validate_password(member: dict, password: str) -> None:
        if len(password) < 12 or len(password) > 128:
            raise PasswordPolicyError("password must contain 12 to 128 characters")
        if password.casefold() == str(member["username"]).casefold():
            raise PasswordPolicyError("password must not equal the username")
        banned_values = {
            value.strip().casefold()
            for value in os.environ.get("GSM_BANNED_PASSWORDS", "").split(",")
            if value.strip()
        }
        if password.casefold() in banned_values:
            raise PasswordPolicyError("password is not allowed")

    def _reserve_login_attempt(
        self,
        normalized_username: str,
        source: str,
        timestamp: datetime,
    ) -> str:
        attempt_id = uuid4().hex
        reserved = self._store.reserve_login_attempt(
            {
                "id": attempt_id,
                "normalized_username": normalized_username,
                "source": source,
                "succeeded": False,
                "occurred_at": timestamp.isoformat(),
            },
            since=timestamp - timedelta(minutes=15),
            username_limit=5,
            source_limit=20,
        )
        if not reserved:
            raise AuthenticationError("try again later")
        return attempt_id


def _allowed_origins_from_environment() -> set[str]:
    values = {
        value.strip()
        for value in os.environ.get(
            "GSM_ALLOWED_ORIGINS",
            "http://localhost:3000,http://127.0.0.1:3000",
        ).split(",")
        if value.strip()
    }
    if os.environ.get("GSM_ENV") == "production" and not values:
        raise RuntimeError("GSM_ALLOWED_ORIGINS is required in production")
    return values


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("authentication timestamps must include a timezone")
    return parsed


def _session_family(value: str, allowed: set[str]) -> str:
    normalized = str(value or "").strip()[:64]
    return normalized if normalized in allowed else "Other"
