/*
 * ojrun — minimal high-precision timing wrapper for judge test cases.
 *
 * Replaces `/usr/bin/time -f "OJ_TIME %M %e"`:
 *   * wall clock from clock_gettime(CLOCK_MONOTONIC) — microsecond report
 *     resolution instead of GNU time's 10 ms `%e` granularity;
 *   * peak RSS from wait4() rusage (ru_maxrss, KiB) — identical semantics
 *     to GNU time's `%M`;
 *   * exit status passthrough follows the shell/GNU-time convention
 *     (128+signo on signal death) so `exit_indicates_memory_limit(137)`
 *     keeps working.
 *
 * The report line on stderr is byte-compatible with the old one:
 *     OJ_TIME <maxrss_kb> <elapsed_seconds with 6 decimals>
 * so SandboxRunner._parse_time_stderr needs no format change.
 *
 * Build (static, runs in any judge image regardless of libc):
 *     gcc -O2 -static -s -o docker/judge/ojbin/ojrun docker/judge/ojrun.c
 * The committed binary is bind-mounted read-only into judge containers at
 * /opt/oj (see submissions/sandbox.py); a missing binary transparently
 * falls back to /usr/bin/time.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static double now_mono(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

int main(int argc, char **argv)
{
    if (argc < 2) {
        fprintf(stderr, "usage: ojrun <command> [args...]\n");
        return 127;
    }

    double start = now_mono();
    pid_t pid = fork();
    if (pid < 0) {
        fprintf(stderr, "ojrun: fork: %s\n", strerror(errno));
        return 127;
    }
    if (pid == 0) {
        execvp(argv[1], argv + 1);
        fprintf(stderr, "ojrun: exec %s: %s\n", argv[1], strerror(errno));
        _exit(127);
    }

    int status = 0;
    struct rusage ru;
    while (wait4(pid, &status, 0, &ru) < 0) {
        if (errno == EINTR)
            continue;
        fprintf(stderr, "ojrun: wait4: %s\n", strerror(errno));
        return 127;
    }
    double elapsed = now_mono() - start;

    /* Always report, even on signal death — the caller distinguishes a
     * missing report (killed before write) from a real measurement. */
    fprintf(stderr, "OJ_TIME %ld %.6f\n", ru.ru_maxrss, elapsed);

    if (WIFEXITED(status))
        return WEXITSTATUS(status);
    if (WIFSIGNALED(status))
        return 128 + WTERMSIG(status);
    return 127;
}
