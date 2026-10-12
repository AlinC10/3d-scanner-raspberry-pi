#include <iostream>
#include <cstring>
#include <cstdlib>
#include <stdint.h>

int main() {
    const char* cmd = "16 0 200 1 2000 6000 40 40";
    char* p = strchr((char*)cmd, ' ');
    if (p) p++;

    std::cout << "cmd[0]: " << cmd[0] << ", cmd[1]: " << cmd[1] << std::endl;
    std::cout << "p points to: '" << p << "'" << std::endl;

    uint8_t id = strtol(p, &p, 10);
    uint32_t steps = strtoul(p, &p, 10);
    bool cw = strtol(p, &p, 10) == 1;
    uint32_t target_delay = strtoul(p, &p, 10);
    uint32_t start_delay = strtoul(p, &p, 10);
    uint32_t accel = strtoul(p, &p, 10);
    uint32_t decel = strtoul(p, &p, 10);

    std::cout << "id: " << (int)id << std::endl;
    std::cout << "steps: " << steps << std::endl;
    std::cout << "cw: " << cw << std::endl;
    std::cout << "target_delay: " << target_delay << std::endl;
    std::cout << "start_delay: " << start_delay << std::endl;
    std::cout << "accel: " << accel << std::endl;
    std::cout << "decel: " << decel << std::endl;

    return 0;
}
