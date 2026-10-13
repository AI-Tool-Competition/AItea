/* SPDX-License-Identifier: MIT
 * AItea offline launcher for reviewed read-only antiword/libwpd converters.
 * No shell; exec replaces this process so PDEATHSIG protects the converter.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/audit.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <sched.h>
#include <signal.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

static void fail(void) { fputs("offline converter rejected input or isolation unavailable\n", stderr); exit(2); }

static void proc_map(const char *name, const char *value) {
    char path[80]; snprintf(path, sizeof(path), "/proc/self/%s", name);
    int fd = open(path, O_WRONLY | O_CLOEXEC);
    if (fd < 0 || write(fd, value, strlen(value)) != (ssize_t)strlen(value) || close(fd)) fail();
}

/* Native converters are single-threaded. Deny process creation and all socket
 * creation, including AF_UNIX (a network namespace alone does not isolate it).
 * No userspace signal/preexec hook is needed in a multithreaded Python parent.
 */
static void syscall_filter(void) {
#if defined(__x86_64__)
#define TARGET_ARCH AUDIT_ARCH_X86_64
#elif defined(__aarch64__)
#define TARGET_ARCH AUDIT_ARCH_AARCH64
#else
#error "Review the seccomp architecture before using this launcher"
#endif
#define DENY(n) BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, (n), 0, 1), BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM)
    struct sock_filter rules[] = {
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, arch)),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, TARGET_ARCH, 1, 0),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS),
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, nr)),
#if defined(__x86_64__)
        /* x32 shares AUDIT_ARCH_X86_64 but uses different syscall numbers. */
        BPF_JUMP(BPF_JMP | BPF_JGE | BPF_K, 0x40000000u, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS),
#endif
        DENY(__NR_socket), DENY(__NR_socketpair), DENY(__NR_connect),
        DENY(__NR_clone),
#ifdef __NR_clone3
        DENY(__NR_clone3),
#endif
#ifdef __NR_fork
        DENY(__NR_fork),
#endif
#ifdef __NR_vfork
        DENY(__NR_vfork),
#endif
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW)
    };
    struct sock_fprog program = {sizeof(rules) / sizeof(rules[0]), rules};
    if (prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &program)) fail();
}

int main(int argc, char **argv) {
    if (argc != 6 || argv[3][0] != '/' || argv[4][0] != '/' || argv[5][0] != '/') fail();
    char *end; long expected = strtol(argv[1], &end, 10);
    if (*end || expected <= 1 || expected != getppid()) fail();
    if (prctl(PR_SET_PDEATHSIG, SIGKILL) || expected != getppid()) fail();
    uid_t uid = getuid(); gid_t gid = getgid(); char mapping[80];
    int executable = open(argv[3], O_RDONLY | O_NOFOLLOW);
    struct stat info;
    if (executable < 0 || fstat(executable, &info) || !S_ISREG(info.st_mode)
        || !(info.st_mode & S_IXUSR) || info.st_mode & (S_IWGRP | S_IWOTH)
        || (info.st_uid != 0 && info.st_uid != uid)) fail();
    if (unshare(CLONE_NEWUSER)) fail();
    proc_map("setgroups", "deny");
    snprintf(mapping, sizeof(mapping), "0 %lu 1\n", (unsigned long)uid); proc_map("uid_map", mapping);
    snprintf(mapping, sizeof(mapping), "0 %lu 1\n", (unsigned long)gid); proc_map("gid_map", mapping);
    if (unshare(CLONE_NEWNET) || prctl(PR_SET_PDEATHSIG, SIGKILL) || expected != getppid()
        || prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)) fail();
    struct rlimit cpu = {15, 15}, memory = {256 * 1024 * 1024, 256 * 1024 * 1024}, core = {0, 0};
    if (setrlimit(RLIMIT_CPU, &cpu) || setrlimit(RLIMIT_AS, &memory) || setrlimit(RLIMIT_CORE, &core)) fail();
    alarm(20);
    if (clearenv() || setenv("LANG", "C.UTF-8", 1) || setenv("LC_ALL", "C.UTF-8", 1)
        || setenv("PATH", "/usr/bin:/bin", 1)) fail();
    char *doc[] = {argv[3], "-m", "UTF-8.txt", "-x", "db", argv[5], NULL};
    char *wpd[] = {argv[3], argv[5], NULL};
    char **arguments;
    if (!strcmp(argv[2], "docbook")) {
        if (setenv("ANTIWORDHOME", argv[4], 1)) fail();
        arguments = doc;
    } else if (!strcmp(argv[2], "wpf-html") || !strcmp(argv[2], "wpf-raw")) {
        if (setenv("LD_LIBRARY_PATH", argv[4], 1)) fail();
        arguments = wpd;
    } else fail();
    syscall_filter();
    extern char **environ;
    fexecve(executable, arguments, environ);
    fail();
}
