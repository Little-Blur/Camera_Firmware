/* pacedwrite FILE MBPS SECONDS: steady writes to the NVMe, no page cache, no bursts.
 * 1 MiB O_DIRECT writes on an absolute-time schedule (MBPS per second), wrapping at 256 MiB.
 * Prints one line per second: t, writes, max write latency (ms). */
#define _GNU_SOURCE
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define CHUNK (1 << 20)
#define WRAP  (256LL << 20)

static double ts(const struct timespec *t) { return t->tv_sec + t->tv_nsec * 1e-9; }

int main(int argc, char **argv)
{
	if (argc != 4) { fprintf(stderr, "usage: pacedwrite FILE MBPS SECONDS\n"); return 2; }
	double mbps = atof(argv[2]), dur = atof(argv[3]);
	int fd = open(argv[1], O_WRONLY | O_CREAT | O_DIRECT, 0644);
	if (fd < 0) { perror("open"); return 1; }
	void *buf;
	if (posix_memalign(&buf, 4096, CHUNK)) return 1;
	FILE *r = fopen("/dev/urandom", "r");
	if (!r || fread(buf, 1, CHUNK, r) != CHUNK) return 1;
	fclose(r);

	long long period = (long long)(1e9 / mbps), off = 0;
	struct timespec next, t0, a, b;
	clock_gettime(CLOCK_MONOTONIC, &next);
	t0 = next;
	double sec_start = ts(&t0), maxlat = 0;
	int n = 0;
	while (ts(&next) - ts(&t0) < dur) {
		clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &next, NULL);
		clock_gettime(CLOCK_MONOTONIC, &a);
		if (pwrite(fd, buf, CHUNK, off) != CHUNK) { perror("pwrite"); return 1; }
		clock_gettime(CLOCK_MONOTONIC, &b);
		double lat = (ts(&b) - ts(&a)) * 1e3;
		if (lat > maxlat) maxlat = lat;
		n++;
		off = (off + CHUNK) % WRAP;
		if (ts(&b) - sec_start >= 1.0) {
			printf("%.3f %d %.2f\n", ts(&b) - ts(&t0), n, maxlat);
			fflush(stdout);
			sec_start = ts(&b); n = 0; maxlat = 0;
		}
		next.tv_nsec += period;
		while (next.tv_nsec >= 1000000000L) { next.tv_nsec -= 1000000000L; next.tv_sec++; }
	}
	close(fd);
	return 0;
}
