"""ForgePort boundary: the shared error type every forge adapter raises
(docs/06). The Protocol + PR DTOs are formalized in the ForgePort stage."""


class ForgeError(Exception):
    """Raised by all forge adapters for HTTP-level failures (docs/06).
    `status` carries the HTTP status code when one exists."""

    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status
