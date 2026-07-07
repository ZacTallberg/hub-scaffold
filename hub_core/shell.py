"""Shared hub SHELL renderer — fills hub_core/frontend/hub_shell.html with the inlined kit
(tokens.css + shell.css + print.css + hub.js + palette.js) and the snapshot as the #hub-data JSON
island. Built ONCE here so every stack's human view (Django or single-file WSGI) renders the SAME
client-rendered tabbed app — no per-stack template duplication. hub.js builds the tabs/tables/modals
client-side from the island (UI == API by construction; the page never scrolls). Stdlib only."""
import json
from functools import lru_cache
from pathlib import Path

FRONTEND = Path(__file__).resolve().parent / "frontend"

# no-FOUC theme bootstrap — must run before styles paint (reads localStorage "hub-theme").
_THEME_INIT = ('<script>(function(){try{var c=localStorage.getItem("hub-theme"),r=document.documentElement;'
               'if(c==="light"||c==="dark"){r.setAttribute("data-theme",c);r.style.colorScheme=c}'
               'else{r.removeAttribute("data-theme");r.style.colorScheme="light dark"}}catch(e){}})();</script>')


@lru_cache(maxsize=16)
def _asset(name):
    p = FRONTEND / name
    return p.read_text(encoding="utf-8") if p.exists() else ""


def render(snap, brand):
    """Return the full single-file HTML for the hub. `snap` = the /hub.json snapshot dict; `brand`
    = the navbar title (e.g. 'Acme · Hub'). Escapes the JSON island (< -> \\u003c so a stray
    </script in any field can't close it) and inlined JS (</script -> <\\/script)."""
    snap_json = json.dumps(snap, separators=(",", ":")).replace("<", "\\u003c")
    repl = {
        "brand": brand,
        "theme_init": _THEME_INIT,
        "tokens_css": _asset("tokens.css"),
        "shell_css": _asset("shell.css"),
        "print_css": _asset("print.css"),
        "snapshot_json": snap_json,
        "hub_js": _asset("hub.js").replace("</script", "<\\/script"),
        "palette_js": _asset("palette.js").replace("</script", "<\\/script"),
    }
    html = _asset("hub_shell.html")
    for k, v in repl.items():
        html = html.replace("{{" + k + "}}", v)
    return html
