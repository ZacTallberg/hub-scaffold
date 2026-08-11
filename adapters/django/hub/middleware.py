class NoStoreHTMLMiddleware:
    """Dynamic dashboard pages must never be served stale from the browser cache after a
    deploy (no-store on interactive pages). Static assets are untouched."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if response.get("Content-Type", "").startswith("text/html"):
            response.setdefault("Cache-Control", "no-store, must-revalidate")
        return response
