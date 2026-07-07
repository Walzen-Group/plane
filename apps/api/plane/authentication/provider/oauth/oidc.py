# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Python imports
import logging
import os
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlparse

import jwt
import pytz
import requests
from django.core.cache import cache

# Module imports
from plane.authentication.adapter.oauth import OauthAdapter
from plane.license.utils.instance_value import get_configuration_value
from plane.authentication.adapter.error import (
    AuthenticationException,
    AUTHENTICATION_ERROR_CODES,
)

# Module-level logger: the discovery document is fetched inside __init__ before
# super().__init__() has run, so self.logger does not exist yet at that point.
logger = logging.getLogger("plane.authentication")

# The discovery document and JWKS almost never change, but the naive flow
# refetched both on every sign-in (discovery twice: once to build the authorize
# URL, once again on the callback), adding several blocking round-trips to the
# IdP per login. Cache discovery in Redis and reuse a process-wide PyJWKClient
# so a warm login performs only the mandatory token exchange.
DISCOVERY_CACHE_TTL = 60 * 60  # seconds

# Process-local PyJWKClient cache, keyed by JWKS URI. PyJWKClient caches the
# fetched key set for `lifespan` seconds, so reusing one instance per URI avoids
# refetching the JWKS on every callback.
_jwks_clients = {}


def _get_jwks_client(jwks_uri):
    client = _jwks_clients.get(jwks_uri)
    if client is None:
        client = jwt.PyJWKClient(
            jwks_uri, cache_keys=True, lifespan=DISCOVERY_CACHE_TTL, timeout=10
        )
        _jwks_clients[jwks_uri] = client
    return client


class OIDCOAuthProvider(OauthAdapter):
    provider = "oidc"
    scope = "openid email profile"

    def __init__(self, request, code=None, state=None, callback=None):
        (OIDC_URL, OIDC_CLIENT_ID, OIDC_CLIENT_SECRET) = get_configuration_value(
            [
                {
                    "key": "OIDC_URL",
                    "default": os.environ.get("OIDC_URL"),
                },
                {
                    "key": "OIDC_CLIENT_ID",
                    "default": os.environ.get("OIDC_CLIENT_ID"),
                },
                {
                    "key": "OIDC_CLIENT_SECRET",
                    "default": os.environ.get("OIDC_CLIENT_SECRET"),
                },
            ]
        )

        if not (OIDC_URL and OIDC_CLIENT_ID and OIDC_CLIENT_SECRET):
            raise AuthenticationException(
                error_code=AUTHENTICATION_ERROR_CODES["OIDC_NOT_CONFIGURED"],
                error_message="OIDC_NOT_CONFIGURED",
            )

        # Enforce scheme and normalize trailing slash(es)
        parsed = urlparse(OIDC_URL)
        if not parsed.scheme or parsed.scheme not in ("https", "http"):
            raise AuthenticationException(
                error_code=AUTHENTICATION_ERROR_CODES["OIDC_NOT_CONFIGURED"],
                # avoid leaking details to query params
                error_message="OIDC_NOT_CONFIGURED",
            )
        OIDC_URL = OIDC_URL.rstrip("/")

        client_id = OIDC_CLIENT_ID
        client_secret = OIDC_CLIENT_SECRET

        # Fetch the OIDC discovery document to resolve the provider endpoints.
        discovery = self.__get_discovery_document(OIDC_URL)
        self.issuer = discovery.get("issuer", OIDC_URL)
        self.token_url = discovery.get("token_endpoint")
        self.userinfo_url = discovery.get("userinfo_endpoint")
        auth_endpoint = discovery.get("authorization_endpoint")
        self.jwks_uri = discovery.get("jwks_uri")

        if not (auth_endpoint and self.token_url and self.userinfo_url and self.jwks_uri):
            raise AuthenticationException(
                error_code=AUTHENTICATION_ERROR_CODES["OIDC_OAUTH_PROVIDER_ERROR"],
                error_message="OIDC_OAUTH_PROVIDER_ERROR",
            )

        # Anti-replay nonce; the view stashes this in the session and it is
        # verified against the id_token claim on callback.
        self.nonce = uuid.uuid4().hex

        # Populated on the callback leg from the validated id_token so user data
        # can be derived without a separate /userinfo request.
        self.id_token_claims = None

        redirect_uri = f"{'https' if request.is_secure() else 'http'}://{request.get_host()}/auth/oidc/callback/"
        url_params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": self.scope,
            "state": state,
            "nonce": self.nonce,
        }
        auth_url = f"{auth_endpoint}?{urlencode(url_params)}"

        super().__init__(
            request,
            self.provider,
            client_id,
            self.scope,
            redirect_uri,
            auth_url,
            self.token_url,
            self.userinfo_url,
            client_secret,
            code,
            callback=callback,
        )

    def __get_discovery_document(self, oidc_url):
        """Fetch (and cache) the OIDC discovery document."""
        cache_key = f"oidc:discovery:{oidc_url}"
        cached = cache.get(cache_key)
        if cached:
            return cached
        try:
            response = requests.get(
                f"{oidc_url}/.well-known/openid-configuration",
                headers={"Accept": "application/json"},
                timeout=10,
            )
            response.raise_for_status()
            document = response.json()
        except requests.RequestException:
            logger.warning("Error fetching OIDC discovery document")
            raise AuthenticationException(
                error_code=AUTHENTICATION_ERROR_CODES["OIDC_OAUTH_PROVIDER_ERROR"],
                error_message="OIDC_OAUTH_PROVIDER_ERROR",
            )
        cache.set(cache_key, document, timeout=DISCOVERY_CACHE_TTL)
        return document

    def get_nonce(self):
        return self.nonce

    def __validate_id_token(self, id_token):
        """
        Validate the id_token (a JWT) against the provider's JWKS:
        verify the signature, audience, issuer and expiry, then confirm the
        nonce matches the value stashed in the session.
        """
        if not id_token:
            raise AuthenticationException(
                error_code=AUTHENTICATION_ERROR_CODES["OIDC_OAUTH_PROVIDER_ERROR"],
                error_message="OIDC_OAUTH_PROVIDER_ERROR",
            )

        # The nonce echoed back by the IdP must match the one we generated and
        # stored in the session at initiate time.
        session_nonce = self.request.session.get("nonce")

        try:
            jwks_client = _get_jwks_client(self.jwks_uri)
            signing_key = jwks_client.get_signing_key_from_jwt(id_token)
            decoded = jwt.decode(
                id_token,
                signing_key.key,
                algorithms=["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"],
                audience=self.client_id,
                issuer=self.issuer,
                options={
                    "require": ["exp", "iat", "aud", "iss"],
                    "verify_signature": True,
                    "verify_aud": True,
                    "verify_iss": True,
                    "verify_exp": True,
                },
            )
        except Exception:
            self.logger.warning("Error validating OIDC id_token")
            raise AuthenticationException(
                error_code=AUTHENTICATION_ERROR_CODES["OIDC_OAUTH_PROVIDER_ERROR"],
                error_message="OIDC_OAUTH_PROVIDER_ERROR",
            )

        # Verify the nonce to defend against token replay.
        token_nonce = decoded.get("nonce")
        if not session_nonce or not token_nonce or token_nonce != session_nonce:
            self.logger.warning("OIDC id_token nonce mismatch")
            raise AuthenticationException(
                error_code=AUTHENTICATION_ERROR_CODES["OIDC_OAUTH_PROVIDER_ERROR"],
                error_message="OIDC_OAUTH_PROVIDER_ERROR",
            )

        return decoded

    def set_token_data(self):
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": self.code,
            "redirect_uri": self.redirect_uri,
            "grant_type": "authorization_code",
        }
        token_response = self.get_user_token(data=data, headers={"Accept": "application/json"})

        id_token = token_response.get("id_token", "")
        # Full JWKS validation of the id_token (signature, aud, iss, exp, nonce).
        # Retain the decoded claims so set_user_data can derive the profile from
        # them instead of making a second /userinfo round-trip.
        self.id_token_claims = self.__validate_id_token(id_token)

        super().set_token_data(
            {
                "access_token": token_response.get("access_token"),
                "refresh_token": token_response.get("refresh_token", None),
                "access_token_expired_at": (
                    datetime.now(tz=pytz.utc) + timedelta(seconds=token_response.get("expires_in"))
                    if token_response.get("expires_in")
                    else None
                ),
                "refresh_token_expired_at": (
                    datetime.fromtimestamp(token_response.get("refresh_token_expired_at"), tz=pytz.utc)
                    if token_response.get("refresh_token_expired_at")
                    else None
                ),
                "id_token": id_token,
            }
        )

    def set_user_data(self):
        # Prefer the claims carried in the validated id_token. With the standard
        # "openid email profile" scopes these already include email/sub/name, so
        # the extra /userinfo request is redundant. Fall back to /userinfo only
        # when the id_token omits the email claim (some IdPs issue lean tokens).
        user_info_response = self.id_token_claims or {}
        if not user_info_response.get("email"):
            user_info_response = {**user_info_response, **self.get_user_response()}
        email = user_info_response.get("email")
        # Coerce missing claims to "" rather than letting None reach the base
        # adapter: User.{avatar,first_name,last_name} are NOT NULL in the DB,
        # and sync_user_data writes user.avatar = whatever we put here when the
        # avatar download path returns nothing.
        super().set_user_data(
            {
                "email": email,
                "user": {
                    "provider_id": user_info_response.get("sub"),
                    "email": email,
                    "avatar": user_info_response.get("picture") or "",
                    "first_name": user_info_response.get("given_name") or "",
                    "last_name": user_info_response.get("family_name") or "",
                    "is_password_autoset": True,
                },
            }
        )
