#define _GNU_SOURCE
#include <sched.h>
#include <stdint.h>
#include <unistd.h>

struct fixture_disk { uint64_t major, first_minor; };
struct fixture_queue { struct fixture_disk *disk; };
struct fixture_request {
    struct fixture_queue *q;
    uint64_t cmd_flags, __data_len, rq_flags;
};

#define MARKER(name) \
    __attribute__((noinline, used)) void name(struct fixture_request *rq, \
        uint64_t error, uint64_t bytes, uint64_t now) { \
        __asm__ volatile("" : : "r"(rq), "r"(error), "r"(bytes), "r"(now) : "memory"); \
    }
MARKER(logical_start)
MARKER(issue)
MARKER(complete)
MARKER(requeue)
MARKER(logical_done)
MARKER(merge)
MARKER(release)
MARKER(tick)

#define AT(event, request, ms) event(&(request), 0, 0, (uint64_t)(ms) * 1000000)
#define COMPLETE(request, ms, bytes) complete(&(request), 0, bytes, (uint64_t)(ms) * 1000000)

int main(void)
{
    usleep(1100000); // A zero-duration capture must survive the first timer tick.
    cpu_set_t allowed;
    if (!sched_getaffinity(0, sizeof(allowed), &allowed)) {
        for (int cpu = 0; cpu < CPU_SETSIZE; cpu++) {
            if (CPU_ISSET(cpu, &allowed)) {
                CPU_ZERO(&allowed); CPU_SET(cpu, &allowed);
                sched_setaffinity(0, sizeof(allowed), &allowed);
                break;
            }
        }
    }
    struct fixture_disk disk = {8, 0}, other_disk = {259, 0};
    struct fixture_queue queue = {&disk}, other_queue = {&other_disk};
    struct fixture_request a = {&queue, 0, 4096, 0};
    struct fixture_request b = {&queue, 1, 4096, 0};
    struct fixture_request flush = {&queue, 2 | (1ULL << 18), 0, 0};
    struct fixture_request internal = {&queue, 2, 0, 2};
    struct fixture_request other = {&other_queue, 0, 4096, 0};

    // Attachment can observe starts with no matching completion hook yet.
    AT(logical_start, a, 500); AT(issue, a, 510);
    AT(release, other, 900);
    AT(logical_start, a, 1000); AT(issue, a, 1020);
    COMPLETE(a, 1160, 4096); AT(logical_done, a, 1165); AT(release, a, 1170);

    AT(logical_start, b, 2000); AT(issue, b, 2010); AT(requeue, b, 2060);
    AT(issue, b, 2090); COMPLETE(b, 2220, 4096); AT(logical_done, b, 2230); AT(release, b, 2240);

    b.__data_len = 8192;
    AT(logical_start, b, 3000); AT(issue, b, 3010); COMPLETE(b, 3050, 4096);
    b.__data_len = 4096;
    COMPLETE(b, 3170, 4096); AT(logical_done, b, 3180); AT(release, b, 3190);

    AT(logical_start, flush, 4000); AT(issue, internal, 4020);
    COMPLETE(internal, 4140, 0); AT(logical_done, flush, 4150); AT(release, flush, 4160);

    // A FUA write completes its data before the postflush completes.
    b.cmd_flags = 1 | (1ULL << 17);
    AT(logical_start, b, 4500); b.rq_flags = 2; AT(issue, b, 4510);
    COMPLETE(b, 4570, 4096); AT(logical_done, b, 4575);
    AT(issue, internal, 4580); COMPLETE(internal, 4700, 0);
    b.rq_flags = 0; AT(logical_done, b, 4750); AT(release, b, 4760);

    b.cmd_flags = 1;
    AT(logical_start, b, 5000); AT(merge, b, 5030); AT(release, b, 5040);
    AT(logical_start, b, 6000); AT(release, b, 6030);
    AT(logical_start, b, 7000); AT(logical_done, b, 7150); AT(release, b, 7160);
    AT(logical_start, b, 8000); AT(issue, b, 8010); AT(issue, b, 8020);
    COMPLETE(b, 8140, 4096); AT(logical_done, b, 8150); AT(release, b, 8160);
    AT(logical_start, b, 9000); AT(issue, b, 9010);
    AT(logical_done, b, 9150); AT(release, b, 9160);

    AT(logical_start, other, 10000); AT(issue, other, 10010);
    COMPLETE(other, 10120, 4096); AT(logical_done, other, 10130);

    AT(logical_start, b, 11000); AT(logical_start, b, 11010);
    AT(release, b, 11020);

    AT(logical_start, b, 20000); AT(issue, b, 20020);
    AT(logical_start, a, 30000); AT(issue, a, 30010);
    COMPLETE(a, 36010, 4096); AT(logical_done, a, 36020); AT(release, a, 36030);
    AT(tick, a, 40000);
    usleep(50000); // Let asynchronous map snapshots drain before END clears them.
    return 0;
}
