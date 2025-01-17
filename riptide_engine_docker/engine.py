import asyncio

import docker
import json
from json import JSONDecodeError
from typing import Tuple, Dict, Union, List, Optional

from docker.errors import APIError, ImageNotFound

from riptide.config.document.command import Command
from riptide.config.document.config import Config
from riptide.config.document.project import Project
from riptide.engine.abstract import AbstractEngine, ServiceStoppedException
from riptide_engine_docker import network, service, path_utils, named_volumes
from riptide_engine_docker.cmd_detached import cmd_detached
from riptide_engine_docker.container_builder import get_service_container_name, RIPTIDE_DOCKER_LABEL_HTTP_PORT
from riptide.engine.project_start_ctx import riptide_start_project_ctx
from riptide.engine.results import StartStopResultStep, MultiResultQueue, ResultQueue, ResultError
from riptide_engine_docker.fg import exec_fg, cmd_fg, service_fg, DEFAULT_EXEC_FG_CMD, cmd_in_service_fg


class DockerEngine(AbstractEngine):

    def __init__(self):
        self.client = docker.from_env()
        self.ping()

    def start_project(self,
                      project: Project,
                      services: List[str],
                      quick=False,
                      command_group: str = "default") -> MultiResultQueue[StartStopResultStep]:
        with riptide_start_project_ctx(project):
            # Start network
            network.start(self.client, project["name"])

            # Start all services
            queues = {}
            loop = asyncio.get_event_loop()
            for service_name in services:
                # Create queue and add to queues
                queue = ResultQueue()
                queues[queue] = service_name
                if service_name in project["app"]["services"]:
                    # Run start task
                    loop.run_in_executor(
                        None,
                        service.start,

                        project["name"],
                        project["app"]["services"][service_name],
                        command_group,
                        self.client,
                        queue,
                        quick
                    )
                else:
                    # Services not found :(
                    queue.end_with_error(ResultError("Service not found."))

            return MultiResultQueue(queues)

    def stop_project(self, project: Project, services: List[str]) -> MultiResultQueue[StartStopResultStep]:
        # Stop all services
        queues = {}
        loop = asyncio.get_event_loop()

        for service_name in services:
            # Create queue and add to queues
            queue = ResultQueue()
            queues[queue] = service_name
            # Run stop task
            loop.run_in_executor(
                None,
                service.stop,

                project["name"],
                service_name,
                self.client,
                queue
            )

        return MultiResultQueue(queues)

    def status(self, project: Project) -> Dict[str, bool]:
        services = {}
        for service_name, service_obj in project["app"]["services"].items():
            services[service_name] = service.status(project["name"], service_obj, self.client, project.parent())
        return services

    def service_status(self, project: Project, service_name: str) -> Dict[str, bool]:
        return service.status(project["name"], project["app"]["services"][service_name], self.client, project.parent())

    def container_name_for(self, project: 'Project', service_name: str):
        return get_service_container_name(project["name"], service_name)

    def address_for(self, project: Project, service_name: str) -> Union[None, Tuple[str, int]]:
        if "port" not in project["app"]["services"][service_name]:
            return None

        container_name = get_service_container_name(project["name"], service_name)
        try:
            container = self.client.containers.get(container_name)
            if container.status != "running":
                return None
            port = container.labels[RIPTIDE_DOCKER_LABEL_HTTP_PORT]
            return "127.0.0.1", port
        except KeyError:
            return None
        except APIError:
            return None

    def cmd(self,
            project: 'Project',
            command_name: str,
            arguments: List[str],
            unimportant_paths_unsynced=False) -> int:
        # Start network
        network.start(self.client, project["name"])

        return cmd_fg(self.client, project, command_name, arguments)

    def cmd_in_service(self,
                       project: 'Project',
                       command_name: str,
                       service_name: str,
                       arguments: List[str]) -> int:
        # Check if service is running
        if not self.service_status(project, service_name):
            raise ServiceStoppedException(f'Service {service_name} must be running to use this command.')

        return cmd_in_service_fg(self.client, project, command_name, service_name, arguments)

    def service_fg(self,
                   project: 'Project',
                   service_name: str,
                   arguments: List[str],
                   unimportant_paths_unsynced=False,
                   command_group: str = "default") -> None:
        # Start network
        network.start(self.client, project["name"])

        with riptide_start_project_ctx(project):
            service_fg(self.client, project, service_name, command_group, arguments)

    def exec(self, project: Project, service_name: str, cols=None, lines=None, root=False) -> None:
        exec_fg(self.client, project, service_name, DEFAULT_EXEC_FG_CMD, cols, lines, root)

    def exec_custom(self, project: Project, service_name: str, command: str, cols=None, lines=None, root=False) -> None:
        exec_fg(self.client, project, service_name, command, cols, lines, root)

    def ping(self):
        try:
            self.client.ping()
        except Exception as err:
            raise ConnectionError("Connection with Docker Daemon failed") from err

    def cmd_detached(self, project: 'Project', command: 'Command', run_as_root=False):
        # Start network
        network.start(self.client, project["name"])
        command.parent_doc = project["app"]

        return cmd_detached(self.client, project, command, run_as_root)

    def pull_images(self, project: 'Project', line_reset='\n', update_func=lambda msg: None) -> None:
        if "services" in project["app"]:
            for name, service in project["app"]["services"].items():
                update_func(f"[service/{name}] Pulling '{service['image']}':\n")
                self.__pull_image(service['image'] if ":" in service['image'] else service['image'] + ":latest",
                                  line_reset, update_func)

        if "commands" in project["app"]:
            for name, command in project["app"]["commands"].items():
                if "image" in command:
                    update_func(f"[command/{name}] Pulling '{command['image']}':\n")
                    self.__pull_image(command['image'] if ":" in command['image'] else command['image'] + ":latest",
                                      line_reset, update_func)

        update_func("Done!\n\n")

    def path_rm(self, path, project: 'Project'):
        return path_utils.rm(self, path, project)

    def path_copy(self, fromm, to, project: 'Project'):
        return path_utils.copy(self, fromm, to, project)

    def performance_value_for_auto(self, key: str, platform: str) -> bool:
        if platform != 'linux':
            if key == 'dont_sync_named_volumes_with_host' or key == 'dont_sync_unimportant_src':
                return True
        return False

    def list_named_volumes(self) -> List[str]:
        return named_volumes.list(self.client)

    def delete_named_volume(self, name: str) -> None:
        named_volumes.delete(self.client, name)

    def exists_named_volume(self, name: str) -> bool:
        return named_volumes.exists(self.client, name)

    def copy_named_volume(self, from_name: str, target_name: str) -> None:
        named_volumes.copy(self.client, from_name, target_name)

    def create_named_volume(self, name: str) -> None:
        named_volumes.create(self.client, name)

    def __pull_image(self, image_name, line_reset, update_func):
        try:
            for line in self.client.api.pull(image_name, stream=True):
                try:
                    status = json.loads(line)
                    if "progress" in status:
                        update = status["status"] + " : " + status[ "progress"]
                    else:
                        update = status["status"]
                except JSONDecodeError:
                    update = line
                update_func(f"{line_reset}    {update}")
            update_func(f"{line_reset}    Done!\n")
        except APIError as ex:
            if "404 Client Error" in str(ex):
                update_func(f"{line_reset}    Warning: Image not found in repository.\n")
            else:
                raise

    def get_service_or_command_image_labels(self, obj: Union['Service', 'Command']) -> Optional[Dict[str, str]]:
        if 'image' not in obj:
            return None
        image_name = obj['image']
        try:
            image = self.client.images.get(image_name)
        except ImageNotFound:
            return None
        return image.labels
