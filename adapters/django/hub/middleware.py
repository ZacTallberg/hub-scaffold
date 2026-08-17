from asgiref.sync import iscoroutinefunction, markcoroutinefunction


class NoStoreHTMLMiddleware:
    """Dynamic dashboard pages must never be served stale from the browser cache after a
    deploy (no-store on interactive pages). Static assets are untouched. Dual-capable so this
    tiny header policy never forces an ASGI deployment, including persistent SSE, through Django's
    sync adaptation thread."""

    sync_capable = True
    async_capable = True

    def __init__(self, get_response):
        self.get_response = get_response
        self.is_async = iscoroutinefunction(get_response)
        if self.is_async:
            markcoroutinefunction(self)

    def __call__(self, request):
        if self.is_async:
            return self.__acall__(request)
        response = self.get_response(request)
        return self._finish(response)

    async def __acall__(self, request):
        response = await self.get_response(request)
        return self._finish(response)

    @staticmethod
    def _finish(response):
        if response.get("Content-Type", "").startswith("text/html"):
            response.setdefault("Cache-Control", "no-store, must-revalidate")
        return response
