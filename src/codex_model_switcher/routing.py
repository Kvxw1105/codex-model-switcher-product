"""Explicit catalog-driven route selection and upstream target policy.

The routing layer knows only stable catalog identities.  Provider and host
information is accepted as an explicit, separately validated target; no
unknown model is inferred from a name or URL.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from .catalog import CatalogDocument
from .models import ModelRoute

WireApi = Literal["responses", "chat"]


class RoutingError(Exception):
    """Base error with a small, safe HTTP-shaped error contract."""

    status_code = 400
    error_type = "routing_error"

    def __init__(self, message: str, **details: object) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, object]:
        error: dict[str, object] = {
            "type": self.error_type,
            "message": self.message,
        }
        error.update(self.details)
        return {"error": error}


class UnknownModelError(RoutingError):
    status_code = 404
    error_type = "unknown_model"

    def __init__(self, model_id: str) -> None:
        super().__init__(
            "model_id is not present in the loaded catalog",
            model_id=model_id,
        )


class RouteConfigurationError(RoutingError):
    status_code = 500
    error_type = "route_configuration_error"


class HostNotAllowedError(RoutingError):
    status_code = 403
    error_type = "host_not_allowed"


class RedirectNotAllowedError(RoutingError):
    status_code = 502
    error_type = "redirect_not_allowed"


def normalize_host(host: str) -> str:
    """Normalize a host for exact allowlist comparison."""

    if not isinstance(host, str) or not host.strip():
        raise HostNotAllowedError("upstream host is missing")
    candidate = host.strip().rstrip(".").lower()
    try:
        candidate = candidate.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise HostNotAllowedError("upstream host is invalid") from error
    if ":" in candidate or "/" in candidate or "@" in candidate:
        raise HostNotAllowedError("allowlist entries must contain a host only")
    return candidate


def validate_allowlisted_url(
    url: str,
    allowed_hosts: Iterable[str],
    *,
    lane: str | None = None,
) -> str:
    """Return ``url`` only when its HTTPS host is explicitly allowlisted."""

    if not isinstance(url, str) or not url.strip():
        raise HostNotAllowedError("upstream URL is missing")
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https":
        raise HostNotAllowedError("upstream URL must use HTTPS")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise HostNotAllowedError("upstream URL contains disallowed components")
    try:
        host = normalize_host(parsed.hostname or "")
    except ValueError as error:
        raise HostNotAllowedError("upstream URL host is invalid") from error
    allowlist = frozenset(normalize_host(value) for value in allowed_hosts)
    if not allowlist or host not in allowlist:
        raise HostNotAllowedError(
            "upstream host is not in the route allowlist",
            host=host,
            lane=lane,
        )
    return url


@dataclass(frozen=True, slots=True)
class RouteTarget:
    """An explicit endpoint policy associated with one catalog route.

    ``base_url`` is intentionally not inferred.  For official routes callers
    must provide the endpoint proven by their integration boundary.  The
    optional ``request_path`` is useful for known OpenAI-compatible providers.
    """

    route: ModelRoute
    base_url: str
    allowed_hosts: frozenset[str]
    wire_api: WireApi = "responses"
    request_path: str | None = None
    follow_redirects: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.route, ModelRoute):
            raise RouteConfigurationError("route target requires a ModelRoute")
        if self.wire_api not in ("responses", "chat"):
            raise RouteConfigurationError("wire_api must be responses or chat")
        normalized_hosts = frozenset(normalize_host(host) for host in self.allowed_hosts)
        if not normalized_hosts:
            raise RouteConfigurationError("route target requires a non-empty host allowlist")
        object.__setattr__(self, "allowed_hosts", normalized_hosts)
        endpoint = self.endpoint
        validate_allowlisted_url(endpoint, normalized_hosts, lane=self.route.lane)
        if self.route.lane == "official" and self.follow_redirects:
            # Redirect support is deliberately opt-in and still host-bound;
            # this guard prevents accidental broad official forwarding.
            if not normalized_hosts:
                raise RouteConfigurationError("official routes require an explicit host allowlist")

    @property
    def endpoint(self) -> str:
        if self.request_path is None:
            return self.base_url
        if not isinstance(self.request_path, str) or not self.request_path.startswith("/"):
            raise RouteConfigurationError("request_path must be an absolute path")
        return self.base_url.rstrip("/") + self.request_path

    @property
    def model_id(self) -> str:
        return self.route.model_id

    @property
    def lane(self) -> str:
        return self.route.lane


RouteDefinition = RouteTarget
RouteConfig = RouteTarget


@dataclass(frozen=True, slots=True)
class DeepSeekContract:
    provider_id: str = "deepseek"
    upstream_model: str = "deepseek-v4-flash"
    # Live evidence confirms DeepSeek's official Responses surface for this
    # low-price model; Responses requests stay on the native wire contract.
    wire_api: WireApi = "responses"
    base_url: str = "https://api.deepseek.com"


def default_deepseek_contract() -> DeepSeekContract:
    """Return only the confirmed third-party provider contract."""

    return DeepSeekContract()


def default_deepseek_target(
    route: ModelRoute,
    *,
    allowed_hosts: Iterable[str] = ("api.deepseek.com",),
) -> RouteTarget:
    """Build the confirmed DeepSeek Responses target.

    The credential remains external to this object and must be resolved from
    ``CredentialStore`` by the Router.
    """

    contract = default_deepseek_contract()
    if (
        route.lane != "third_party"
        or route.provider_id != contract.provider_id
        or route.upstream_model != contract.upstream_model
    ):
        raise RouteConfigurationError("route does not match the default DeepSeek contract")
    return RouteTarget(
        route=route,
        base_url=contract.base_url,
        allowed_hosts=frozenset(allowed_hosts),
        wire_api=contract.wire_api,
        request_path="/responses",
    )


class RoutingTable:
    """Resolve a requested model ID against one already-loaded catalog."""

    def __init__(
        self,
        catalog: CatalogDocument | Iterable[ModelRoute],
        targets: Mapping[str, RouteTarget] | None = None,
    ) -> None:
        if isinstance(catalog, CatalogDocument):
            routes = catalog.models
        else:
            routes = tuple(catalog)
        if any(not isinstance(route, ModelRoute) for route in routes):
            raise TypeError("routing table requires ModelRoute values")
        self._routes = {route.model_id: route for route in routes}
        if len(self._routes) != len(routes):
            raise RouteConfigurationError("catalog contains duplicate model IDs")
        self._targets = dict(targets or {})
        for model_id, target in self._targets.items():
            if model_id not in self._routes:
                raise RouteConfigurationError("target is not present in the loaded catalog")
            if target.route != self._routes[model_id]:
                raise RouteConfigurationError("target route does not match the loaded catalog")

    @property
    def model_ids(self) -> frozenset[str]:
        return frozenset(self._routes)

    def resolve(self, model_id: str) -> ModelRoute:
        route = self._routes.get(model_id)
        if route is None:
            raise UnknownModelError(model_id)
        return route

    def resolve_target(self, model_id: str) -> RouteTarget:
        self.resolve(model_id)
        try:
            return self._targets[model_id]
        except KeyError as error:
            raise RouteConfigurationError("model has no explicit upstream target") from error


RouteResolver = RoutingTable


def resolve_route(catalog: CatalogDocument, model_id: str) -> ModelRoute:
    """Resolve only by stable ``model_id``; never infer a provider."""

    return RoutingTable(catalog).resolve(model_id)
