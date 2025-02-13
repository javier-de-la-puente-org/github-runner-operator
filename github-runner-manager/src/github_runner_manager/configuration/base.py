# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""TODO Module containing Configuration."""

from dataclasses import dataclass
from typing import Optional

from pydantic import AnyHttpUrl, BaseModel, Field, IPvAnyAddress, MongoDsn, validator

from . import github


@dataclass
class ApplicationConfiguration:
    """TODO.

    Attributes:
        extra_labels: TODO
        github_config: TODO
        service_config: TODO
        non_reactive_configuration: TODO
        reactive_configuration: TODO
    """

    extra_labels: list[str]
    github_config: github.GitHubConfiguration
    service_config: "SupportServiceConfig"
    non_reactive_configuration: "NonReactiveConfiguration"
    reactive_configuration: "ReactiveConfiguration | None"


@dataclass
class SupportServiceConfig:
    """Configuration for supporting services for runners.

    Attributes:
        proxy_config: The proxy configuration.
        dockerhub_mirror: The dockerhub mirror to use for runners.
        ssh_debug_connections: The information on the ssh debug services.
        repo_policy_compliance: The configuration of the repo policy compliance service.
    """

    proxy_config: "ProxyConfig | None"
    dockerhub_mirror: str | None
    ssh_debug_connections: "list[SSHDebugConnection] | None"
    repo_policy_compliance: "RepoPolicyComplianceConfig | None"


class ProxyConfig(BaseModel):
    """Proxy configuration.

    Attributes:
        aproxy_address: The address of aproxy snap instance if use_aproxy is enabled.
        http: HTTP proxy address.
        https: HTTPS proxy address.
        no_proxy: Comma-separated list of hosts that should not be proxied.
        use_aproxy: Whether aproxy should be used for the runners.
    """

    http: Optional[AnyHttpUrl]
    https: Optional[AnyHttpUrl]
    no_proxy: Optional[str]
    use_aproxy: bool = False

    @property
    def aproxy_address(self) -> Optional[str]:
        """Return the aproxy address."""
        if self.use_aproxy:
            proxy_address = self.http or self.https
            # assert is only used to make mypy happy
            assert (
                proxy_address is not None and proxy_address.host is not None
            )  # nosec for [B101:assert_used]
            aproxy_address = (
                proxy_address.host
                if not proxy_address.port
                else f"{proxy_address.host}:{proxy_address.port}"
            )
        else:
            aproxy_address = None
        return aproxy_address

    @validator("use_aproxy")
    @classmethod
    def check_use_aproxy(cls, use_aproxy: bool, values: dict) -> bool:
        """Validate the proxy configuration.

        Args:
            use_aproxy: Value of use_aproxy variable.
            values: Values in the pydantic model.

        Raises:
            ValueError: if use_aproxy was set but no http/https was passed.

        Returns:
            Validated use_aproxy value.
        """
        if use_aproxy and not (values.get("http") or values.get("https")):
            raise ValueError("aproxy requires http or https to be set")

        return use_aproxy

    def __bool__(self) -> bool:
        """Return whether the proxy config is set.

        Returns:
            Whether the proxy config is set.
        """
        return bool(self.http or self.https)


class SSHDebugConnection(BaseModel):
    """SSH connection information for debug workflow.

    Attributes:
        host: The SSH relay server host IP address inside the VPN.
        port: The SSH relay server port.
        rsa_fingerprint: The host SSH server public RSA key fingerprint.
        ed25519_fingerprint: The host SSH server public ed25519 key fingerprint.
    """

    host: IPvAnyAddress
    port: int = Field(0, gt=0, le=65535)
    rsa_fingerprint: str = Field(pattern="^SHA256:.*")
    ed25519_fingerprint: str = Field(pattern="^SHA256:.*")


class RepoPolicyComplianceConfig(BaseModel):
    """Configuration for the repo policy compliance service.

    Attributes:
        token: Token for the repo policy compliance service.
        url: URL of the repo policy compliance service.
    """

    token: str
    url: AnyHttpUrl


@dataclass
class NonReactiveConfiguration:
    """TODO.

    Attributes:
        combinations: TODO
    """

    combinations: "list[NonReactiveCombination]"


@dataclass
class NonReactiveCombination:
    """TODO.

    Attributes:
        image: TODO
        flavor: TODO
        base_virtual_machines: TODO
    """

    image: "Image"
    flavor: "Flavor"
    base_virtual_machines: int


@dataclass
class ReactiveConfiguration:
    """TODO.

    Attributes:
        queue: TODO
        max_total_virtual_machines: TODO
        images: TODO
        flavors: TODO
    """

    queue: "QueueConfig"
    max_total_virtual_machines: int
    images: "list[Image]"
    flavors: "list[Flavor]"


class QueueConfig(BaseModel):
    """The configuration for the message queue.

    Attributes:
        mongodb_uri: The URI of the MongoDB database.
        queue_name: The name of the queue.
    """

    mongodb_uri: MongoDsn
    queue_name: str


class Image(BaseModel):
    """TODO.

    Attributes:
        image: TODO
        labels: TODO
    """

    image: str
    labels: list[str]


class Flavor(BaseModel):
    """TODO.

    Attributes:
        flavor: TODO
        labels: TODO
    """

    flavor: str
    labels: list[str]
