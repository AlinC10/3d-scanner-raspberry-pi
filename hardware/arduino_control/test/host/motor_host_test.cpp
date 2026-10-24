// Host-side test for Motor.cpp / CommandHandler.cpp against the fake Arduino.h in this folder.
// Plain C++17, no framework. Build and run with ./run.sh (see README.md).
#include <stdio.h>
#include <vector>

#include "Arduino.h"
#include "CommandHandler.h"
#include "Motor.h"
#include "Protocol.h"

static int g_checks = 0;
static int g_failed = 0;

#define CHECK(cond)                                                                \
    do {                                                                           \
        g_checks++;                                                                \
        if (!(cond)) {                                                             \
            g_failed++;                                                            \
            printf("    FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);             \
        }                                                                          \
    } while (0)

#define CHECK_NEAR(a, b, tol) CHECK(llabs((long long)(a) - (long long)(b)) <= (long long)(tol))

constexpr uint8_t STEP0 = 6, DIR0 = 7, EN0 = 8;  // turntable pins used by the Pi software
constexpr uint8_t STEP1 = 5, DIR1 = 4, EN1 = 3;  // Z axis pins
constexpr uint8_t ESTOP = 0xFF;

// Timestamps (us) of the rising edges written to `pin`.
static std::vector<uint32_t> risingEdges(const uint8_t pin) {
    std::vector<uint32_t> t;
    for (const PinEvent& e : fake::st().events) {
        if (!e.isMode && e.pin == pin && e.value == HIGH) t.push_back(e.t);
    }
    return t;
}

// True if EN was ever driven LOW (driver energized) by an OUTPUT pin since the last reset.
static bool enEverDrivenLow(const uint8_t pin) {
    for (const PinEvent& e : fake::st().events) {
        if (e.pin == pin && e.isOutput && e.driven == LOW) return true;
    }
    return false;
}

static void configure(Motor& m, const uint8_t id, const uint8_t step, const uint8_t dir, const uint8_t en) {
    m.pinSetup(id, step, dir, en);
}

static void test_fake_models_avr_port_latch() {
    printf("fake: pinMode(OUTPUT) drives the PORT register value\n");
    fake::reset();
    pinMode(20, OUTPUT);
    CHECK(fake::level(20) == LOW);  // this is exactly the pre-fix EN behaviour
    digitalWrite(21, HIGH);
    CHECK(fake::level(21) == LOW);  // input pin: not driven yet
    pinMode(21, OUTPUT);
    CHECK(fake::level(21) == HIGH);
}

static void test_en_high_after_pinsetup() {
    printf("A1: EN is HIGH (driver off) right after pinSetup\n");
    fake::reset();
    Motor m;
    configure(m, 0, STEP0, DIR0, EN0);
    CHECK(m.isConfigured());
    CHECK(!m.isEnabled());
    CHECK(fake::level(EN0) == HIGH);
    CHECK(!enEverDrivenLow(EN0));  // never glitched LOW while the pin became an output
}

static void test_four_arg_constructor_not_enabled() {
    printf("A1: 4-argument constructor starts with enableStatus=false\n");
    fake::reset();
    Motor m(0, STEP0, DIR0, EN0);
    CHECK(!m.isEnabled());
    CHECK(!m.isConfigured());
}

static void test_disable_always_writes_high() {
    printf("A1: disable() on an already-disabled motor still writes HIGH; idempotent\n");
    fake::reset();
    Motor m;
    configure(m, 0, STEP0, DIR0, EN0);
    // Simulate a pin that disagrees with enableStatus (e.g. left LOW by a previous firmware state).
    digitalWrite(EN0, LOW);
    CHECK(fake::level(EN0) == LOW);
    CHECK(!m.isEnabled());
    m.disable();
    CHECK(fake::level(EN0) == HIGH);
    // Second disable is fine and still writes the pin.
    size_t before = fake::st().events.size();
    m.disable();
    CHECK(fake::level(EN0) == HIGH);
    CHECK(fake::st().events.size() == before + 1);
    // stop() uses disable().
    digitalWrite(EN0, LOW);
    m.stop();
    CHECK(fake::level(EN0) == HIGH);
    CHECK(!m.isEnabled());
}

static void test_enable_disable_cycle() {
    printf("enable() drives LOW, disable() drives HIGH, enable() after disable works\n");
    fake::reset();
    Motor m;
    configure(m, 0, STEP0, DIR0, EN0);
    m.enable();
    CHECK(fake::level(EN0) == LOW);
    CHECK(m.isEnabled());
    m.disable();
    CHECK(fake::level(EN0) == HIGH);
    m.enable();
    CHECK(fake::level(EN0) == LOW);
}

static void test_unconfigured_motor_is_untouched() {
    printf("A1: unconfigured motor (EN=-1) is never driven\n");
    fake::reset();
    Motor m;
    m.disable();
    m.enable();
    CHECK(fake::st().events.empty());
    CHECK(m.rotate(true, 10, 1000) == STATUS_ERR_NOT_CONFIGURED);
}

static void test_idle_emergency_stop_disables_all() {
    printf("A1: 0xFF while idle puts EN HIGH on every configured motor\n");
    fake::reset();
    Motor motors[2];
    CommandHandler h(motors, 2);
    h.parseAndExecute("2 0 6 7 8");
    h.parseAndExecute("2 1 5 4 3");
    CHECK(fake::level(EN0) == HIGH);
    CHECK(fake::level(EN1) == HIGH);
    CHECK(!enEverDrivenLow(EN0) && !enEverDrivenLow(EN1));

    // The pre-fix bug state: pins LOW although enableStatus is false -> 0xFF used to be a no-op.
    digitalWrite(EN0, LOW);
    digitalWrite(EN1, LOW);
    fake::st().rx.push_back(ESTOP);
    h.update();
    CHECK(fake::level(EN0) == HIGH);
    CHECK(fake::level(EN1) == HIGH);
    CHECK(fake::st().rx.empty());

    // Also after both were energized by moves/ENABLE.
    h.parseAndExecute("3 0");
    h.parseAndExecute("3 1");
    CHECK(fake::level(EN0) == LOW && fake::level(EN1) == LOW);
    fake::st().rx.push_back(ESTOP);
    h.update();
    CHECK(fake::level(EN0) == HIGH && fake::level(EN1) == HIGH);

    // The '!' alias behaves the same.
    h.parseAndExecute("3 0");
    fake::st().rx.push_back('!');
    h.update();
    CHECK(fake::level(EN0) == HIGH);
}

static void test_idle_emergency_stop_with_one_motor_configured() {
    printf("0xFF while idle with an unconfigured motor present does not touch its pins\n");
    fake::reset();
    Motor motors[2];
    CommandHandler h(motors, 2);
    h.parseAndExecute("2 0 6 7 8");
    fake::st().events.clear();
    fake::st().rx.push_back(ESTOP);
    h.update();
    for (const PinEvent& e : fake::st().events) CHECK(e.pin == EN0);
    CHECK(fake::level(EN0) == HIGH);
}

static void test_disable_command() {
    printf("A1: DISABLE command right after CONFIG writes EN HIGH and replies 0\n");
    fake::reset();
    Motor motors[2];
    CommandHandler h(motors, 2);
    h.parseAndExecute("2 0 6 7 8");
    digitalWrite(EN0, LOW);  // pre-fix state
    fake::st().tx.clear();
    h.parseAndExecute("4 0");
    CHECK(fake::level(EN0) == HIGH);
    CHECK(fake::st().tx == "0\n");
}

static void test_rotate_first_pulse_immediate_and_period() {
    printf("A2: rotate(): first pulse is immediate, steps are one period apart, floor is 60 us\n");
    fake::reset();
    Motor m;
    configure(m, 0, STEP0, DIR0, EN0);
    const uint32_t t0 = fake::st().now;
    CHECK(m.rotate(true, 5, 1000) == STATUS_OK);
    const std::vector<uint32_t> p = risingEdges(STEP0);
    CHECK(p.size() == 5);
    CHECK(p[0] - t0 <= 30);  // only the 20 us DIR setup time precedes it
    for (size_t i = 1; i < p.size(); i++) CHECK_NEAR(p[i] - p[i - 1], 1000, 2);  // first interval is 1-2 us short (fake clock)
    CHECK(fake::level(DIR0) == HIGH);
    CHECK(fake::level(EN0) == LOW);  // auto-enabled
    // Completion is reported after the last period elapsed.
    CHECK_NEAR(fake::st().now - p.back(), 1000, 3);

    fake::reset();
    Motor f;
    configure(f, 0, STEP0, DIR0, EN0);
    CHECK(f.rotate(false, 4, 10) == STATUS_OK);  // 10 us requested -> clamped to the 60 us floor
    const std::vector<uint32_t> q = risingEdges(STEP0);
    CHECK(q.size() == 4);
    for (size_t i = 1; i < q.size(); i++) CHECK_NEAR(q[i] - q[i - 1], 60, 2);
    CHECK(fake::level(DIR0) == LOW);
}

static void test_rotate_invalid_args() {
    printf("rotate(): steps == 0 -> STATUS_ERR_INVALID_ARG, no pulses\n");
    fake::reset();
    Motor m;
    configure(m, 0, STEP0, DIR0, EN0);
    CHECK(m.rotate(true, 0, 1000) == STATUS_ERR_INVALID_ARG);
    CHECK(risingEdges(STEP0).empty());
}

static void test_rotate_micros_wraparound() {
    printf("A2: rotate() is wrap-safe across the 32-bit micros() rollover\n");
    fake::reset();
    fake::st().now = 0xFFFFFF00u;
    Motor m;
    configure(m, 0, STEP0, DIR0, EN0);
    CHECK(m.rotate(true, 6, 500) == STATUS_OK);
    const std::vector<uint32_t> p = risingEdges(STEP0);
    CHECK(p.size() == 6);
    for (size_t i = 1; i < p.size(); i++) CHECK_NEAR((uint32_t)(p[i] - p[i - 1]), 500, 2);
}

static void test_rotate_abort_during_long_wait() {
    printf("A2: 0xFF during a 30 ms wait in rotate() is honoured immediately\n");
    fake::reset();
    Motor m;
    configure(m, 0, STEP0, DIR0, EN0);
    const uint32_t t0 = fake::st().now;
    // Pulse 0 is at ~t0+20; the next one would be due ~30 ms later. Inject the stop 10 ms into the wait.
    const uint32_t tStop = t0 + 10000;
    fake::injectAt(tStop, ESTOP);
    const uint8_t status = m.rotate(true, 100, 30000);
    CHECK(status == STATUS_ERR_ABORTED);
    CHECK(fake::st().now - tStop < 1000);  // far less than the 20 ms left of the period
    CHECK(risingEdges(STEP0).size() == 1);
    CHECK(fake::level(EN0) == HIGH);
    CHECK(!m.isEnabled());
    CHECK(fake::st().rx.empty());  // the 0xFF byte was consumed
}

static void test_rotate_abort_before_first_pulse() {
    printf("rotate(): 0xFF already waiting when the move starts -> aborted with no pulse\n");
    fake::reset();
    Motor m;
    configure(m, 0, STEP0, DIR0, EN0);
    fake::st().rx.push_back(ESTOP);
    CHECK(m.rotate(true, 10, 1000) == STATUS_ERR_ABORTED);
    CHECK(risingEdges(STEP0).empty());
    CHECK(fake::level(EN0) == HIGH);
}

static void test_rotate_abort_in_final_wait() {
    printf("A2: 0xFF during the final step period of rotate() is still reported as aborted\n");
    fake::reset();
    Motor m;
    configure(m, 0, STEP0, DIR0, EN0);
    const uint32_t t0 = fake::st().now;
    fake::injectAt(t0 + 5000, ESTOP);
    CHECK(m.rotate(true, 1, 30000) == STATUS_ERR_ABORTED);
    CHECK(fake::st().now - (t0 + 5000) < 1000);
    CHECK(fake::level(EN0) == HIGH);
}

static void test_rotate_ramp_abort() {
    printf("0xFF during rotateRamp() (long slow-start wait) -> STATUS_ERR_ABORTED and EN HIGH\n");
    fake::reset();
    Motor m;
    configure(m, 0, STEP0, DIR0, EN0);
    const uint32_t t0 = fake::st().now;
    const uint32_t tStop = t0 + 12000;
    fake::injectAt(tStop, ESTOP);
    // start 30 ms -> the first accel steps are ~30 ms apart.
    const uint8_t status = m.rotateRamp(true, 200, 1000, 30000, 50, 50, 30000);
    CHECK(status == STATUS_ERR_ABORTED);
    CHECK(fake::st().now - tStop < 1000);
    CHECK(fake::level(EN0) == HIGH);
    CHECK(!m.isEnabled());
    CHECK(fake::st().rx.empty());
}

static void test_rotate_ramp_first_pulse_and_end_delay() {
    printf("A2/ramp: first pulse immediate; end_delay reaches the last step period\n");
    fake::reset();
    Motor m;
    configure(m, 0, STEP0, DIR0, EN0);
    const uint32_t t0 = fake::st().now;
    const uint32_t steps = 400, target = 1000, start = 4000, accel = 100, decel = 100, endDelay = 20000;
    CHECK(m.rotateRamp(true, steps, target, start, accel, decel, endDelay) == STATUS_OK);
    const std::vector<uint32_t> p = risingEdges(STEP0);
    CHECK(p.size() == steps);
    CHECK(p[0] - t0 <= 30);
    const uint32_t tDone = fake::st().now;
    // The last step period is the wait after the final pulse (rotateRamp waits it out before returning).
    CHECK_NEAR(tDone - p.back(), endDelay, 5);
    // The step before it is one period slightly shorter than endDelay, all periods stay within [target, endDelay].
    for (size_t i = 1; i < p.size(); i++) {
        const uint32_t d = p[i] - p[i - 1];
        CHECK(d >= target - 1 && d <= endDelay + 1);
    }
    // Cruise phase runs at the target delay.
    CHECK(p[200] - p[199] == target);
    // Deceleration is monotonic.
    for (size_t i = accel + (steps - accel - decel) + 1; i < p.size(); i++) {
        CHECK((p[i] - p[i - 1]) >= (p[i - 1] - p[i - 2]));
    }
    CHECK(fake::level(EN0) == LOW);
}

static void test_rotate_ramp_end_delay_zero() {
    printf("ramp: end_delay == 0 decelerates back to start_delay\n");
    fake::reset();
    Motor m;
    configure(m, 0, STEP0, DIR0, EN0);
    CHECK(m.rotateRamp(false, 300, 1000, 4000, 100, 100, 0) == STATUS_OK);
    const std::vector<uint32_t> p = risingEdges(STEP0);
    CHECK(p.size() == 300);
    CHECK_NEAR(fake::st().now - p.back(), 4000, 5);
}

static void test_command_handler_abort_reply() {
    printf("A1/A2: 0xFF mid-move through CommandHandler -> reply 4, EN HIGH\n");
    fake::reset();
    Motor motors[2];
    CommandHandler h(motors, 2);
    h.parseAndExecute("2 0 6 7 8");
    h.parseAndExecute("2 1 5 4 3");
    fake::st().tx.clear();

    const uint32_t tStop = fake::st().now + 5000;
    fake::injectAt(tStop, ESTOP);
    h.parseAndExecute("5 0 100 1 30000");  // ROTATE, 30 ms steps
    CHECK(fake::st().tx == "4\n");
    CHECK(fake::level(EN0) == HIGH);
    CHECK(fake::st().now - tStop < 1000);

    fake::st().tx.clear();
    fake::injectAt(fake::st().now + 5000, ESTOP);
    h.parseAndExecute("16 1 100 1 1000 20000 20 20 20000");  // ACCEL_ROTATE
    CHECK(fake::st().tx == "4\n");
    CHECK(fake::level(EN1) == HIGH);
}

static void test_command_handler_end_delay_argument() {
    printf("ramp: <end_us> argument of the 16 command reaches rotateRamp\n");
    fake::reset();
    Motor motors[2];
    CommandHandler h(motors, 2);
    h.parseAndExecute("2 0 6 7 8");
    fake::st().tx.clear();
    h.parseAndExecute("16 0 200 1 1000 4000 50 50 15000");
    CHECK(fake::st().tx == "0\n");
    const std::vector<uint32_t> p = risingEdges(STEP0);
    CHECK(p.size() == 200);
    CHECK_NEAR(fake::st().now - p.back(), 15000, 5);
}

int main() {
    test_fake_models_avr_port_latch();
    test_en_high_after_pinsetup();
    test_four_arg_constructor_not_enabled();
    test_disable_always_writes_high();
    test_enable_disable_cycle();
    test_unconfigured_motor_is_untouched();
    test_idle_emergency_stop_disables_all();
    test_idle_emergency_stop_with_one_motor_configured();
    test_disable_command();
    test_rotate_first_pulse_immediate_and_period();
    test_rotate_invalid_args();
    test_rotate_micros_wraparound();
    test_rotate_abort_during_long_wait();
    test_rotate_abort_before_first_pulse();
    test_rotate_abort_in_final_wait();
    test_rotate_ramp_abort();
    test_rotate_ramp_first_pulse_and_end_delay();
    test_rotate_ramp_end_delay_zero();
    test_command_handler_abort_reply();
    test_command_handler_end_delay_argument();

    printf("\n%d checks, %d failed\n", g_checks, g_failed);
    return g_failed == 0 ? 0 : 1;
}
