# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from plane.authentication.adapter.base import Adapter


class CredentialAdapter(Adapter):
    """Common interface for all credential providers"""

    # Email-based (email/password, magic link) providers. Used by the signup
    # gate to distinguish email registration from identity-provider (OIDC) sign-up.
    is_credential_provider = True

    def __init__(self, request, provider, callback=None):
        super().__init__(request=request, provider=provider, callback=callback)
        self.request = request
        self.provider = provider

    def authenticate(self):
        self.set_user_data()
        return self.complete_login_or_signup()
