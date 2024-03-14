# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Module for handling interactions with OpenStack."""
import json
import logging
import secrets
from dataclasses import dataclass
from typing import Iterable, Literal, Optional

import jinja2
import keystoneauth1.exceptions.http
import openstack
import openstack.compute.v2.server
import openstack.connection
import openstack.exceptions
import openstack.image.v2.image
from openstack.exceptions import OpenStackCloudException
from openstack.identity.v3.project import Project

from charm_state import Arch, ProxyConfig, SSHDebugConnection, UnsupportedArchitectureError
from errors import (
    OpenstackImageBuildError,
    OpenStackUnauthorizedError,
    RunnerBinaryError,
    SubprocessError,
)
from github_client import GithubClient
from github_type import RunnerApplication
from runner_type import GithubPath
from utilities import execute_command, retry

logger = logging.getLogger(__name__)

IMAGE_PATH_TMPL = "jammy-server-cloudimg-{architecture}-compressed.img"
IMAGE_NAME = "jammy"
BUILD_OPENSTACK_IMAGE_SCRIPT_FILENAME = "scripts/build-openstack-image.sh"


def _create_connection(cloud_config: dict[str, dict]) -> openstack.connection.Connection:
    """Create a connection object.

    This method should be called with a valid cloud_config. See def _validate_cloud_config.
    Also, this method assumes that the clouds.yaml exists on ~/.config/openstack/clouds.yaml.
    See charm_state.py _write_openstack_config_to_disk.

    Args:
        cloud_config: The configuration in clouds.yaml format to apply.

    Raises:
        InvalidConfigError: if the config has not all required information.

    Returns:
        An openstack.connection.Connection object.
    """
    clouds = list(cloud_config["clouds"].keys())
    if len(clouds) > 1:
        logger.warning("Multiple clouds defined in clouds.yaml. Using the first one to connect.")
    cloud_name = clouds[0]

    # api documents that keystoneauth1.exceptions.MissingRequiredOptions can be raised but
    # I could not reproduce it. Therefore, no catch here.
    return openstack.connect(cloud_name)


def list_projects(cloud_config: dict[str, dict]) -> list[Project]:
    """List all projects in the OpenStack cloud.

    The purpose of the method is just to try out openstack integration and
    it may be removed in the future.

    It currently returns objects directly from the sdk,
    which may not be ideal (mapping to domain objects may be preferable).

    Returns:
        A list of projects.
    """
    conn = _create_connection(cloud_config)
    try:
        projects = conn.list_projects()
        logger.debug("OpenStack connection successful.")
        logger.debug("Projects: %s", projects)
        # pylint thinks this isn't an exception
    except keystoneauth1.exceptions.http.Unauthorized as exc:
        raise OpenStackUnauthorizedError(  # pylint: disable=bad-exception-cause
            "Unauthorized to connect to OpenStack."
        ) from exc

    return projects


def _generate_docker_proxy_unit_file(proxies: Optional[ProxyConfig] = None) -> str:
    """Generate docker proxy systemd unit file.

    Args:
        proxies: HTTP proxy settings.

    Returns:
        Contents of systemd-docker-proxy unit file.
    """
    environment = jinja2.Environment(loader=jinja2.FileSystemLoader("templates"), autoescape=True)
    return environment.get_template("systemd-docker-proxy.j2").render(proxies=proxies)


def _generate_docker_client_proxy_config_json(http_proxy: str, https_proxy: str, no_proxy: str):
    """Generate proxy config.json for docker client.

    Args:
        http_proxy: HTTP proxy URL.
        https_proxy: HTTPS proxy URL.
        no_proxy: URLs to not proxy through.

    Returns:
        Contents of docker config.json file.
    """
    return json.dumps(
        {
            "proxies": {
                "default": {
                    key: value
                    for key, value in (
                        ("httpProxy", http_proxy),
                        ("httpsProxy", https_proxy),
                        ("noProxy", no_proxy),
                    )
                    if value
                }
            }
        }
    )


def _build_image_command(
    runner_info: RunnerApplication, proxies: Optional[ProxyConfig] = None
) -> list[str]:
    """Get command for building runner image.

    Args:
        runner_info: The runner application to fetch runner tar download url.
        proxies: HTTP proxy settings.

    Returns:
        Command to execute to build runner image.
    """
    docker_proxy_service_conf_content = _generate_docker_proxy_unit_file(proxies=proxies)

    http_proxy = str(proxies.http) if (proxies and proxies.http) else ""
    https_proxy = str(proxies.https) if (proxies and proxies.https) else ""
    no_proxy = str(proxies.no_proxy) if (proxies and proxies.no_proxy) else ""

    docker_client_proxy_content = _generate_docker_client_proxy_config_json(
        http_proxy=http_proxy, https_proxy=https_proxy, no_proxy=no_proxy
    )

    cmd = [
        "/usr/bin/bash",
        BUILD_OPENSTACK_IMAGE_SCRIPT_FILENAME,
        runner_info["download_url"],
        http_proxy,
        https_proxy,
        no_proxy,
        docker_proxy_service_conf_content,
        docker_client_proxy_content,
    ]

    return cmd


@dataclass
class InstanceConfig:
    """The configuration values for creating a single runner instance.

    Args:
        name: Name of the image to launch the GitHub runner instance with.
        labels: The runner instance labels.
        registration_token: Token for registering the runner on GitHub.
        github_path: The GitHub repo/org path
        openstack_image: The Openstack image to use to boot the instance with.
    """

    name: str
    labels: Iterable[str]
    registration_token: str
    github_path: GithubPath
    openstack_image: openstack.image.v2.image.Image


def _get_supported_runner_arch(arch: str) -> Literal["amd64", "arm64"]:
    """Validate and return supported runner architecture.

    Args:
        arch: str

    Raises:
        UnsupportedArchitectureError: If an unsupported architecture was passed.

    Returns:
        The supported architecture.
    """
    match arch:
        case "x64":
            return "amd64"
        case "arm64":
            return "arm64"
        case _:
            raise UnsupportedArchitectureError(arch)


def build_image(
    arch: Arch,
    cloud_config: dict[str, dict],
    github_client: GithubClient,
    path: GithubPath,
    proxies: Optional[ProxyConfig] = None,
) -> str:
    """Build and upload an image to OpenStack.

    Args:
        cloud_config: The cloud configuration to connect OpenStack with.
        github_client: The Github client to interact with Github API.
        path: Github organisation or repository path.
        proxies: HTTP proxy settings.

    Raises:
        ImageBuildError: If there were errors building/creating the image.

    Returns:
        The created OpenStack image id.
    """
    try:
        runner_application = github_client.get_runner_application(path=path, arch=arch)
    except RunnerBinaryError as exc:
        raise OpenstackImageBuildError("Failed to fetch runner application.") from exc

    try:
        execute_command(_build_image_command(runner_application, proxies), check_exit=True)
    except SubprocessError as exc:
        raise OpenstackImageBuildError("Failed to build image.") from exc

    try:
        runner_arch = runner_application["architecture"]
        image_arch = _get_supported_runner_arch(arch=runner_arch)
    except UnsupportedArchitectureError as exc:
        raise OpenstackImageBuildError(f"Unsupported architecture {runner_arch}") from exc

    try:
        conn = _create_connection(cloud_config)
        existing_image: openstack.image.v2.image.Image
        for existing_image in conn.search_images(name_or_id=IMAGE_NAME):
            # images with same name (different ID) can be created and will error during server
            # instantiation.
            if not conn.delete_image(name_or_id=existing_image.id, wait=True):
                raise OpenstackImageBuildError("Failed to delete duplicate image on Openstack.")
        image: openstack.image.v2.image.Image = conn.create_image(
            name=IMAGE_NAME, filename=IMAGE_PATH_TMPL.format(architecture=image_arch), wait=True
        )
        return image.id
    except OpenStackCloudException as exc:
        raise OpenstackImageBuildError("Failed to upload image.") from exc


def create_instance_config(
    unit_name: str,
    openstack_image: openstack.image.v2.image.Image,
    path: GithubPath,
    github_client: GithubClient,
) -> InstanceConfig:
    """Create an instance config from charm data.

    Args:
        unit_name: The charm unit name.
        image: Ubuntu image flavor.
        path: Github organisation or repository path.
        github_client: The Github client to interact with Github API.
    """
    app_name, unit_num = unit_name.rsplit("/", 1)
    suffix = secrets.token_hex(12)
    registration_token = github_client.get_runner_registration_token(path=path)
    return InstanceConfig(
        name=f"{app_name}-{unit_num}-{suffix}",
        labels=(app_name, "jammy"),
        registration_token=registration_token,
        github_path=path,
        openstack_image=openstack_image,
    )


class InstanceLaunchError(Exception):
    """Exception representing an error during instance launch process."""


def _generate_runner_env(
    templates_env: jinja2.Environment,
    proxies: Optional[ProxyConfig] = None,
    dockerhub_mirror: Optional[str] = None,
    ssh_debug_connections: list[SSHDebugConnection] | None = None,
) -> str:
    """Generate Github runner .env file contents.

    Args:
        templates_env: The jinja template environment.
        proxies: Proxy values to enable on the Github runner.
        dockerhub_mirror: The url to Dockerhub to reduce rate limiting.
        ssh_debug_connections: Tmate SSH debug connection information to load as environment vars.

    Returns:
        The .env contents to be loaded by Github runner.
    """
    return templates_env.get_template("env.j2").render(
        proxies=proxies,
        pre_job_script="",
        dockerhub_mirror=dockerhub_mirror or "",
        ssh_debug_info=(secrets.choice(ssh_debug_connections) if ssh_debug_connections else None),
    )


def _generate_cloud_init_userdata(
    templates_env: jinja2.Environment, instance_config: InstanceConfig, runner_env: str
) -> str:
    """Generate cloud init userdata to launch at startup.

    Args:
        templates_env: The jinja template environment.
        instance_config: The configuration values for Openstack instance to launch.
        runner_env: The contents of .env to source when launching Github runner.

    Returns:
        The cloud init userdata script.
    """
    return templates_env.get_template("openstack-userdata.sh.j2").render(
        github_url=f"https://github.com/{instance_config.github_path.path()}",
        token=instance_config.registration_token,
        instance_labels=",".join(instance_config.labels),
        instance_name=instance_config.name,
        env_contents=runner_env,
    )


@retry(tries=5, delay=5, max_delay=60, backoff=2, local_logger=logger)
def create_instance(
    cloud_config: dict[str, dict],
    instance_config: InstanceConfig,
    proxies: Optional[ProxyConfig] = None,
    dockerhub_mirror: Optional[str] = None,
    ssh_debug_connections: list[SSHDebugConnection] | None = None,
) -> None:
    """Create an OpenStack instance.

    Args:
        cloud_config: The cloud configuration to connect Openstack with.
        instance_config: The configuration values for Openstack instance to launch.

    Raises:
        InstanceLaunchError: if any errors occurred while launching Openstack instance.
    """
    environment = jinja2.Environment(loader=jinja2.FileSystemLoader("templates"), autoescape=True)

    env_contents = _generate_runner_env(
        templates_env=environment,
        proxies=proxies,
        dockerhub_mirror=dockerhub_mirror,
        ssh_debug_connections=ssh_debug_connections,
    )
    cloud_userdata = _generate_cloud_init_userdata(
        templates_env=environment, instance_config=instance_config, runner_env=env_contents
    )

    try:
        conn = _create_connection(cloud_config)
        conn.create_server(
            name=instance_config.name,
            image=instance_config.openstack_image,
            flavor="m1.small",
            userdata=cloud_userdata,
            wait=True,
        )
    except OpenStackCloudException as exc:
        raise InstanceLaunchError("Failed to launch instance.") from exc
