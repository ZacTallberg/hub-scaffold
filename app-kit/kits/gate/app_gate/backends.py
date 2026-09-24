"""Authenticator adapters for ``settings.APP_GATE_AUTHENTICATOR``.

The contract is one callable: ``(username, password) -> auth.Actor``, raising
``auth.CredentialRejected`` when the directory says no and ``auth.DirectoryUnavailable`` when it
could not be asked. The two MUST stay distinct: a directory outage reported as "wrong password"
sends people to reset credentials that were never the problem.

``django_password`` checks Django's own user table and is what the example app uses. A directory
adapter (an LDAP bind, an identity provider's resource-owner flow) is written against the same
signature in your project and named in ``APP_GATE_AUTHENTICATOR``; give every outbound call an
explicit timeout and map a timeout to ``DirectoryUnavailable``.
"""
from __future__ import annotations

from .auth import Actor, CredentialRejected


def django_password(username: str, password: str) -> Actor:
    from django.contrib.auth import authenticate as django_authenticate

    user = django_authenticate(username=username, password=password)
    if user is None or not user.is_active:
        raise CredentialRejected("the username or password was not accepted")
    return Actor.of(user.get_username(), user.get_full_name(), user.email or "")
