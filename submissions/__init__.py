import threading
from .container_cleanup import start_container_cleanup

# Start container cleanup daemon when submissions app loads
thread = threading.Thread(target=start_container_cleanup, daemon=True, name='container-cleanup-init')
thread.start()
