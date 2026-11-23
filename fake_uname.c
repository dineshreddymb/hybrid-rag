#define _GNU_SOURCE
#include <sys/utsname.h>
#include <string.h>
#include <dlfcn.h>

/*
 * Intercept uname() to spoof Linux kernel version as 6.8.0-generic.
 * This resolves MongoDB 8.0 SERVER-121912 startup check (validateRseqKernelCompat)
 * on cloud environments (like Railway, AWS, Azure) running kernels 6.19+ or 7.x.
 * Coupled with GLIBC_TUNABLES="glibc.pthread.rseq=0", TCMalloc falls back safely
 * to per-thread caching without failing on rseq ABI changes.
 */
int uname(struct utsname *buf) {
    int (*real_uname)(struct utsname *) = dlsym(RTLD_NEXT, "uname");
    int res = real_uname(buf);
    if (res == 0 && buf != NULL) {
        strncpy(buf->release, "6.8.0-generic", sizeof(buf->release) - 1);
        buf->release[sizeof(buf->release) - 1] = '\0';
    }
    return res;
}
