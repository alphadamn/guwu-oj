from .container_cleanup import DockerCleanupThread

thread = DockerCleanupThread(interval=5)
thread.start()
