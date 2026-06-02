# Docker Judge Security Setup

This directory contains security profiles for the Docker judge container.

## Security Features

### Two-Step Seccomp Approach

The judge uses a two-step seccomp approach for optimal security:

1. **Compilation Phase** (`seccomp-compile.json`):
   - Relaxed seccomp profile that allows compilers to function properly
   - Allows process creation (clone, fork, vfork) for compiler processes
   - Allows file write operations for creating compiled binaries
   - Allows execve for running compiler tools
   - Still blocks network access
   - Blocks filesystem manipulation (mount, umount, pivot_root, chroot)
   - Blocks kernel module loading

2. **Execution Phase** (`seccomp-execute.json`):
   - Tight seccomp profile that blocks dangerous syscalls
   - Blocks process creation (clone, fork, vfork, clone3)
   - Blocks execution of other programs (execve, execveat)
   - Blocks file modification operations (write, truncate, unlink, etc.)
   - Blocks network access
   - Blocks filesystem manipulation
   - Blocks kernel module loading
   - Blocks process manipulation (ptrace, process_vm_readv, process_vm_writev)
   - Blocks key management syscalls
   - Blocks privilege escalation syscalls
   - Blocks IPC operations (semaphores, shared memory, message queues)
   - Blocks inotify and fanotify operations
   - Blocks BPF operations
   - Blocks perf events

This approach ensures that:
- Compilers can work normally with necessary permissions
- Executed code runs in a highly restricted environment
- Security is maximized without breaking functionality

### AppArmor Profile (apparmor-profile)
- Provides additional confinement beyond Docker's default security
- Restricts file access to /sandbox directory
- Denies network access
- Denies device access (except specific allowed devices)
- Denies access to sensitive system files (/etc/passwd, /etc/shadow, etc.)
- Denies ptrace and process manipulation
- Denies mount operations
- Denys module operations

### Additional Security Options in sandbox.py
- `--network none`: No network access
- `--security-opt no-new-privileges`: Prevents privilege escalation
- `--cap-drop ALL`: Drops all Linux capabilities
- `--read-only`: Read-only root filesystem
- `--tmpfs /tmp`: Temporary filesystem for /tmp
- `--device` restrictions: Only allows specific devices (/dev/null, /dev/zero, /dev/random, /dev/urandom)
- Runs as unprivileged user (nobody, uid/gid 65534)
- PIDs limit: Restricts number of processes

## Setup Instructions

### 1. Load AppArmor Profile (Linux only)

The AppArmor profile must be loaded into the kernel before use. Run the following commands as root:

```bash
# Copy the profile to AppArmor directory
sudo cp docker/judge/apparmor-profile /etc/apparmor.d/oj-judge

# Load the profile
sudo apparmor_parser -r /etc/apparmor.d/oj-judge

# Verify the profile is loaded
sudo aa-status | grep oj-judge
```

### 2. Build Docker Image

```bash
docker build -t oj-judge:latest docker/judge
```

### 3. Test the Setup

You can test if the security profiles are working by running a test container:

```bash
docker run --rm \
  --security-opt seccomp=docker/judge/seccomp-profile.json \
  --security-opt apparmor=oj-judge \
  --cap-drop ALL \
  --read-only \
  --tmpfs /tmp \
  oj-judge:latest \
  ls /sandbox
```

### 4. Disable AppArmor if Not Available (Optional)

If you're running on a system without AppArmor support (e.g., some Docker Desktop configurations), you can disable AppArmor by modifying `submissions/sandbox.py`:

Comment out or remove this line:
```python
'--security-opt', 'apparmor=oj-judge',
```

The Seccomp profile will still provide strong security even without AppArmor.

## Security Checklist

- [x] Network isolation (--network none)
- [x] No new privileges (--security-opt no-new-privileges)
- [x] Capability dropping (--cap-drop ALL)
- [x] Seccomp profile (system call filtering)
- [x] AppArmor profile (additional confinement)
- [x] Read-only root filesystem
- [x] Temporary filesystem for /tmp
- [x] Device access restrictions
- [x] Unprivileged user (nobody)
- [x] Process limit (pids-limit)
- [x] Memory limits
- [x] Time limits

## Troubleshooting

### AppArmor Profile Not Found

If you get an error about the AppArmor profile not being found, ensure:
1. You're running on Linux (AppArmor is Linux-specific)
2. AppArmor is enabled on your system
3. The profile has been loaded with `apparmor_parser`

### Seccomp Profile Not Found

If you get an error about the Seccomp profile not being found, ensure:
1. The path to `seccomp-profile.json` is correct in `sandbox.py`
2. The file exists and is readable

### Permission Denied Errors

If you encounter permission errors, ensure:
1. The work directory has proper permissions (0777)
2. Docker has access to the necessary directories
