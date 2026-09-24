from django.conf import settings
from django.core.checks import Warning as CheckWarning
from django.core.checks import register

from .context_processors import KNOWN


@register()
def shell_declaration(_app_configs=None, **_kwargs):
    raw = getattr(settings, "APP_SHELL", {}) or {}
    out = []
    unknown = sorted(set(raw) - set(KNOWN))
    if unknown:
        out.append(CheckWarning(
            f"APP_SHELL declares unknown capabilities {unknown}; they will do nothing.",
            hint=f"Known capabilities: {', '.join(KNOWN)}.", id="app_shell.W001"))
    from django.urls import NoReverseMatch, reverse

    for key in ("palette", "live"):
        value = raw.get(key)
        if not value:
            continue
        if not (isinstance(value, dict) and (value.get("url") or value.get("url_name"))):
            out.append(CheckWarning(f"APP_SHELL['{key}'] needs {{'url_name': ...}} or {{'url': ...}}",
                                    id="app_shell.W002"))
        elif value.get("url_name") and not value.get("url"):
            try:
                reverse(value["url_name"])
            except NoReverseMatch:
                out.append(CheckWarning(
                    f"APP_SHELL['{key}'] names url {value['url_name']!r}, which does not "
                    "reverse; the capability is switched off rather than pointed at nothing.",
                    id="app_shell.W002"))
    return out
