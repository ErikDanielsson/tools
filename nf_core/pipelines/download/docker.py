import concurrent
import concurrent.futures
import logging
import select
import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Optional

import rich.progress

from nf_core.pipelines.download.container_fetcher import ContainerFetcher, ContainerProgress

log = logging.getLogger(__name__)


class DockerFetcher(ContainerFetcher):
    """
    Fetcher for Docker containers.
    """

    def __init__(
        self,
        container_library: Iterable[str],
        registry_set: Iterable[str],
        parallel: int = 4,
    ):
        """
        Intialize the docker image fetcher

        """

        progress_ctx = DockerProgress()
        super().__init__(
            container_library=container_library,
            registry_set=registry_set,
            progress_ctx=progress_ctx,
            cache_dir=None,  # Docker does not use a cache directory
            library_dir=None,  # Docker does not use a library directory
            amend_cachedir=False,  # Docker does not use a cache directory
            parallel=parallel,
        )

    def check_and_set_implementation(self):
        if not shutil.which("docker"):
            raise OSError("Docker is needed to pull images, but it is not installed or not in $PATH")
        self.implementation = "docker"

    def clean_container_file_extension(self, container_fn):
        """
        This makes sure that the Docker container filename has a .tar extension
        """
        extension = ".tar"
        if container_fn.endswith(".tar"):
            container_fn.strip(".tar")
        # Strip : and / characters
        container_fn = container_fn.replace("/", "-").replace(":", "-")
        # Add file extension
        container_fn = container_fn + extension
        return container_fn

    def fetch_remote_containers(self, containers: list[tuple[str, Path]], parallel: int = 4) -> None:
        """
        Fetch remote containers in parallel:
        - A single thread pulls the images using the `docker image pull` command,
        - Multiple threads saves the pull images to tar archives using the `docker image save` command.
        Args:
            containers (Iterable[tuple[str, str]]): A list of tuples with the container name
        """
        # Update the main task -- we both need to pull and save the images
        self.progress.update_main_task(total=len(containers))

        # We use a single thread pool to pull and save images. To ensure that only a single image is pulled at a time,
        # we let the next pull task wait for the previous one to finish. Save tasks can run in parallel, but they need to
        # wait for the pull task to finish before they can save the image.
        with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as pool:
            futures = []

            # Initialize the wait_future to None, which will be used to wait for the previous pull task to finish
            for container, output_path in containers:
                # Submit the pull task to the pool
                future = pool.submit(self.pull_and_save_image, container, output_path)
                futures.append(future)

            # Make ctrl-c work with multi-threading: set a sentinel that is checked by the subprocesses
            self.kill_with_fire = False

            # Wait for all pull and save tasks to finish
            try:
                for future in concurrent.futures.as_completed(futures):
                    try:
                        future.result()  # This will raise an exception if the pull or save failed
                    except DockerError as e:
                        log.error(f"Error while processing container {e.container}: {e.message}")
                    except Exception as e:
                        log.error(f"Unexpected error: {e}")

            except KeyboardInterrupt:
                # Cancel the future threads that haven't started yet
                for future in futures:
                    future.cancel()
                # Set the sentinel to True to pass the signal to subprocesses
                self.kill_with_fire = True
                # Re-raise exception on the main thread
                raise

    def pull_and_save_image(self, container: str, output_path: Path):
        """
        Pull a docker image and then save it
        """
        # Progress bar to show that something is happening
        container_short_name = container.split("/")[-1][:50]
        task = self.progress.add_task(
            f"Fetching '{container_short_name}'",
            progress_type="docker",
            current_log="",
            total=2,
            status="Pulling",
        )

        self.pull_image(container, task)

        # Update progress bar
        self.progress.advance(task)
        self.progress.update(task, status="Saving")
        # self.progress.update(task, description=f"Saving '{container_short_name}'")

        # Save the image
        self.save_image(container, output_path, task)

        # Update progress bar
        self.progress.advance(task)
        self.progress.remove_task(task)

        # Task should advance in any case. Failure to pull will not kill the pulling process.
        self.progress.update_main_task(advance=1)

    def construct_pull_command(self, address: str):
        pull_command = ["docker", "image", "pull", address]
        return pull_command

    def pull_image(self, container: str, progress_task: rich.progress.Task) -> None:
        """
        Pull a single Docker image from a registry.

        Args:
            container (str): The container. Should be the full address of the container e.g. `quay.io/biocontainers/name:version`
            output_path (str): The final local output path
            prev_pull_future (concurrent.futures.Future, None): A future that is used to wait for the previous pull task to finish.
        """
        # Try pulling the image from the specified address
        try:
            pull_command = self.construct_pull_command(container)
            log.debug(f"Pulling docker image: {container}")
            log.debug(f"Docker command: {' '.join(pull_command)}")
            self._run_docker_command(pull_command, container, None, container, progress_task)
        except (DockerError.InvalidTagError, DockerError.ImageNotFoundError) as e:
            log.error(e.message)
            log.error(f"Not able to pull image of {container}. Service might be down or internet connection is dead.")
        except DockerError.OtherError as e:
            # Try other registries
            log.error(e.message)
            log.error(e.helpmessage)
            log.error(f"Not able to pull image of {container}. Service might be down or internet connection is dead.")

    def construct_save_command(self, output_path: Path, address: str):
        save_command = [
            "docker",
            "image",
            "save",
            address,
            "--output",
            str(output_path),
        ]
        return save_command

    def save_image(self, container: str, output_path: Path, progress_task: rich.progress.Task) -> None:
        """Save a Docker image that has been pulled to a file.

        Args:
            container (str): A pipeline's container name. Usually it is of similar format
                to ``biocontainers/name:tag``
            out_path (str): The final target output path
            cache_path (str, None): The NXF_DOCKER_CACHEDIR path if set, None if not
            wait_future (concurrent.futures.Future, None): A future that is used to wait for the previous pull task to finish.
        """
        log.debug(f"Saving Docker image '{container}' to {output_path}")
        address = container
        save_command = self.construct_save_command(output_path, address)
        self._run_docker_command(save_command, container, output_path, address, progress_task)

    def _run_docker_command(
        self,
        command: list[str],
        container: str,
        output_path: Optional[Path],
        address: str,
        progress_task: rich.progress.Task,
    ) -> None:
        """
        Internal command to run docker commands and error handle them properly
        """
        with subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=1,
        ) as proc:
            # Monitor the process:
            # - read lines if there are any,
            # - check if we should kill it,
            # - update the progress bar
            lines = []
            while True:
                if self.kill_with_fire:
                    proc.kill()
                    raise KeyboardInterrupt("Docker command was cancelled by user")

                rlist, _, _ = select.select([proc.stdout], [], [], 0.1)
                if rlist and proc.stdout is not None:
                    line = proc.stdout.readline()
                    if line:
                        lines.append(line)
                        self.progress.update(progress_task, current_log=line.strip())
                    elif proc.poll() is not None:
                        # Process has finished, break the loop
                        break
                elif proc.poll() is not None:
                    # Process has finished, break the loop
                    break
            log.debug(
                f"Docker command '{' '.join(command)}' finished with return code {proc.returncode}. Waiting for it to exit."
            )
            proc.wait()
            log.debug(f"Docker command '{' '.join(command)}' has exited.")

        if lines:
            # something went wrong with the container retrieval
            possible_error_lines = {
                "invalid reference format",
                "Error response from daemon:",
            }
            if any(pel in line for pel in possible_error_lines for line in lines):
                self.progress.remove_task(progress_task)
                raise DockerError(
                    container=container,
                    address=address,
                    out_path=output_path,
                    command=command,
                    error_msg=lines,
                )


class DockerProgress(ContainerProgress):
    def get_task_types_and_columns(self):
        task_types_and_columns = super().get_task_types_and_columns()
        task_types_and_columns.update(
            {
                "docker": (
                    "[magenta]{task.description}",
                    # "[blue]{task.fields[current_log]}",
                    rich.progress.BarColumn(bar_width=None),
                    "([blue]{task.fields[status]})",
                ),
            }
        )
        return task_types_and_columns


# Distinct errors for the docker container fetching, required for acting on the exceptions
class DockerError(Exception):
    """A class of errors related to pulling containers with Docker"""

    def __init__(
        self,
        container: str,
        address: str,
        out_path: Optional[Path],
        command: list[str],
        error_msg: list[str],
    ):
        self.container = container
        self.address = address
        self.out_path = out_path
        self.command = command
        self.error_msg = error_msg
        self.message = None

        error_patterns = {
            "reference does not exist": self.ImageNotPulledError,
            "repository does not exist": self.ImageNotFoundError,
            "manifest unknown": self.InvalidTagError,
        }
        for line in error_msg:
            for pattern, error_class in error_patterns.items():
                if pattern in line:
                    self.error_type = error_class(self)
                    break
        else:
            self.error_type = self.OtherError(self)

        log.error(self.error_type.message)
        log.info(self.error_type.helpmessage)
        log.debug(f"Failed command:\n{' '.join(self.command)}")
        log.debug(f"Docker error messages:\n{''.join(error_msg)}")

        raise self.error_type

    class ImageNotPulledError(AttributeError):
        """Docker is trying to save an image that was not pulled"""

        def __init__(self, error_log):
            self.error_log = error_log
            self.message = f'[bold red] Cannot save "{self.error_log.container}" as it was not pulled [/]\n'
            self.helpmessage = "Please pull the image first and confirm that it can be pulled.\n"
            super().__init__(self.message)

    class ImageNotFoundError(FileNotFoundError):
        """The image can not be found in the registry"""

        def __init__(self, error_log):
            self.error_log = error_log
            self.message = f'[bold red]"The pipeline requested the download of non-existing container image "{self.error_log.address}"[/]\n'
            self.helpmessage = (
                f'Please try to rerun \n"{" ".join(self.error_log.command)}" manually with a different registry.f\n'
            )

            super().__init__(self.message)

    class InvalidTagError(AttributeError):
        """Image and registry are valid, but the (version) tag is not"""

        def __init__(self, error_log):
            self.error_log = error_log
            self.message = f'[bold red]"{self.error_log.address.split(":")[-1]}" is not a valid tag of "{self.error_log.container}"[/]\n'
            self.helpmessage = f'Please chose a different library than {self.error_log.address}\nor try to locate the "{self.error_log.address.split(":")[-1]}" version of "{self.error_log.container}" manually.\nPlease troubleshoot the command \n"{" ".join(self.error_log.command)}" manually.\n'
            super().__init__(self.message)

    class OtherError(RuntimeError):
        """Undefined error with the container"""

        def __init__(self, error_log):
            self.error_log = error_log
            self.message = f'[bold red]"The pipeline requested the download of non-existing container image "{self.error_log.address}"[/]\n'
            self.helpmessage = (
                f'Please try to rerun \n"{" ".join(self.error_log.command)}" manually with a different registry.\n'
            )
            super().__init__(self.message, self.helpmessage, self.error_log)
