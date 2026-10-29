#define _GNU_SOURCE
#include <sched.h>
#include <pthread.h>
#include <stdint.h>
#include <unistd.h>

struct fixture_disk { uint64_t major, first_minor; };
struct fixture_queue { struct fixture_disk *disk; };
struct fixture_request {
    struct fixture_queue *q;
    uint64_t cmd_flags, __data_len, rq_flags, state;
};
struct fixture_flush_queue { struct fixture_request *flush_rq; };

#define MARKER(name) \
    __attribute__((noinline, used)) void name(struct fixture_request *rq, \
        uint64_t error, uint64_t bytes, uint64_t now) { \
        __asm__ volatile("" : : "r"(rq), "r"(error), "r"(bytes), "r"(now) : "memory"); \
    }
MARKER(logical_start)
MARKER(issue)
MARKER(complete)
MARKER(flush_done)
MARKER(requeue)
MARKER(logical_done)
MARKER(merge)
MARKER(release)
MARKER(tick)

__attribute__((noinline, used)) void flush_sequence(struct fixture_request *rq,
    struct fixture_flush_queue *fq, uint64_t seq, uint64_t error, uint64_t now)
{
    __asm__ volatile("" : : "r"(rq), "r"(fq), "r"(seq), "r"(error), "r"(now) : "memory");
}

#define AT(event, request, ms) event(&(request), 0, 0, (uint64_t)(ms) * 1000000)
#define COMPLETE(request, ms, bytes) complete(&(request), 0, bytes, (uint64_t)(ms) * 1000000)
#define FLUSH_DONE(request, ms) do { \
    (request).state = 2; AT(flush_done, request, ms); \
    (request).state = 0; \
    flush_sequence(&flush, &fq, 1, 0, (uint64_t)(ms) * 1000000); \
} while (0)

struct competing_callbacks {
    struct fixture_request *rq, *logical;
    struct fixture_flush_queue *fq;
    pthread_barrier_t entry;
    pthread_mutex_t lock;
    int refs;
    uint64_t now;
};

static void *finish_reference(void *arg)
{
    struct competing_callbacks *c = arg;
    pthread_barrier_wait(&c->entry);
    // Kernel callback entry is outside mq_flush_lock. COMPLETE does not mean
    // the timeout and completion reference holders have both returned.
    flush_done(c->rq, 0, 0, c->now);
    pthread_mutex_lock(&c->lock);
    if (--c->refs == 0) {
        c->rq->state = 0;
        flush_sequence(c->logical, c->fq, 1, 0, c->now);
        // Several logical requests can share this physical completion.
        flush_sequence(c->logical, c->fq, 4, 0, c->now);
    }
    pthread_mutex_unlock(&c->lock);
    return NULL;
}

int main(void)
{
    usleep(1100000); // A zero-duration capture must survive the first timer tick.
    cpu_set_t allowed, original;
    if (sched_getaffinity(0, sizeof(original), &original)) return 2;
    if (CPU_COUNT(&original) < 2) return 2;
    allowed = original;
    for (int cpu = 0; cpu < CPU_SETSIZE; cpu++) {
        if (CPU_ISSET(cpu, &allowed)) {
            CPU_ZERO(&allowed); CPU_SET(cpu, &allowed);
            if (sched_setaffinity(0, sizeof(allowed), &allowed)) return 2;
            break;
        }
    }
    struct fixture_disk disk = {8, 0}, other_disk = {259, 0};
    struct fixture_queue queue = {&disk}, other_queue = {&other_disk};
    struct fixture_request a = {&queue, 0, 4096, 0};
    struct fixture_request b = {&queue, 1, 4096, 0};
    struct fixture_request flush = {&queue, 2 | (1ULL << 18), 0, 0};
    struct fixture_request internal = {&queue, 2, 0, 2};
    struct fixture_flush_queue fq = {&internal};
    struct fixture_request other = {&other_queue, 0, 4096, 0};

    // Attachment can observe starts with no matching completion hook yet.
    AT(logical_start, a, 500); AT(issue, a, 510);
    AT(release, other, 600);
    AT(logical_start, a, 650); AT(issue, a, 660);
    AT(flush_done, internal, 800);
    flush_sequence(&flush, &fq, 1, 0, 800000000);
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

    // Internal flush requests are reused without being freed. Recover a missed
    // generic completion at the flush callback before the next issue.
    internal.cmd_flags = 262146; internal.rq_flags = 10;
    AT(issue, internal, 15000);
    internal.state = 1; AT(flush_done, internal, 15050);
    FLUSH_DONE(internal, 15120);
    AT(issue, internal, 15200); COMPLETE(internal, 15320, 0);
    FLUSH_DONE(internal, 15321);
    AT(issue, internal, 15400); FLUSH_DONE(internal, 15520);
    AT(issue, internal, 15600); AT(issue, internal, 15620);
    FLUSH_DONE(internal, 15720);

    // A timeout reference can enter after state becomes COMPLETE, before
    // generic completion or the final reference holder consumes the request.
    AT(issue, internal, 16000);
    internal.state = 2; AT(flush_done, internal, 16050);
    COMPLETE(internal, 16120, 0); FLUSH_DONE(internal, 16121);

    // Data-stage and initialization callbacks must not consume a live flush.
    AT(issue, internal, 17000);
    internal.state = 1;
    flush_sequence(&flush, &fq, 1, 0, 17050000000);
    internal.state = 0;
    flush_sequence(&flush, &fq, 2, 0, 17060000000);
    flush_sequence(&flush, &fq, 6, 0, 17070000000);
    flush_sequence(&flush, &fq, 4, 5, 17120000000);

    // A completed waiter can kick the next physical flush before the old
    // callback finishes walking its remaining logical waiters.
    AT(issue, internal, 18000); FLUSH_DONE(internal, 18120);
    AT(issue, internal, 18200); internal.state = 1;
    flush_sequence(&flush, &fq, 4, 0, 18250000000);
    FLUSH_DONE(internal, 18320);

    AT(logical_start, b, 20000); AT(issue, b, 20020);
    AT(logical_start, a, 30000); AT(issue, a, 30010);
    COMPLETE(a, 36010, 4096); AT(logical_done, a, 36020); AT(release, a, 36030);
    // Restore the original CPU set so timeout/completion callbacks can overlap.
    if (sched_setaffinity(0, sizeof(original), &original)) return 2;
    struct competing_callbacks c = {
        .rq = &internal, .logical = &flush, .fq = &fq,
        .lock = PTHREAD_MUTEX_INITIALIZER,
    };
    if (pthread_barrier_init(&c.entry, NULL, 2)) return 2;
    for (int i = 0; i < 128; i++) {
        uint64_t start = 100000 + i * 1000;
        AT(issue, internal, start);
        internal.state = 2; c.refs = 2; c.now = (start + 120) * 1000000;
        pthread_t first, second;
        if (pthread_create(&first, NULL, finish_reference, &c) ||
            pthread_create(&second, NULL, finish_reference, &c)) return 2;
        if (pthread_join(first, NULL) || pthread_join(second, NULL)) return 2;
    }
    pthread_barrier_destroy(&c.entry);
    pthread_mutex_destroy(&c.lock);
    AT(tick, a, 40000);
    usleep(50000); // Let asynchronous map snapshots drain before END clears them.
    return 0;
}
