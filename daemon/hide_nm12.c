/* LD_PRELOAD shim: hide the non-contiguous NM12/NM21 formats from VIDIOC_ENUM_FMT, so
 * v4l2src picks single-plane NV12 (one dmabuf per frame) and mpph265enc can import it
 * directly instead of CPU-copying every frame.
 *   gcc -O2 -shared -fPIC -o hide_nm12.so hide_nm12.c -ldl
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdarg.h>
#include <sys/ioctl.h>
#include <linux/videodev2.h>

static int (*real_ioctl)(int, unsigned long, ...);

static int hidden(__u32 f) { return f == V4L2_PIX_FMT_NV12M || f == V4L2_PIX_FMT_NV21M; }

int ioctl(int fd, unsigned long req, ...)
{
	va_list ap;
	va_start(ap, req);
	void *arg = va_arg(ap, void *);
	va_end(ap);
	if (!real_ioctl)
		real_ioctl = dlsym(RTLD_NEXT, "ioctl");
	if (req != VIDIOC_ENUM_FMT)
		return real_ioctl(fd, req, arg);

	/* Map the caller's index onto the driver's list with the hidden formats skipped */
	struct v4l2_fmtdesc *f = arg;
	__u32 want = f->index, seen = 0;
	for (__u32 idx = 0;; idx++) {
		f->index = idx;
		int r = real_ioctl(fd, req, f);
		if (r) {
			f->index = want;
			return r;
		}
		if (hidden(f->pixelformat))
			continue;
		if (seen++ == want) {
			f->index = want;
			return 0;
		}
	}
}
